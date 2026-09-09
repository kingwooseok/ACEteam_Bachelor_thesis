#!/usr/bin/env python3
"""Join ACE receive-path measurements and calculate latency components.

The UDP input is the receiver's native-endian 56-byte ``sample_record`` ABI,
so it must be analyzed on a host with the same byte order and C integer ABI as
the capture host. CSV outputs from the XDP tools are portable.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import os
import re
import statistics
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence, TextIO, cast


# 이 분석기는 한 번의 실험에서 흩어져 저장된 관측을 패킷별로 다시 연결한다.
# ingress CSV -> NIC 쪽 최초 XDP, CPUMAP CSV -> 대상 CPU의 두 번째 XDP,
# AF_XDP CSV / UDP binary -> 각 사용자 수신 프로그램의 관측이라는 대응이다.
# 패킷에는 run_id를 싣지 않는다. manifest의 run 디렉터리가 실험 경계를 정하고,
# 그 안에서만 (flow_id, sequence)가 동일한 패킷을 뜻한다.
# 따라서 서로 다른 run의 sequence가 다시 0부터 시작해도 여기서 섞이지 않는다.
Key = tuple[int, int]

MANIFEST_VERSION = 1
SUMMARY_SCHEMA_VERSION = 3
COMPLETION_SCHEMA_VERSION = 2

UDP_RECORD = struct.Struct("@QqqqqIIHHI")

ACE_PATH_PASS = 0
ACE_PATH_CPUMAP = 1
ACE_PATH_XSK = 2
VALID_REQUESTED_PATHS = {ACE_PATH_PASS, ACE_PATH_CPUMAP, ACE_PATH_XSK}

META_HWTS_VALID = 1 << 0
META_PACKET_ID_VALID = 1 << 1
META_CPUMAP_SEEN = 1 << 2
META_TIMESTAMP_ERROR = 1 << 3
META_PATH_REQUESTED = 1 << 4
META_KNOWN_FLAGS = (
    META_HWTS_VALID
    | META_PACKET_ID_VALID
    | META_CPUMAP_SEEN
    | META_TIMESTAMP_ERROR
    | META_PATH_REQUESTED
)
META_INGRESS_REQUIRED = META_PACKET_ID_VALID | META_PATH_REQUESTED
CPUMAP_FLAGS_REQUIRED = (
    META_PACKET_ID_VALID | META_CPUMAP_SEEN | META_PATH_REQUESTED
)
AF_XDP_VALIDATION_REQUIRED = (1 << 0) | (1 << 1) | (1 << 2)

UDP_HWTS_VALID = META_HWTS_VALID
UDP_CMSG_TRUNCATED = 1 << 1
UDP_KNOWN_FLAGS = UDP_HWTS_VALID | UDP_CMSG_TRUNCATED

INCONSISTENT_INGRESS_PATH = 1 << 0
INCONSISTENT_CPUMAP_INGRESS_PATH = 1 << 1
INCONSISTENT_CPUMAP_PATH = 1 << 2
INCONSISTENT_CPUMAP_FLAGS = 1 << 3
INCONSISTENT_UDP_PATH = 1 << 4
INCONSISTENT_AF_XDP_PATH = 1 << 5
INCONSISTENT_AF_XDP_METADATA = 1 << 6
INCONSISTENT_AMBIGUOUS_USER = 1 << 7
INCONSISTENT_INGRESS_FLAGS = 1 << 8
INCONSISTENT_UDP_FLAGS = 1 << 9

INCONSISTENCY_BITS = {
    "ingress_path": INCONSISTENT_INGRESS_PATH,
    "cpumap_ingress_path": INCONSISTENT_CPUMAP_INGRESS_PATH,
    "cpumap_path": INCONSISTENT_CPUMAP_PATH,
    "cpumap_flags": INCONSISTENT_CPUMAP_FLAGS,
    "udp_path": INCONSISTENT_UDP_PATH,
    "afxdp_path": INCONSISTENT_AF_XDP_PATH,
    "afxdp_metadata": INCONSISTENT_AF_XDP_METADATA,
    "ambiguous_user": INCONSISTENT_AMBIGUOUS_USER,
    "ingress_flags": INCONSISTENT_INGRESS_FLAGS,
    "udp_flags": INCONSISTENT_UDP_FLAGS,
}


class InputError(ValueError):
    """An input file or argument violates the recorded ABI or run boundary."""


@dataclass(frozen=True)
class SourceTable:
    records: dict[Key, dict[str, int]]
    rows: int
    duplicate_rows: int
    conflicting_duplicate_rows: int


@dataclass(frozen=True)
class CalibrationPoint:
    monotonic_ns: int
    offset_ns: int


@dataclass(frozen=True)
class CalibrationFile:
    path: Path
    rows: int
    selected_rows: int


@dataclass(frozen=True)
class CalibrationLookup:
    offset_ns: int | None
    position: str
    out_of_range: bool
    distance_ns: int


@dataclass(frozen=True)
class Calibration:
    points: tuple[CalibrationPoint, ...]
    times: tuple[int, ...]
    files: tuple[CalibrationFile, ...]
    rows: int
    selected_rows: int
    duplicate_selected_times: int

    def lookup(
        self, monotonic_ns: int, allow_endpoint_clamp: bool = False
    ) -> CalibrationLookup:
        """Locate/interpolate the PHC-minus-MONOTONIC offset."""

        # PHC와 MONOTONIC의 차이는 실행 중 조금씩 변할 수 있으므로,
        # 관측 시각을 둘러싼 보정점 두 개 사이에서 offset을 선형 보간한다.
        # 측정 범위 밖에는 두 보정점이 없으므로 기본값은 변환 불가(None)다.
        # endpoint-clamp를 명시했을 때만 가장 가까운 끝점의 offset을 쓴다.
        # 보간은 두 시계를 연결하는 추정이며 HW timestamp 자체를 다시 재는 일은 아니다.
        right = bisect.bisect_right(self.times, monotonic_ns)
        if right == 0:
            first = self.points[0]
            distance = first.monotonic_ns - monotonic_ns
            offset = first.offset_ns if allow_endpoint_clamp else None
            return CalibrationLookup(offset, "before", True, distance)
        if right == len(self.points):
            last = self.points[-1]
            if monotonic_ns == last.monotonic_ns:
                return CalibrationLookup(last.offset_ns, "exact", False, 0)
            distance = monotonic_ns - last.monotonic_ns
            offset = last.offset_ns if allow_endpoint_clamp else None
            return CalibrationLookup(offset, "after", True, distance)

        left = self.points[right - 1]
        if monotonic_ns == left.monotonic_ns:
            return CalibrationLookup(left.offset_ns, "exact", False, 0)
        following = self.points[right]
        span = following.monotonic_ns - left.monotonic_ns
        numerator = (
            left.offset_ns * span
            + (following.offset_ns - left.offset_ns)
            * (monotonic_ns - left.monotonic_ns)
        )
        return CalibrationLookup(
            round_ratio(numerator, span), "inside", False, 0
        )

    def offset_at(
        self, monotonic_ns: int, allow_endpoint_clamp: bool = False
    ) -> int | None:
        return self.lookup(monotonic_ns, allow_endpoint_clamp).offset_ns


@dataclass(frozen=True)
class Manifest:
    path: Path
    run_directory: Path
    version: int
    run_id: str


def round_ratio(numerator: int, denominator: int) -> int:
    """Round an integer ratio to nearest, with exact halves away from zero."""

    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator >= 0:
        return (2 * numerator + denominator) // (2 * denominator)
    return -((2 * -numerator + denominator) // (2 * denominator))


def data_lines(file: TextIO) -> Iterable[str]:
    for line in file:
        if line.strip() and not line.lstrip().startswith("#"):
            yield line


def parse_integer(
    value: str | None, path: Path, row_number: int, field: str
) -> int:
    if value is None or value.strip() == "":
        raise InputError(f"{path}: row {row_number}: missing {field}")
    try:
        return int(value, 10)
    except ValueError as error:
        raise InputError(
            f"{path}: row {row_number}: invalid integer {field}={value!r}"
        ) from error


def validate_key(
    path: Path, row_number: int, flow_id: int, sequence: int
) -> None:
    if not 0 <= flow_id <= 0xFFFFFFFF:
        raise InputError(f"{path}: row {row_number}: flow_id outside uint32")
    if not 0 <= sequence <= 0xFFFFFFFFFFFFFFFF:
        raise InputError(f"{path}: row {row_number}: sequence outside uint64")


CSV_FIELD_RANGES = {
    "flow_id": (0, 0xFFFFFFFF, "uint32"),
    "sequence": (0, 0xFFFFFFFFFFFFFFFF, "uint64"),
    "rx_queue": (0, 0xFFFFFFFF, "uint32"),
    "cpu": (0, 0xFFFFFFFF, "uint32"),
    "hw_rx_ns": (0, 0xFFFFFFFFFFFFFFFF, "uint64"),
    "initial_xdp_ns": (0, 0xFFFFFFFFFFFFFFFF, "uint64"),
    "cpumap_ns": (0, 0xFFFFFFFFFFFFFFFF, "uint64"),
    "user_rx_mono_ns": (0, 0xFFFFFFFFFFFFFFFF, "uint64"),
    "tx_realtime_ns": (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF, "int64"),
    "user_rx_real_ns": (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF, "int64"),
    "flags": (0, 0xFFFFFFFF, "uint32"),
    "meta_flags": (0, 0xFFFFFFFF, "uint32"),
    "validation_flags": (0, 0xFFFFFFFF, "uint32"),
    "requested_path": (0, 0xFFFF, "uint16"),
    "timestamp_error": (-0x8000, 0x7FFF, "int16"),
    "packet_length": (0, 0xFFFFFFFF, "uint32"),
}


def validate_csv_ranges(
    record: Mapping[str, int], path: Path, row_number: int
) -> None:
    for field, (minimum, maximum, type_name) in CSV_FIELD_RANGES.items():
        field_value = record.get(field)
        if field_value is not None and not minimum <= field_value <= maximum:
            raise InputError(
                f"{path}: row {row_number}: {field} outside {type_name}"
            )


def validate_csv_header(
    reader: csv.DictReader[str], path: Path, required_fields: set[str]
) -> None:
    if reader.fieldnames is None:
        raise InputError(f"{path}: CSV header is missing")
    if any(name is None or name == "" for name in reader.fieldnames):
        raise InputError(f"{path}: empty CSV column name")
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise InputError(f"{path}: duplicate CSV column name")
    missing_fields = required_fields.difference(reader.fieldnames)
    if missing_fields:
        missing = ", ".join(sorted(missing_fields))
        raise InputError(f"{path}: missing CSV columns: {missing}")


def load_csv_table(path: Path, required_fields: set[str]) -> SourceTable:
    records: dict[Key, dict[str, int]] = {}
    rows = 0
    duplicates = 0
    conflicts = 0

    try:
        file = path.open("r", encoding="utf-8", newline="")
    except OSError as error:
        raise InputError(f"cannot open {path}: {error.strerror}") from error

    with file:
        reader = csv.DictReader(data_lines(file))
        validate_csv_header(reader, path, required_fields)
        for row_number, raw_record in enumerate(reader, start=2):
            if None in raw_record:
                raise InputError(f"{path}: row {row_number}: extra CSV fields")
            rows += 1
            record = {
                field: parse_integer(raw_record.get(field), path, row_number, field)
                for field in reader.fieldnames or ()
            }
            validate_csv_ranges(record, path, row_number)
            flow_id = record["flow_id"]
            sequence = record["sequence"]
            validate_key(path, row_number, flow_id, sequence)
            key = (flow_id, sequence)
            previous = records.get(key)
            if previous is not None:
                duplicates += 1
                if previous != record:
                    conflicts += 1
                continue
            records[key] = record

    return SourceTable(records, rows, duplicates, conflicts)


def load_udp_records(path: Path) -> SourceTable:
    # receiver.c가 남긴 sample_record를 읽는다. wire packet의 32-byte header와
    # 이 56-byte 측정 파일 형식은 서로 다르다. 전자는 송수신 공용 식별 정보이고,
    # 후자는 수신기가 추가로 측정한 user/HW 시각까지 담은 로컬 기록이다.
    try:
        contents = path.read_bytes()
    except OSError as error:
        raise InputError(f"cannot open {path}: {error.strerror}") from error
    if len(contents) % UDP_RECORD.size:
        raise InputError(
            f"{path}: size {len(contents)} is not a multiple of "
            f"{UDP_RECORD.size} bytes"
        )

    records: dict[Key, dict[str, int]] = {}
    rows = len(contents) // UDP_RECORD.size
    duplicates = 0
    conflicts = 0
    for index, unpacked in enumerate(UDP_RECORD.iter_unpack(contents), start=1):
        (
            sequence,
            tx_ns,
            user_mono_ns,
            user_real_ns,
            hw_ns,
            flow_id,
            length,
            flags,
            reserved16,
            reserved32,
        ) = unpacked
        validate_key(path, index, flow_id, sequence)
        if reserved16 != 0 or reserved32 != 0:
            raise InputError(f"{path}: record {index}: reserved field is nonzero")
        record = {
            "flow_id": flow_id,
            "sequence": sequence,
            "tx_realtime_ns": tx_ns,
            "user_rx_mono_ns": user_mono_ns,
            "user_rx_real_ns": user_real_ns,
            "hw_rx_ns": hw_ns,
            "packet_length": length,
            "flags": flags,
        }
        key = (flow_id, sequence)
        previous = records.get(key)
        if previous is not None:
            duplicates += 1
            if previous != record:
                conflicts += 1
            continue
        records[key] = record

    return SourceTable(records, rows, duplicates, conflicts)


CALIBRATION_FIELDS = {
    "iteration",
    "sample",
    "system_before_ns",
    "phc_ns",
    "system_after_ns",
    "span_ns",
    "offset_ns",
    "selected",
}


def load_calibration_file(
    path: Path,
) -> tuple[CalibrationFile, list[CalibrationPoint]]:
    selected: list[CalibrationPoint] = []
    rows = 0
    try:
        file = path.open("r", encoding="utf-8", newline="")
    except OSError as error:
        raise InputError(f"cannot open {path}: {error.strerror}") from error

    with file:
        reader = csv.DictReader(data_lines(file))
        validate_csv_header(reader, path, CALIBRATION_FIELDS)
        for row_number, raw_record in enumerate(reader, start=2):
            if None in raw_record:
                raise InputError(f"{path}: row {row_number}: extra CSV fields")
            rows += 1
            values = {
                field: parse_integer(raw_record.get(field), path, row_number, field)
                for field in CALIBRATION_FIELDS
            }
            before = values["system_before_ns"]
            after = values["system_after_ns"]
            span = values["span_ns"]
            if values["iteration"] < 0 or values["sample"] < 0:
                raise InputError(
                    f"{path}: row {row_number}: negative iteration or sample"
                )
            if values["iteration"] > 0xFFFFFFFF or values["sample"] > 0xFFFFFFFF:
                raise InputError(
                    f"{path}: row {row_number}: iteration or sample outside uint32"
                )
            for field in (
                "system_before_ns", "phc_ns", "system_after_ns",
                "span_ns", "offset_ns",
            ):
                if not -0x8000000000000000 <= values[field] <= 0x7FFFFFFFFFFFFFFF:
                    raise InputError(
                        f"{path}: row {row_number}: {field} outside int64"
                    )
            if before < 0 or after < before or span != after - before:
                raise InputError(
                    f"{path}: row {row_number}: inconsistent system span"
                )
            # 보정 도구는 MONOTONIC -> PHC -> MONOTONIC 순서로 시계를 읽는다.
            # 두 MONOTONIC 시각의 중간을 PHC를 읽은 시각으로 근사하므로
            # offset의 부호는 항상 PHC - MONOTONIC이다.
            midpoint = before + span // 2
            expected_offset = values["phc_ns"] - midpoint
            if values["offset_ns"] != expected_offset:
                raise InputError(
                    f"{path}: row {row_number}: inconsistent PHC offset"
                )
            if values["selected"] not in (0, 1):
                raise InputError(
                    f"{path}: row {row_number}: selected must be 0 or 1"
                )
            if values["selected"]:
                selected.append(CalibrationPoint(midpoint, expected_offset))

    if not selected:
        raise InputError(f"{path}: no selected calibration samples")
    return CalibrationFile(path, rows, len(selected)), selected


def load_calibration(paths: Path | Sequence[Path]) -> Calibration:
    """Load and merge one or more calibration captures."""

    # 실험 전/후 보정 파일을 함께 넣으면 하나의 시간축으로 연결된다.
    # 각 iteration에서 보정 도구가 selected로 표시한 표본만 보간점으로 쓰며,
    # 파일을 준 순서가 아니라 MONOTONIC 시각 순서로 정렬한다.
    path_list = [paths] if isinstance(paths, Path) else list(paths)
    if not path_list:
        raise InputError("at least one PHC calibration file is required")

    files: list[CalibrationFile] = []
    all_selected: list[tuple[CalibrationPoint, Path]] = []
    for path in path_list:
        calibration_file, selected = load_calibration_file(path)
        files.append(calibration_file)
        all_selected.extend((point, path) for point in selected)

    all_selected.sort(key=lambda item: item[0].monotonic_ns)
    unique: list[CalibrationPoint] = []
    point_sources: list[Path] = []
    duplicate_times = 0
    for point, source in all_selected:
        if unique and point.monotonic_ns == unique[-1].monotonic_ns:
            duplicate_times += 1
            if point.offset_ns != unique[-1].offset_ns:
                raise InputError(
                    "conflicting selected PHC offsets at MONOTONIC "
                    f"{point.monotonic_ns}: {point_sources[-1]} and {source}"
                )
            continue
        unique.append(point)
        point_sources.append(source)

    points = tuple(unique)
    return Calibration(
        points=points,
        times=tuple(point.monotonic_ns for point in points),
        files=tuple(files),
        rows=sum(item.rows for item in files),
        selected_rows=sum(item.selected_rows for item in files),
        duplicate_selected_times=duplicate_times,
    )


def empty_table() -> SourceTable:
    return SourceTable({}, 0, 0, 0)


JOINED_FIELDS = (
    "flow_id", "sequence", "ingress_present", "cpumap_present",
    "afxdp_present", "udp_present", "user_source", "requested_path",
    "ingress_rx_queue", "cpumap_cpu", "afxdp_rx_queue", "packet_length",
    "tx_realtime_ns", "user_rx_mono_ns", "user_rx_real_ns",
    "ingress_hw_rx_ns", "afxdp_hw_rx_ns", "udp_hw_rx_ns",
    "ingress_initial_xdp_ns", "afxdp_initial_xdp_ns", "initial_xdp_ns",
    "initial_xdp_source", "cpumap_ns", "calibration_offset_at_xdp_ns",
    "calibration_xdp_position", "calibration_xdp_out_of_range",
    "calibration_xdp_distance_ns", "calibration_offset_at_user_ns",
    "calibration_user_position", "calibration_user_out_of_range",
    "calibration_user_distance_ns", "xdp_hw_ns", "cpumap_xdp_ns",
    "user_cpumap_ns", "user_xdp_ns", "user_hw_ns", "ingress_flags",
    "ingress_requested_path", "ingress_timestamp_error", "cpumap_flags",
    "cpumap_requested_path", "afxdp_meta_flags", "afxdp_requested_path",
    "afxdp_validation_flags", "afxdp_timestamp_error", "udp_flags",
    "afxdp_tx_realtime_ns", "afxdp_user_rx_mono_ns",
    "afxdp_user_rx_real_ns", "afxdp_packet_length", "udp_tx_realtime_ns",
    "udp_user_rx_mono_ns", "udp_user_rx_real_ns", "udp_packet_length",
    "inconsistency_flags",
)

METRIC_FIELDS = {
    "xdp-hw": "xdp_hw_ns",
    "cpumap-xdp": "cpumap_xdp_ns",
    "user-cpumap": "user_cpumap_ns",
    "user-xdp": "user_xdp_ns",
    "user-hw": "user_hw_ns",
}


def value(record: Mapping[str, int] | None, name: str) -> int | None:
    return None if record is None else record.get(name)


def positive_value(record: Mapping[str, int] | None, field: str) -> int | None:
    result = value(record, field)
    return result if result is not None and result > 0 else None


def positive_u64_value(
    record: Mapping[str, int] | None, field: str
) -> int | None:
    result = value(record, field)
    return result if result is not None and 0 < result <= 0xFFFFFFFFFFFFFFFF else None


def metadata_timestamp_coherent(record: Mapping[str, int]) -> bool:
    flags = record["flags"] if "flags" in record else record["meta_flags"]
    hw_ns = record["hw_rx_ns"]
    error = record["timestamp_error"]
    if (
        flags < 0
        or flags > 0xFFFFFFFF
        or flags & ~META_KNOWN_FLAGS
        or hw_ns < 0
        or hw_ns > 0xFFFFFFFFFFFFFFFF
        or error < -0x8000
        or error > 0x7FFF
    ):
        return False
    hw_valid = bool(flags & META_HWTS_VALID)
    error_flag = bool(flags & META_TIMESTAMP_ERROR)
    if error_flag != (error != 0):
        return False
    if hw_valid:
        return hw_ns > 0 and not error_flag
    return hw_ns == 0


def lookup_fields(
    lookup: CalibrationLookup | None, prefix: str
) -> dict[str, int | str | None]:
    if lookup is None:
        return {
            f"calibration_offset_at_{prefix}_ns": None,
            f"calibration_{prefix}_position": "",
            f"calibration_{prefix}_out_of_range": None,
            f"calibration_{prefix}_distance_ns": None,
        }
    return {
        f"calibration_offset_at_{prefix}_ns": lookup.offset_ns,
        f"calibration_{prefix}_position": lookup.position,
        f"calibration_{prefix}_out_of_range": int(lookup.out_of_range),
        f"calibration_{prefix}_distance_ns": lookup.distance_ns,
    }


def joined_row(
    key: Key,
    ingress: Mapping[str, int] | None,
    cpumap: Mapping[str, int] | None,
    afxdp: Mapping[str, int] | None,
    udp: Mapping[str, int] | None,
    calibration: Calibration,
    allow_endpoint_clamp: bool = False,
) -> dict[str, int | str | None]:
    """Build one processed row while retaining every source's raw values."""

    # 한 행은 동일한 패킷의 관측 지점들을 모은 것이다. 모든 지점이 필수는 아니다.
    # PASS에는 CPUMAP 기록이 없고, XSK 경로에는 일반 UDP 수신 기록이 없다.
    # flags/path는 해당 timestamp가 어느 경로에서 생겼는지 판단하는 데 사용한다.
    # 관측이 없는 지연 성분은 0으로 채우지 않고 None으로 남긴다.
    if afxdp is not None and udp is not None:
        user_source = "ambiguous"
        user = None
    elif afxdp is not None:
        user_source = "afxdp"
        user = afxdp
    elif udp is not None:
        user_source = "udp"
        user = udp
    else:
        user_source = ""
        user = None

    inconsistencies = 0
    ingress_path = value(ingress, "requested_path")
    cpumap_path = value(cpumap, "requested_path")
    afxdp_path = value(afxdp, "requested_path")

    ingress_core_valid = False
    ingress_hw_valid = False
    if ingress is not None:
        ingress_flags = ingress["flags"]
        if ingress_path not in VALID_REQUESTED_PATHS:
            inconsistencies |= INCONSISTENT_INGRESS_PATH
        ingress_core_valid = (
            ingress_path in VALID_REQUESTED_PATHS
            and ingress_flags & META_INGRESS_REQUIRED == META_INGRESS_REQUIRED
            and not (ingress_flags & META_CPUMAP_SEEN)
            and not (ingress_flags & ~META_KNOWN_FLAGS)
            and 0 <= ingress_flags <= 0xFFFFFFFF
            and positive_u64_value(ingress, "initial_xdp_ns") is not None
        )
        if not ingress_core_valid or not metadata_timestamp_coherent(ingress):
            inconsistencies |= INCONSISTENT_INGRESS_FLAGS
        ingress_hw_valid = (
            ingress_core_valid
            and metadata_timestamp_coherent(ingress)
            and bool(ingress_flags & META_HWTS_VALID)
        )

    cpumap_valid = False
    if cpumap is not None:
        cpumap_flags = cpumap["flags"]
        if cpumap_path != ACE_PATH_CPUMAP:
            inconsistencies |= INCONSISTENT_CPUMAP_PATH
        if (
            cpumap_flags & CPUMAP_FLAGS_REQUIRED != CPUMAP_FLAGS_REQUIRED
            or cpumap_flags < 0
            or cpumap_flags > 0xFFFFFFFF
            or cpumap_flags & ~META_KNOWN_FLAGS
        ):
            inconsistencies |= INCONSISTENT_CPUMAP_FLAGS
        if ingress is not None and (
            ingress_path != ACE_PATH_CPUMAP or cpumap_path != ingress_path
        ):
            inconsistencies |= INCONSISTENT_CPUMAP_INGRESS_PATH
        cpumap_valid = (
            cpumap_path == ACE_PATH_CPUMAP
            and cpumap_flags & CPUMAP_FLAGS_REQUIRED == CPUMAP_FLAGS_REQUIRED
            and 0 <= cpumap_flags <= 0xFFFFFFFF
            and not (cpumap_flags & ~META_KNOWN_FLAGS)
            and positive_u64_value(cpumap, "cpumap_ns") is not None
            and (
                ingress is None
                or (
                    ingress_core_valid
                    and ingress_path == ACE_PATH_CPUMAP
                    and cpumap_path == ingress_path
                )
            )
        )

    afxdp_core_valid = False
    afxdp_metadata_consistent = False
    afxdp_hw_valid = False
    if afxdp is not None:
        meta_flags = afxdp["meta_flags"]
        validation_flags = afxdp["validation_flags"]
        if afxdp_path != ACE_PATH_XSK or (
            ingress is not None and ingress_path != ACE_PATH_XSK
        ):
            inconsistencies |= INCONSISTENT_AF_XDP_PATH
        afxdp_core_valid = (
            validation_flags == AF_XDP_VALIDATION_REQUIRED
            and meta_flags & META_INGRESS_REQUIRED == META_INGRESS_REQUIRED
            and not (meta_flags & META_CPUMAP_SEEN)
            and 0 <= meta_flags <= 0xFFFFFFFF
            and not (meta_flags & ~META_KNOWN_FLAGS)
            and afxdp_path == ACE_PATH_XSK
            and positive_u64_value(afxdp, "initial_xdp_ns") is not None
            and positive_value(afxdp, "user_rx_mono_ns") is not None
            and metadata_timestamp_coherent(afxdp)
        )
        afxdp_metadata_consistent = afxdp_core_valid
        if afxdp_core_valid and ingress is not None:
            afxdp_metadata_consistent = all(
                value(afxdp, afxdp_field) == value(ingress, ingress_field)
                for afxdp_field, ingress_field in (
                    ("rx_queue", "rx_queue"),
                    ("hw_rx_ns", "hw_rx_ns"),
                    ("initial_xdp_ns", "initial_xdp_ns"),
                    ("meta_flags", "flags"),
                    ("requested_path", "requested_path"),
                    ("timestamp_error", "timestamp_error"),
                )
            )
        if not afxdp_metadata_consistent:
            inconsistencies |= INCONSISTENT_AF_XDP_METADATA
        afxdp_hw_valid = afxdp_core_valid and bool(meta_flags & META_HWTS_VALID)

    udp_valid = False
    udp_hw_valid = False
    if udp is not None:
        udp_flags = udp["flags"]
        if ingress is not None and ingress_path == ACE_PATH_XSK:
            inconsistencies |= INCONSISTENT_UDP_PATH
        udp_hw_valid = bool(udp_flags & UDP_HWTS_VALID) and (
            positive_value(udp, "hw_rx_ns") is not None
        ) and not (udp_flags & UDP_CMSG_TRUNCATED)
        udp_valid = (
            positive_value(udp, "user_rx_mono_ns") is not None
            and not (udp_flags & ~UDP_KNOWN_FLAGS)
            and not (
                udp_flags & UDP_HWTS_VALID
                and udp_flags & UDP_CMSG_TRUNCATED
            )
            and (bool(udp_flags & UDP_HWTS_VALID) == (udp["hw_rx_ns"] > 0))
        )
        if (
            udp_hw_valid
            and ingress_hw_valid
            and value(udp, "hw_rx_ns") != value(ingress, "hw_rx_ns")
        ):
            udp_valid = False
        if not udp_valid:
            inconsistencies |= INCONSISTENT_UDP_FLAGS

    if user_source == "ambiguous":
        inconsistencies |= INCONSISTENT_AMBIGUOUS_USER

    raw_xdp_mono = positive_u64_value(ingress, "initial_xdp_ns")
    if ingress is None:
        raw_xdp_mono = positive_u64_value(afxdp, "initial_xdp_ns")

    # AF_XDP metadata에는 최초 XDP 시각도 복사되어 있다. ingress map 기록이
    # 없더라도 유효한 AF_XDP metadata가 있으면 그 시각을 이어 쓸 수 있다.
    # initial_xdp_source를 함께 남겨 실제 사용한 기록 출처를 구분한다.
    initial_xdp: int | None = None
    initial_xdp_source = ""
    if ingress is not None and ingress_core_valid:
        initial_xdp = positive_u64_value(ingress, "initial_xdp_ns")
        initial_xdp_source = "ingress"
    elif ingress is None and afxdp_core_valid:
        initial_xdp = positive_u64_value(afxdp, "initial_xdp_ns")
        initial_xdp_source = "afxdp"

    # t_user는 수신 프로그램의 관측 경계다. UDP는 recvmsg 반환 직후,
    # AF_XDP는 각 RX descriptor의 패킷을 파싱하기 직전에 시각을 읽는다.
    # NIC 도착이나 업무 처리 완료 시각이 아니므로 지연 해석에서도 구분한다.
    cpumap_ns = positive_u64_value(cpumap, "cpumap_ns")
    user_mono = positive_value(user, "user_rx_mono_ns")
    xdp_lookup = (
        calibration.lookup(raw_xdp_mono, allow_endpoint_clamp)
        if raw_xdp_mono is not None
        else None
    )
    user_lookup = (
        calibration.lookup(user_mono, allow_endpoint_clamp)
        if user_mono is not None
        else None
    )

    xdp_hw: int | None = None
    if ingress is not None and ingress_hw_valid:
        xdp_hw = positive_value(ingress, "hw_rx_ns")
    elif ingress is None and afxdp_hw_valid:
        xdp_hw = positive_value(afxdp, "hw_rx_ns")

    user_semantically_valid = False
    user_hw: int | None = None
    if user_source == "afxdp":
        user_semantically_valid = (
            afxdp_metadata_consistent
            and not (inconsistencies & INCONSISTENT_AF_XDP_PATH)
        )
        if ingress is not None and ingress_hw_valid:
            user_hw = positive_value(ingress, "hw_rx_ns")
        elif ingress is None and afxdp_hw_valid:
            user_hw = positive_value(afxdp, "hw_rx_ns")
    elif user_source == "udp":
        user_semantically_valid = (
            udp_valid and not (inconsistencies & INCONSISTENT_UDP_PATH)
        )
        if udp_hw_valid:
            user_hw = positive_value(udp, "hw_rx_ns")
        elif ingress_hw_valid:
            user_hw = positive_value(ingress, "hw_rx_ns")

    # HW RX는 PHC, XDP/CPUMAP/user_mono는 같은 Pi의 MONOTONIC 계열이다.
    # 서로 다른 기준 시각끼리 바로 빼면 시계 offset이 지연에 섞여 들어간다.
    # PHC -> MONOTONIC 변환은 hw - offset이므로 아래 계산은
    # mono - (hw - offset) == mono + offset - hw로 표현한다.
    # 송신자의 tx_realtime_ns는 원본으로 보존할 뿐 이 로컬 지연식에 넣지 않는다.
    xdp_offset = None if xdp_lookup is None else xdp_lookup.offset_ns
    user_offset = None if user_lookup is None else user_lookup.offset_ns
    xdp_hw_metric = (
        initial_xdp + xdp_offset - xdp_hw
        if initial_xdp is not None
        and xdp_offset is not None
        and xdp_hw is not None
        else None
    )
    # CPUMAP-XDP는 최초 분류 이후 대상 CPU의 CPUMAP 프로그램까지 걸린 시간이다.
    # queue 대기와 CPU 간 전달이 포함되므로 CPUMAP 함수 실행 시간만 뜻하지 않는다.
    # user-XDP는 최초 XDP부터 사용자 관측까지, user-CPUMAP은 두 번째 XDP부터
    # 사용자 관측까지다. 이 세 성분은 같은 시계라 PHC 보정 없이 계산한다.
    cpumap_xdp_metric = (
        cpumap_ns - initial_xdp
        if cpumap_valid and cpumap_ns is not None and initial_xdp is not None
        else None
    )
    user_xdp_metric = (
        user_mono - initial_xdp
        if user_semantically_valid
        and user_mono is not None
        and initial_xdp is not None
        else None
    )
    user_cpumap_metric = (
        user_mono - cpumap_ns
        if user_semantically_valid
        and cpumap_valid
        and user_mono is not None
        and cpumap_ns is not None
        else None
    )
    user_hw_metric = (
        user_mono + user_offset - user_hw
        if user_semantically_valid
        and user_mono is not None
        and user_offset is not None
        and user_hw is not None
        else None
    )

    requested_path = ingress_path
    if requested_path is None:
        requested_path = afxdp_path
    if requested_path is None:
        requested_path = cpumap_path

    # 계산에 선택한 값과 별개로 각 source의 원시 timestamp도 모두 남긴다.
    # 나중에 보정 방식이나 관측 출처를 확인할 때 joined CSV만으로 비교할 수 있다.
    # present 열은 기록의 존재, metric 열의 빈칸은 그 지연 성분의 계산 불가를 뜻한다.
    row: dict[str, int | str | None] = {
        "flow_id": key[0],
        "sequence": key[1],
        "ingress_present": int(ingress is not None),
        "cpumap_present": int(cpumap is not None),
        "afxdp_present": int(afxdp is not None),
        "udp_present": int(udp is not None),
        "user_source": user_source,
        "requested_path": requested_path,
        "ingress_rx_queue": value(ingress, "rx_queue"),
        "cpumap_cpu": value(cpumap, "cpu"),
        "afxdp_rx_queue": value(afxdp, "rx_queue"),
        "packet_length": value(user, "packet_length"),
        "tx_realtime_ns": value(user, "tx_realtime_ns"),
        "user_rx_mono_ns": value(user, "user_rx_mono_ns"),
        "user_rx_real_ns": value(user, "user_rx_real_ns"),
        "ingress_hw_rx_ns": value(ingress, "hw_rx_ns"),
        "afxdp_hw_rx_ns": value(afxdp, "hw_rx_ns"),
        "udp_hw_rx_ns": value(udp, "hw_rx_ns"),
        "ingress_initial_xdp_ns": value(ingress, "initial_xdp_ns"),
        "afxdp_initial_xdp_ns": value(afxdp, "initial_xdp_ns"),
        "initial_xdp_ns": initial_xdp,
        "initial_xdp_source": initial_xdp_source,
        "cpumap_ns": value(cpumap, "cpumap_ns"),
        "xdp_hw_ns": xdp_hw_metric,
        "cpumap_xdp_ns": cpumap_xdp_metric,
        "user_cpumap_ns": user_cpumap_metric,
        "user_xdp_ns": user_xdp_metric,
        "user_hw_ns": user_hw_metric,
        "ingress_flags": value(ingress, "flags"),
        "ingress_requested_path": ingress_path,
        "ingress_timestamp_error": value(ingress, "timestamp_error"),
        "cpumap_flags": value(cpumap, "flags"),
        "cpumap_requested_path": cpumap_path,
        "afxdp_meta_flags": value(afxdp, "meta_flags"),
        "afxdp_requested_path": afxdp_path,
        "afxdp_validation_flags": value(afxdp, "validation_flags"),
        "afxdp_timestamp_error": value(afxdp, "timestamp_error"),
        "udp_flags": value(udp, "flags"),
        "afxdp_tx_realtime_ns": value(afxdp, "tx_realtime_ns"),
        "afxdp_user_rx_mono_ns": value(afxdp, "user_rx_mono_ns"),
        "afxdp_user_rx_real_ns": value(afxdp, "user_rx_real_ns"),
        "afxdp_packet_length": value(afxdp, "packet_length"),
        "udp_tx_realtime_ns": value(udp, "tx_realtime_ns"),
        "udp_user_rx_mono_ns": value(udp, "user_rx_mono_ns"),
        "udp_user_rx_real_ns": value(udp, "user_rx_real_ns"),
        "udp_packet_length": value(udp, "packet_length"),
        "inconsistency_flags": inconsistencies,
    }
    row.update(lookup_fields(xdp_lookup, "xdp"))
    row.update(lookup_fields(user_lookup, "user"))
    return row


def percentile_nearest_rank(sorted_values: Sequence[int], percentile: int) -> int:
    index = max(0, math.ceil(percentile * len(sorted_values) / 100) - 1)
    return sorted_values[index]


def summarize_metric(values: Sequence[int], joined_rows: int) -> dict[str, object]:
    # 통계의 표본 수는 계산 가능한 관측 수다. missing을 0ns로 넣으면 분포가
    # 실제보다 좋아 보이므로 개수만 따로 센다. 음수도 삭제하지 않고 표시한다.
    # missing_count는 이 joined 집합에 대한 값이지 송신한 전체 패킷의 손실률은 아니다.
    ordered = sorted(values)
    result: dict[str, object] = {
        "unit": "ns",
        "count": len(ordered),
        "missing_count": joined_rows - len(ordered),
        "negative_count": sum(item < 0 for item in ordered),
        "min_ns": None,
        "max_ns": None,
        "mean_ns": None,
        "median_ns": None,
        "p95_ns": None,
        "p99_ns": None,
    }
    if ordered:
        result.update(
            {
                "min_ns": ordered[0],
                "max_ns": ordered[-1],
                "mean_ns": statistics.fmean(ordered),
                "median_ns": statistics.median(ordered),
                "p95_ns": percentile_nearest_rank(ordered, 95),
                "p99_ns": percentile_nearest_rank(ordered, 99),
            }
        )
    return result


def source_summary(table: SourceTable, provided: bool) -> dict[str, object]:
    return {
        "provided": provided,
        "rows": table.rows,
        "unique_keys": len(table.records),
        "duplicate_rows": table.duplicate_rows,
        "conflicting_duplicate_rows": table.conflicting_duplicate_rows,
    }


def lookup_summary(
    rows: Sequence[Mapping[str, int | str | None]], prefixes: Sequence[str]
) -> dict[str, int | None]:
    positions = [
        str(row[f"calibration_{prefix}_position"])
        for row in rows
        for prefix in prefixes
        if row[f"calibration_{prefix}_position"]
    ]
    distances = [
        cast(int, row[f"calibration_{prefix}_distance_ns"])
        for row in rows
        for prefix in prefixes
        if row[f"calibration_{prefix}_out_of_range"] == 1
    ]
    return {
        "total": len(positions),
        "before": positions.count("before"),
        "inside": positions.count("inside"),
        "exact": positions.count("exact"),
        "after": positions.count("after"),
        "out_of_range": len(distances),
        "max_distance_ns": max(distances) if distances else None,
    }


def build_summary(
    rows: Sequence[Mapping[str, int | str | None]],
    sources: Mapping[str, tuple[SourceTable, bool]],
    calibration: Calibration,
    input_paths: Mapping[str, Path | Sequence[Path] | None],
    input_artifacts: Mapping[str, object],
    manifest: Manifest,
    allow_endpoint_clamp: bool,
    completion_marker: Path,
) -> dict[str, object]:
    joined_count = len(rows)
    metric_values = {
        name: [cast(int, row[field]) for row in rows if row[field] is not None]
        for name, field in METRIC_FIELDS.items()
    }
    duplicate_counts = {
        name: table.duplicate_rows for name, (table, _) in sources.items()
    }
    missing_sources = {
        name: sum(row[f"{name}_present"] == 0 for row in rows)
        for name in ("ingress", "cpumap", "afxdp", "udp")
    }
    missing_sources["user"] = sum(not row["user_source"] for row in rows)
    missing_sources["ambiguous_user"] = sum(
        row["user_source"] == "ambiguous" for row in rows
    )
    inconsistency_counts = {
        name: sum(
            bool(cast(int, row["inconsistency_flags"]) & bit) for row in rows
        )
        for name, bit in INCONSISTENCY_BITS.items()
    }

    serialized_inputs: dict[str, object] = {}
    for name, path in input_paths.items():
        if isinstance(path, Sequence) and not isinstance(path, (str, Path)):
            serialized_inputs[name] = [str(item) for item in path]
        else:
            serialized_inputs[name] = str(path) if path is not None else None

    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "run": {
            "manifest": str(manifest.path),
            "manifest_version": manifest.version,
            "run_directory": str(manifest.run_directory),
            "run_id": manifest.run_id,
        },
        "join_key": ["flow_id", "sequence"],
        "joined_rows": joined_count,
        "inputs": serialized_inputs,
        "input_artifacts": input_artifacts,
        "completion_marker": str(completion_marker),
        "sources": {
            name: source_summary(table, provided)
            for name, (table, provided) in sources.items()
        },
        "calibration": {
            "files": [
                {
                    "path": str(item.path),
                    "rows": item.rows,
                    "selected_rows": item.selected_rows,
                }
                for item in calibration.files
            ],
            "rows": calibration.rows,
            "selected_rows": calibration.selected_rows,
            "unique_selected_times": len(calibration.points),
            "duplicate_selected_times": calibration.duplicate_selected_times,
            "first_monotonic_ns": calibration.points[0].monotonic_ns,
            "last_monotonic_ns": calibration.points[-1].monotonic_ns,
            "outside_range_policy": (
                "endpoint-clamp"
                if allow_endpoint_clamp
                else "exclude-clock-crossing-metrics"
            ),
            "inside_range_policy": "linear-interpolation",
            "lookups": {
                **lookup_summary(rows, ("xdp", "user")),
                "xdp": lookup_summary(rows, ("xdp",)),
                "user": lookup_summary(rows, ("user",)),
            },
        },
        "duplicates": {
            **duplicate_counts,
            "total": sum(duplicate_counts.values()),
        },
        "missing": missing_sources,
        "inconsistency_bit_definitions": INCONSISTENCY_BITS,
        "inconsistencies": {
            **inconsistency_counts,
            "rows": sum(
                bool(cast(int, row["inconsistency_flags"])) for row in rows
            ),
            "total": sum(inconsistency_counts.values()),
        },
        "negative": {
            name: sum(item < 0 for item in values)
            for name, values in metric_values.items()
        },
        "metrics": {
            name: summarize_metric(values, joined_count)
            for name, values in metric_values.items()
        },
    }


def reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value_ in pairs:
        if key in result:
            raise InputError(f"manifest contains duplicate JSON key {key!r}")
        result[key] = value_
    return result


def load_manifest(path: Path) -> Manifest:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise InputError(f"manifest is not a regular file: {path}")
        with resolved.open("r", encoding="utf-8") as file:
            payload = json.load(file, object_pairs_hook=reject_duplicate_json_keys)
    except InputError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InputError(f"cannot read manifest {path}: {error}") from error

    if not isinstance(payload, dict):
        raise InputError(f"{resolved}: manifest root must be a JSON object")
    version = payload.get("manifest_version")
    if type(version) is not int or version != MANIFEST_VERSION:
        raise InputError(
            f"{resolved}: manifest_version must be {MANIFEST_VERSION}"
        )
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id
    ):
        raise InputError(f"{resolved}: invalid run_id")
    return Manifest(resolved, resolved.parent.resolve(strict=True), version, run_id)


def path_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def resolve_input(path: Path, label: str, run_directory: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise InputError(f"cannot resolve {label} {path}: {error}") from error
    if not resolved.is_file():
        raise InputError(f"{label} is not a regular file: {path}")
    if not path_within(resolved, run_directory):
        raise InputError(
            f"{label} escapes manifest run directory {run_directory}: {path}"
        )
    return resolved


def resolve_output(path: Path, label: str, run_directory: Path) -> Path:
    if path.is_symlink():
        raise InputError(f"{label} must not be a symbolic link: {path}")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise InputError(f"cannot resolve parent of {label} {path}: {error}") from error
    if not parent.is_dir():
        raise InputError(f"parent of {label} is not a directory: {path.parent}")
    resolved = path.resolve(strict=False)
    if not path_within(resolved, run_directory):
        raise InputError(
            f"{label} escapes manifest run directory {run_directory}: {path}"
        )
    return resolved


def validate_arguments(args: argparse.Namespace) -> Manifest:
    manifest = load_manifest(args.manifest)
    args.manifest = manifest.path
    args.ingress = resolve_input(args.ingress, "ingress input", manifest.run_directory)
    for attribute, label in (
        ("cpumap", "CPUMAP input"),
        ("afxdp", "AF_XDP input"),
        ("udp_native", "UDP input"),
    ):
        path = getattr(args, attribute)
        if path is not None:
            setattr(args, attribute, resolve_input(path, label, manifest.run_directory))

    args.phc_calibration = [
        resolve_input(path, "PHC calibration input", manifest.run_directory)
        for path in args.phc_calibration
    ]
    args.output_csv = resolve_output(
        args.output_csv, "joined CSV output", manifest.run_directory
    )
    args.summary_json = resolve_output(
        args.summary_json, "summary JSON output", manifest.run_directory
    )
    if args.completion_marker is None:
        args.completion_marker = Path(f"{args.summary_json}.complete")
    args.completion_marker = resolve_output(
        args.completion_marker, "completion marker", manifest.run_directory
    )

    output_paths = {
        args.output_csv,
        args.summary_json,
        args.completion_marker,
    }
    if len(output_paths) != 3:
        raise InputError("joined CSV, summary JSON, and completion paths must differ")
    return manifest


def required_columns() -> dict[str, set[str]]:
    return {
        "ingress": {
            "flow_id", "sequence", "rx_queue", "hw_rx_ns",
            "initial_xdp_ns", "flags", "requested_path", "timestamp_error",
        },
        "cpumap": {
            "flow_id", "sequence", "cpu", "cpumap_ns", "flags",
            "requested_path",
        },
        "afxdp": {
            "flow_id", "sequence", "rx_queue", "tx_realtime_ns",
            "user_rx_mono_ns", "user_rx_real_ns", "hw_rx_ns",
            "initial_xdp_ns", "meta_flags", "requested_path",
            "timestamp_error", "packet_length", "validation_flags",
        },
    }


def analyze(
    args: argparse.Namespace,
) -> tuple[list[dict[str, int | str | None]], dict[str, object]]:
    manifest = validate_arguments(args)
    input_paths: dict[str, Path | Sequence[Path] | None] = {
        "manifest": args.manifest,
        "ingress": args.ingress,
        "cpumap": args.cpumap,
        "afxdp": args.afxdp,
        "udp_native": args.udp_native,
        "phc_calibration": args.phc_calibration,
    }
    columns = required_columns()
    ingress = load_csv_table(args.ingress, columns["ingress"])
    cpumap = (
        load_csv_table(args.cpumap, columns["cpumap"])
        if args.cpumap is not None else empty_table()
    )
    afxdp = (
        load_csv_table(args.afxdp, columns["afxdp"])
        if args.afxdp is not None else empty_table()
    )
    udp = load_udp_records(args.udp_native) if args.udp_native else empty_table()
    calibration = load_calibration(args.phc_calibration)

    sources = {
        "ingress": (ingress, True),
        "cpumap": (cpumap, args.cpumap is not None),
        "afxdp": (afxdp, args.afxdp is not None),
        "udp": (udp, args.udp_native is not None),
    }
    # inner join이면 중간 관측이 빠진 패킷까지 사라져 결과가 편향될 수 있다.
    # 모든 source의 key 합집합을 사용해 한 지점에서만 보인 패킷도 한 행으로 남긴다.
    # 어느 source에도 기록되지 않은 패킷 수는 이 파일들만으로 복원하지 못한다.
    keys: set[Key] = set()
    for table, _ in sources.values():
        keys.update(table.records)

    rows: list[dict[str, int | str | None]] = []
    for key in sorted(keys):
        rows.append(joined_row(
            key, ingress.records.get(key), cpumap.records.get(key),
            afxdp.records.get(key), udp.records.get(key), calibration,
            args.allow_endpoint_clamp,
        ))

    input_artifacts = input_artifact_snapshot(input_paths)
    summary = build_summary(
        rows, sources, calibration, input_paths, input_artifacts, manifest,
        args.allow_endpoint_clamp, args.completion_marker,
    )
    return rows, summary


def reserve_output(path: Path) -> int:
    return os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644
    )


def file_digest(path: Path) -> dict[str, int | str]:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def input_artifact_snapshot(
    input_paths: Mapping[str, Path | Sequence[Path] | None],
) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    for name, value in input_paths.items():
        if value is None:
            snapshot[name] = None
        elif isinstance(value, Sequence) and not isinstance(value, (str, Path)):
            snapshot[name] = [
                {"path": str(path), **file_digest(path)} for path in value
            ]
        else:
            path = cast(Path, value)
            snapshot[name] = {"path": str(path), **file_digest(path)}
    return snapshot


def verify_input_artifacts(snapshot: Mapping[str, object]) -> None:
    for name, raw_specs in snapshot.items():
        if raw_specs is None:
            continue
        specs = raw_specs if isinstance(raw_specs, list) else [raw_specs]
        if not specs:
            raise InputError(f"input artifact list {name} is empty")
        for index, raw_spec in enumerate(specs):
            if not isinstance(raw_spec, Mapping):
                raise InputError(f"invalid input artifact specification for {name}")
            raw_path = raw_spec.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                raise InputError(f"invalid input artifact path for {name}")
            expected = {
                "path": raw_path,
                "bytes": raw_spec.get("bytes"),
                "sha256": raw_spec.get("sha256"),
            }
            actual = {"path": raw_path, **file_digest(Path(raw_path))}
            if actual != expected:
                suffix = f"[{index}]" if isinstance(raw_specs, list) else ""
                raise InputError(f"input artifact changed: {name}{suffix}")


def fsync_directories(paths: Sequence[Path]) -> None:
    for directory in sorted({path.parent for path in paths}, key=str):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def output_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def validate_output_state(
    csv_path: Path, json_path: Path, marker_path: Path, run_id: str
) -> None:
    existing = [
        path for path in (csv_path, json_path, marker_path) if output_exists(path)
    ]
    if not existing:
        return
    if len(existing) == 3:
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            expected_csv = marker["outputs"]["joined_csv"]
            expected_json = marker["outputs"]["summary_json"]
            actual_csv = file_digest(csv_path)
            actual_json = file_digest(json_path)
            complete = (
                marker.get("completion_schema_version") == COMPLETION_SCHEMA_VERSION
                and marker.get("schema_version") == COMPLETION_SCHEMA_VERSION
                and marker.get("artifact_kind") == "ace-joined-run"
                and marker.get("run_id") == run_id
                and expected_csv.get("path") == str(csv_path)
                and expected_json.get("path") == str(json_path)
                and actual_csv["sha256"] == expected_csv["sha256"]
                and actual_csv["bytes"] == expected_csv["bytes"]
                and actual_json["sha256"] == expected_json["sha256"]
                and actual_json["bytes"] == expected_json["bytes"]
            )
        except (
            AttributeError,
            IndexError,
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            complete = False
        if complete:
            raise InputError("refusing to overwrite an already complete output set")
    names = ", ".join(str(path) for path in existing)
    raise InputError(f"incomplete or corrupt prior output set exists: {names}")


def write_outputs(
    csv_path: Path,
    json_path: Path,
    marker_path: Path,
    rows: Sequence[Mapping[str, int | str | None]],
    summary: Mapping[str, object],
    run_id: str,
) -> None:
    validate_output_state(csv_path, json_path, marker_path, run_id)
    raw_inputs = summary.get("input_artifacts")
    if not isinstance(raw_inputs, Mapping):
        raise InputError("summary input_artifacts must be an object")
    verify_input_artifacts(raw_inputs)
    created: list[Path] = []
    descriptor = -1
    try:
        descriptor = reserve_output(csv_path)
        created.append(csv_path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
            descriptor = -1
            writer = csv.DictWriter(
                file, fieldnames=JOINED_FIELDS, extrasaction="raise"
            )
            writer.writeheader()
            writer.writerows(rows)
            file.flush()
            os.fsync(file.fileno())

        descriptor = reserve_output(json_path)
        created.append(json_path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            descriptor = -1
            json.dump(summary, file, indent=2, sort_keys=True, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        fsync_directories((csv_path, json_path))
        # complete 표시는 CSV와 summary의 쓰기가 끝났다는 파일 단위 표시다.
        # 모든 패킷이 모든 경로를 통과했다는 뜻은 아니며 누락 관측은 그대로 남는다.
        # 반복 실험 집계는 이 표시를 입구로 삼아 완성된 분석 결과를 읽는다.
        completion = {
            "schema_version": COMPLETION_SCHEMA_VERSION,
            "completion_schema_version": COMPLETION_SCHEMA_VERSION,
            "artifact_kind": "ace-joined-run",
            "run_id": run_id,
            "summary_schema_version": summary["schema_version"],
            "inputs": raw_inputs,
            "outputs": {
                "joined_csv": {"path": str(csv_path), **file_digest(csv_path)},
                "summary_json": {"path": str(json_path), **file_digest(json_path)},
            },
        }
        descriptor = reserve_output(marker_path)
        created.append(marker_path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            descriptor = -1
            json.dump(completion, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        fsync_directories((marker_path,))
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        for path in reversed(created):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Full-outer-join ACE records on flow_id+sequence and calculate "
            "cross-layer timing metrics"
        )
    )
    parser.add_argument(
        "--manifest", required=True, type=Path,
        help="versioned manifest.json defining the run directory",
    )
    parser.add_argument(
        "--ingress", required=True, type=Path, help="xdp_loader ingress CSV"
    )
    parser.add_argument("--cpumap", type=Path, help="optional CPUMAP CSV")
    parser.add_argument("--afxdp", type=Path, help="optional AF_XDP CSV")
    parser.add_argument(
        "--udp-native", "--udp", dest="udp_native", type=Path,
        help="optional native-endian 56-byte UDP samples",
    )
    parser.add_argument(
        "--phc-calibration", required=True, action="append", type=Path,
        help="repeatable phc_calibrate CSV; selected points are merged",
    )
    parser.add_argument(
        "--allow-endpoint-clamp", action="store_true",
        help="use endpoint offsets outside the calibration bracket",
    )
    parser.add_argument(
        "--output-csv", required=True, type=Path,
        help="new joined CSV (must not already exist)",
    )
    parser.add_argument(
        "--summary-json", required=True, type=Path,
        help="new summary JSON (must not already exist)",
    )
    parser.add_argument(
        "--completion-marker", type=Path,
        help="new completion JSON (default: SUMMARY_JSON.complete)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = argument_parser()
    args = parser.parse_args(argv)
    try:
        rows, summary = analyze(args)
        summary_run = cast(Mapping[str, object], summary["run"])
        run_id = cast(str, summary_run["run_id"])
        write_outputs(
            args.output_csv, args.summary_json, args.completion_marker,
            rows, summary, run_id,
        )
    except (
        InputError, OSError, UnicodeError, csv.Error, struct.error
    ) as error:
        parser.exit(1, f"error: {error}\n")
    print(
        f"joined {len(rows)} packet key(s): {args.output_csv}; "
        f"summary: {args.summary_json}; complete: {args.completion_marker}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
