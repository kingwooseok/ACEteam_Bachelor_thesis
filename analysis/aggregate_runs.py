#!/usr/bin/env python3
"""Aggregate completed ACE joined results across repeated experiment runs.

Only a joiner's completion marker is accepted as a run input.  The marker
cryptographically binds the joined CSV and its summary JSON, while the summary
binds both files to a manifest and run directory.  This keeps accidentally
mixed or half-written artifacts out of deadline statistics.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence, cast


JOIN_COMPLETION_SCHEMA_VERSION = 2
JOIN_SUMMARY_SCHEMA_VERSION = 3
MANIFEST_VERSION = 1
AGGREGATION_SCHEMA_VERSION = 1
AGGREGATION_COMPLETION_SCHEMA_VERSION = 1

METRIC_FIELDS = {
    "xdp-hw": "xdp_hw_ns",
    "cpumap-xdp": "cpumap_xdp_ns",
    "user-cpumap": "user_cpumap_ns",
    "user-xdp": "user_xdp_ns",
    "user-hw": "user_hw_ns",
}

AGGREGATE_FIELDS = (
    "metric",
    "unit",
    "deadline_ns",
    "run_total_count",
    "run_with_samples_count",
    "run_without_samples_count",
    "run_with_deadline_miss_count",
    "run_with_deadline_miss_rate",
    "run_p50_mean_ns",
    "run_p50_ci95_low_ns",
    "run_p50_ci95_high_ns",
    "run_p95_mean_ns",
    "run_p95_ci95_low_ns",
    "run_p95_ci95_high_ns",
    "run_p99_mean_ns",
    "run_p99_ci95_low_ns",
    "run_p99_ci95_high_ns",
    "run_deadline_miss_rate_mean",
    "run_deadline_miss_rate_ci95_low",
    "run_deadline_miss_rate_ci95_high",
    "packet_count",
    "negative_count",
    "p50_ns",
    "p95_ns",
    "p99_ns",
    "max_ns",
    "deadline_miss_count",
    "deadline_miss_rate",
)

RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
INPUT_NAMES = (
    "manifest", "ingress", "cpumap", "afxdp", "udp_native",
    "phc_calibration",
)

# Two-sided 95% Student-t critical values (97.5th percentile), df=1..30.
T_CRITICAL_95 = (
    0.0,
    12.7062047364, 4.30265272975, 3.18244630528, 2.77644510520,
    2.57058183564, 2.44691184879, 2.36462425101, 2.30600413503,
    2.26215716285, 2.22813885196, 2.20098516009, 2.17881282966,
    2.16036865646, 2.14478668792, 2.13144954556, 2.11990529922,
    2.10981557783, 2.10092204024, 2.09302405441, 2.08596344727,
    2.07961384473, 2.07387306790, 2.06865761042, 2.06389856163,
    2.05953855275, 2.05552943864, 2.05183051648, 2.04840714180,
    2.04522964213, 2.04227245630,
)


class InputError(ValueError):
    """An input artifact, cohort, or output path is unsafe or inconsistent."""


@dataclass(frozen=True)
class RunData:
    run_id: str
    marker_path: Path
    summary_path: Path
    joined_path: Path
    manifest_path: Path
    marker_digest: Mapping[str, int | str]
    summary_digest: Mapping[str, int | str]
    joined_digest: Mapping[str, int | str]
    manifest_digest: Mapping[str, int | str]
    input_artifacts: Mapping[str, object]
    joined_rows: int
    metric_values: Mapping[str, tuple[int, ...]]
    parameters: Mapping[str, object]
    cohort_identity: Mapping[str, object]
    analysis_policy: Mapping[str, object]


def reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def reject_nonfinite_json(value: str) -> object:
    raise InputError(f"non-finite JSON number {value!r}")


def load_json(path: Path, label: str) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(
                file,
                object_pairs_hook=reject_duplicate_json_keys,
                parse_constant=reject_nonfinite_json,
            )
    except InputError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InputError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(payload, dict):
        raise InputError(f"{label} {path}: JSON root must be an object")
    return payload


def integer_field(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{label} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f">= {minimum}"
        if maximum is not None:
            bounds += f" and <= {maximum}"
        raise InputError(f"{label} must be {bounds}")
    return value


def string_field(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InputError(f"{label} must be a nonempty string")
    return value


def path_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def resolve_file(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise InputError(f"cannot resolve {label} {path}: {error}") from error
    if not resolved.is_file():
        raise InputError(f"{label} is not a regular file: {path}")
    return resolved


def resolve_recorded_file(raw_path: object, run_directory: Path, label: str) -> Path:
    text = string_field(raw_path, label)
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = run_directory / candidate
    resolved = resolve_file(candidate, label)
    if not path_within(resolved, run_directory):
        raise InputError(f"{label} escapes run directory {run_directory}: {text}")
    return resolved


def file_digest(path: Path) -> dict[str, int | str]:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        size = path.stat().st_size
    except OSError as error:
        raise InputError(f"cannot hash {path}: {error}") from error
    return {"bytes": size, "sha256": digest.hexdigest()}


def marker_artifact(
    outputs: Mapping[str, object],
    key: str,
    marker_directory: Path,
) -> Path:
    specification = outputs.get(key)
    if not isinstance(specification, dict):
        raise InputError(f"completion outputs.{key} must be an object")
    recorded_path = string_field(
        specification.get("path"), f"completion outputs.{key}.path"
    )
    candidate = Path(recorded_path)
    if not candidate.is_absolute():
        candidate = marker_directory / candidate
    artifact = resolve_file(candidate, f"{key} artifact")

    expected_size = integer_field(
        specification.get("bytes"), f"completion outputs.{key}.bytes"
    )
    expected_hash = string_field(
        specification.get("sha256"), f"completion outputs.{key}.sha256"
    )
    if SHA256_PATTERN.fullmatch(expected_hash) is None:
        raise InputError(f"completion outputs.{key}.sha256 is not lowercase SHA-256")
    actual = file_digest(artifact)
    if actual["bytes"] != expected_size or actual["sha256"] != expected_hash:
        raise InputError(f"completion marker digest mismatch for {artifact}")
    return artifact


def nested_mapping(
    parent: Mapping[str, object], key: str, label: str
) -> dict[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise InputError(f"{label} must be an object")
    return value


def validate_summary_inputs(
    summary: Mapping[str, object], run_directory: Path
) -> dict[str, object]:
    inputs = nested_mapping(summary, "inputs", "summary inputs")
    if set(inputs) != set(INPUT_NAMES):
        raise InputError("summary inputs have missing or unknown names")
    resolved: dict[str, object] = {}
    for name in INPUT_NAMES:
        raw_value = inputs[name]
        if raw_value is None:
            if name in ("manifest", "ingress", "phc_calibration"):
                raise InputError(f"summary input {name} is required")
            resolved[name] = None
            continue
        if name == "phc_calibration":
            if not isinstance(raw_value, list) or not raw_value:
                raise InputError(
                    "summary input phc_calibration must be a nonempty list"
                )
            values: list[object] = raw_value
        else:
            if not isinstance(raw_value, str):
                raise InputError(f"summary input {name} must be a path or null")
            values = [raw_value]
        resolved_values: list[Path] = []
        for index, value in enumerate(values):
            suffix = f"[{index}]" if len(values) > 1 else ""
            resolved_values.append(resolve_recorded_file(
                value, run_directory, f"summary input {name}{suffix}"
            ))
        resolved[name] = (
            resolved_values if name == "phc_calibration" else resolved_values[0]
        )
    return resolved


def verified_input_spec(
    raw_spec: object, run_directory: Path, label: str
) -> Path:
    if not isinstance(raw_spec, dict) or set(raw_spec) != {
        "path", "bytes", "sha256"
    }:
        raise InputError(f"{label} must contain exactly path, bytes, and sha256")
    artifact = resolve_recorded_file(raw_spec.get("path"), run_directory, label)
    expected_size = integer_field(raw_spec.get("bytes"), f"{label}.bytes")
    expected_hash = string_field(raw_spec.get("sha256"), f"{label}.sha256")
    if SHA256_PATTERN.fullmatch(expected_hash) is None:
        raise InputError(f"{label}.sha256 is not lowercase SHA-256")
    actual = file_digest(artifact)
    if actual["bytes"] != expected_size or actual["sha256"] != expected_hash:
        raise InputError(f"input artifact digest mismatch for {artifact}")
    return artifact


def validate_input_artifacts(
    raw_inputs: object, run_directory: Path
) -> dict[str, object]:
    if not isinstance(raw_inputs, dict) or set(raw_inputs) != set(INPUT_NAMES):
        raise InputError("completion inputs have missing or unknown names")
    resolved: dict[str, object] = {}
    for name in INPUT_NAMES:
        value = raw_inputs[name]
        if value is None:
            if name in ("manifest", "ingress", "phc_calibration"):
                raise InputError(f"completion input {name} is required")
            resolved[name] = None
            continue
        if name == "phc_calibration":
            if not isinstance(value, list) or not value:
                raise InputError(
                    "completion input phc_calibration must be a nonempty list"
                )
            resolved[name] = [
                verified_input_spec(item, run_directory,
                                    f"completion input {name}[{index}]")
                for index, item in enumerate(value)
            ]
        else:
            if isinstance(value, list):
                raise InputError(f"completion input {name} must not be a list")
            resolved[name] = verified_input_spec(
                value, run_directory, f"completion input {name}"
            )
    return resolved


def parse_csv_integer(raw: str | None, path: Path, row: int, field: str) -> int:
    if raw is None or raw == "" or raw.strip() != raw:
        raise InputError(f"{path}: row {row}: invalid {field}")
    try:
        return int(raw, 10)
    except ValueError as error:
        raise InputError(
            f"{path}: row {row}: invalid integer {field}={raw!r}"
        ) from error


# 이 단계의 입력은 raw 수신 기록이 아니라 join_results.py가 완성한 run들이다.
# run 내부의 패킷 연결/시계 보정은 이미 끝났고, 여기서는 동일 조건의 반복 결과를
# 모아 패킷 전체 분포와 run 사이 변동을 각각 계산한다.
# 각 run이 독립적인 반복이라는 해석은 실제 실험 설계에서 충족해야 한다.
def load_joined_metrics(path: Path) -> tuple[int, dict[str, tuple[int, ...]]]:
    values: dict[str, list[int]] = {name: [] for name in METRIC_FIELDS}
    keys: set[tuple[int, int]] = set()
    row_count = 0
    try:
        file = path.open("r", encoding="utf-8", newline="")
    except OSError as error:
        raise InputError(f"cannot open joined CSV {path}: {error}") from error

    with file:
        reader = csv.DictReader(file)
        fields = reader.fieldnames
        if fields is None:
            raise InputError(f"{path}: CSV header is missing")
        if any(field is None or field == "" for field in fields):
            raise InputError(f"{path}: empty CSV column name")
        if len(fields) != len(set(fields)):
            raise InputError(f"{path}: duplicate CSV column name")
        required = {"flow_id", "sequence", *METRIC_FIELDS.values()}
        missing = required.difference(fields)
        if missing:
            raise InputError(
                f"{path}: missing CSV columns: {', '.join(sorted(missing))}"
            )

        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise InputError(f"{path}: row {row_number}: extra CSV fields")
            row_count += 1
            flow_id = parse_csv_integer(
                row.get("flow_id"), path, row_number, "flow_id"
            )
            sequence = parse_csv_integer(
                row.get("sequence"), path, row_number, "sequence"
            )
            if not 0 <= flow_id <= 0xFFFFFFFF:
                raise InputError(f"{path}: row {row_number}: flow_id outside uint32")
            if not 0 <= sequence <= 0xFFFFFFFFFFFFFFFF:
                raise InputError(f"{path}: row {row_number}: sequence outside uint64")
            key = (flow_id, sequence)
            if key in keys:
                raise InputError(
                    f"{path}: row {row_number}: duplicate flow_id+sequence {key}"
                )
            keys.add(key)

            for metric, field in METRIC_FIELDS.items():
                raw = row.get(field)
                if raw is None:
                    raise InputError(f"{path}: row {row_number}: missing {field}")
                # 빈 metric은 관측 또는 보정이 부족해 계산하지 못한 값이다.
                # 이를 0ns나 deadline miss로 바꾸지 않고 해당 metric 표본에서 뺀다.
                # 따라서 집계된 miss rate의 분모도 전체 송신 수가 아닌 유효 관측 수다.
                if raw == "":
                    continue
                values[metric].append(
                    parse_csv_integer(raw, path, row_number, field)
                )

    return row_count, {name: tuple(items) for name, items in values.items()}


def percentile_nearest_rank(sorted_values: Sequence[int], percentile: int) -> int:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    index = max(0, math.ceil(percentile * len(sorted_values) / 100) - 1)
    return sorted_values[index]


def metric_statistics(values: Sequence[int], deadline_ns: int) -> dict[str, object]:
    # deadline을 엄밀히 초과한 값만 miss다. deadline과 같은 값은 성공으로 센다.
    # p99는 정렬된 표본에서 nearest-rank 위치를 고르는 관측 분위수이며,
    # 아직 관측하지 않은 최악 지연에 대한 보장은 아니다.
    ordered = sorted(values)
    misses = sum(value > deadline_ns for value in ordered)
    return {
        "count": len(ordered),
        "negative_count": sum(value < 0 for value in ordered),
        "p50_ns": percentile_nearest_rank(ordered, 50) if ordered else None,
        "p95_ns": percentile_nearest_rank(ordered, 95) if ordered else None,
        "p99_ns": percentile_nearest_rank(ordered, 99) if ordered else None,
        "max_ns": ordered[-1] if ordered else None,
        "deadline_miss_count": misses,
        "deadline_miss_rate": misses / len(ordered) if ordered else None,
    }


def student_t_critical_95(degrees_of_freedom: int) -> float:
    """Return the two-sided 95% t critical value without SciPy.

    Exact tabulated values cover the small samples where the correction is
    large.  A third-order Cornish-Fisher expansion is used above 30 df.
    """

    if degrees_of_freedom < 1:
        raise ValueError("degrees_of_freedom must be positive")
    if degrees_of_freedom < len(T_CRITICAL_95):
        return T_CRITICAL_95[degrees_of_freedom]
    z = 1.959963984540054
    df = float(degrees_of_freedom)
    z2 = z * z
    z3 = z2 * z
    z5 = z3 * z2
    z7 = z5 * z2
    return (
        z
        + (z3 + z) / (4.0 * df)
        + (5.0 * z5 + 16.0 * z3 + 3.0 * z) / (96.0 * df * df)
        + (3.0 * z7 + 19.0 * z5 + 17.0 * z3 - 15.0 * z)
        / (384.0 * df * df * df)
    )


def mean_confidence_interval_95(
    values: Sequence[int | float],
) -> dict[str, int | float | None]:
    """Calculate an equal-run-weighted Student-t interval for a mean."""

    # 여기서 values 하나는 패킷 하나가 아니라 run 하나의 요약값이다.
    # 예를 들어 run별 p99 세 개를 받으면 '세 p99의 평균'과 그 평균의 CI를 구한다.
    # 이것은 모든 패킷을 합친 p99의 CI와 다르며, 각 run의 가중치는 동일하다.
    # 표준오차 = run 간 표준편차 / sqrt(run 수), 여기에 t 임계값을 곱한다.
    # 유효한 run이 하나뿐이면 평균만 있고 분산을 추정할 CI는 None으로 남긴다.
    sample = [float(value) for value in values]
    count = len(sample)
    mean = statistics.fmean(sample) if sample else None
    result: dict[str, int | float | None] = {
        "sample_count": count,
        "mean": mean,
        "sample_stddev": None,
        "ci95_low": None,
        "ci95_high": None,
    }
    if count < 2:
        return result
    assert mean is not None
    sample_stddev = statistics.stdev(sample)
    margin = (
        student_t_critical_95(count - 1)
        * sample_stddev
        / math.sqrt(count)
    )
    result.update(
        {
            "sample_stddev": sample_stddev,
            "ci95_low": mean - margin,
            "ci95_high": mean + margin,
        }
    )
    return result


def validate_summary_metrics(
    summary: Mapping[str, object],
    row_count: int,
    metric_values: Mapping[str, Sequence[int]],
) -> None:
    joined_rows = integer_field(summary.get("joined_rows"), "summary joined_rows")
    if joined_rows != row_count:
        raise InputError(
            f"summary joined_rows={joined_rows}, but joined CSV has {row_count} rows"
        )
    if summary.get("join_key") != ["flow_id", "sequence"]:
        raise InputError("summary join_key is not flow_id+sequence")
    summaries = nested_mapping(summary, "metrics", "summary metrics")
    for metric, values in metric_values.items():
        recorded = summaries.get(metric)
        if not isinstance(recorded, dict):
            raise InputError(f"summary metric {metric} must be an object")
        ordered = sorted(values)
        expected: dict[str, object] = {
            "count": len(ordered),
            "missing_count": row_count - len(ordered),
            "negative_count": sum(value < 0 for value in ordered),
            "min_ns": ordered[0] if ordered else None,
            "max_ns": ordered[-1] if ordered else None,
            "p95_ns": percentile_nearest_rank(ordered, 95) if ordered else None,
            "p99_ns": percentile_nearest_rank(ordered, 99) if ordered else None,
        }
        if recorded.get("unit") != "ns":
            raise InputError(f"summary metric {metric} has an unexpected unit")
        for field, expected_value in expected.items():
            actual_value = recorded.get(field)
            if expected_value is None:
                agrees = actual_value is None
            else:
                agrees = (
                    isinstance(actual_value, int)
                    and not isinstance(actual_value, bool)
                    and actual_value == expected_value
                )
            if not agrees:
                raise InputError(
                    f"summary metric {metric}.{field} disagrees with joined CSV"
                )


def manifest_cohort_identity(manifest: Mapping[str, object]) -> dict[str, object]:
    # 반복 실험은 같은 설정으로 다시 실행한 결과끼리 묶는다. manifest에 기록된
    # kernel/project 상태와 산출물 정보로 비교 조건을 구성한다.
    # RT와 non-RT처럼 의도적으로 조건이 다른 실험은 각각 집계한 뒤 비교한다.
    interface = manifest.get("interface")
    interface_name = interface.get("name") if isinstance(interface, dict) else None
    system = manifest.get("system")
    kernel_release = (
        system.get("kernel_release") if isinstance(system, dict) else None
    )

    def repository_identity(name: str) -> dict[str, object]:
        repository = manifest.get(name)
        if not isinstance(repository, dict):
            return {}
        series = repository.get("committed_series")
        return {
            "head": repository.get("head"),
            "status": repository.get("status"),
            "worktree_patch_digest": repository.get("worktree_patch_digest"),
            "committed_series": series if isinstance(series, dict) else None,
        }

    return {
        "interface_name": interface_name,
        "kernel_release": kernel_release,
        "project_repository": repository_identity("project_repository"),
        "kernel_repository": repository_identity("kernel_repository"),
        "artifact_hashes": manifest.get("artifact_hashes", []),
        "artifact_tree_hashes": manifest.get("artifact_tree_hashes", []),
    }


def analysis_policy(summary: Mapping[str, object]) -> dict[str, object]:
    calibration = nested_mapping(summary, "calibration", "summary calibration")
    outside = string_field(
        calibration.get("outside_range_policy"),
        "summary calibration.outside_range_policy",
    )
    inside = string_field(
        calibration.get("inside_range_policy"),
        "summary calibration.inside_range_policy",
    )
    return {
        "outside_range_policy": outside,
        "inside_range_policy": inside,
        "percentile_method": "nearest-rank",
        "sources_provided": source_presence(summary),
    }


def source_presence(summary: Mapping[str, object]) -> dict[str, bool]:
    sources = nested_mapping(summary, "sources", "summary sources")
    presence: dict[str, bool] = {}
    for name in ("ingress", "cpumap", "afxdp", "udp"):
        source = sources.get(name)
        if not isinstance(source, dict) or type(source.get("provided")) is not bool:
            raise InputError(f"summary sources.{name}.provided must be boolean")
        presence[name] = cast(bool, source["provided"])
    return presence


def load_run(marker_path: Path) -> RunData:
    marker = resolve_file(marker_path, "run completion marker")
    marker_directory = marker.parent
    completion = load_json(marker, "run completion marker")
    version = integer_field(
        completion.get("completion_schema_version"),
        "completion_schema_version",
    )
    if version != JOIN_COMPLETION_SCHEMA_VERSION:
        raise InputError(
            f"{marker}: completion_schema_version must be "
            f"{JOIN_COMPLETION_SCHEMA_VERSION}"
        )
    schema_version = integer_field(
        completion.get("schema_version"), "completion schema_version"
    )
    if schema_version != JOIN_COMPLETION_SCHEMA_VERSION:
        raise InputError(
            f"{marker}: schema_version must be {JOIN_COMPLETION_SCHEMA_VERSION}"
        )
    if completion.get("artifact_kind") != "ace-joined-run":
        raise InputError(f"{marker}: artifact_kind must be ace-joined-run")
    run_id = string_field(completion.get("run_id"), "completion run_id")
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise InputError(f"{marker}: invalid run_id")
    summary_version = integer_field(
        completion.get("summary_schema_version"), "summary_schema_version"
    )
    if summary_version != JOIN_SUMMARY_SCHEMA_VERSION:
        raise InputError(
            f"{marker}: summary_schema_version must be {JOIN_SUMMARY_SCHEMA_VERSION}"
        )
    outputs = nested_mapping(completion, "outputs", "completion outputs")
    joined = marker_artifact(outputs, "joined_csv", marker_directory)
    summary_path = marker_artifact(outputs, "summary_json", marker_directory)
    if len({marker, joined, summary_path}) != 3:
        raise InputError(f"{marker}: completion artifacts must be distinct")

    summary = load_json(summary_path, "run summary")
    recorded_summary_version = integer_field(
        summary.get("schema_version"), "summary schema_version"
    )
    if recorded_summary_version != summary_version:
        raise InputError(f"{summary_path}: schema_version disagrees with marker")
    run = nested_mapping(summary, "run", "summary run")
    if run.get("run_id") != run_id:
        raise InputError(f"{summary_path}: run_id disagrees with marker")
    recorded_manifest_version = integer_field(
        run.get("manifest_version"), "summary run.manifest_version"
    )
    if recorded_manifest_version != MANIFEST_VERSION:
        raise InputError(f"{summary_path}: unsupported manifest version")

    recorded_run_directory = string_field(
        run.get("run_directory"), "summary run.run_directory"
    )
    try:
        resolved_recorded_directory = Path(recorded_run_directory).resolve(strict=True)
    except OSError as error:
        raise InputError(
            f"cannot resolve recorded run directory {recorded_run_directory}: {error}"
        ) from error
    if not resolved_recorded_directory.is_dir():
        raise InputError(f"{summary_path}: recorded run directory is not a directory")
    run_directory = resolved_recorded_directory
    for label, artifact in (
        ("completion marker", marker),
        ("joined CSV", joined),
        ("summary JSON", summary_path),
    ):
        if not path_within(artifact, run_directory):
            raise InputError(
                f"{summary_path}: {label} escapes recorded run directory"
            )

    recorded_completion = resolve_recorded_file(
        summary.get("completion_marker"), run_directory,
        "summary completion_marker",
    )
    if recorded_completion != marker:
        raise InputError(f"{summary_path}: completion marker path disagrees")
    summary_inputs = validate_summary_inputs(summary, run_directory)
    marker_inputs = completion.get("inputs")
    summary_artifacts = summary.get("input_artifacts")
    if marker_inputs != summary_artifacts:
        raise InputError("completion and summary input artifacts disagree")
    verified_inputs = validate_input_artifacts(marker_inputs, run_directory)
    if verified_inputs != summary_inputs:
        raise InputError("input artifact paths disagree with summary inputs")

    manifest_path = cast(Path, verified_inputs["manifest"])
    recorded_manifest = resolve_recorded_file(
        run.get("manifest"), run_directory, "summary run manifest"
    )
    if recorded_manifest != manifest_path:
        raise InputError(f"{summary_path}: manifest paths disagree")
    if manifest_path.parent != run_directory:
        raise InputError(f"{summary_path}: manifest is not at the run root")
    manifest = load_json(manifest_path, "run manifest")
    manifest_version = integer_field(
        manifest.get("manifest_version"), "manifest_version"
    )
    if manifest_version != MANIFEST_VERSION:
        raise InputError(f"{manifest_path}: unsupported manifest_version")
    if manifest.get("run_id") != run_id:
        raise InputError(f"{manifest_path}: run_id disagrees with marker")
    parameters = manifest.get("parameters", {})
    if not isinstance(parameters, dict):
        raise InputError(f"{manifest_path}: parameters must be an object")
    presence = source_presence(summary)
    expected_presence = {
        "ingress": True,
        "cpumap": verified_inputs["cpumap"] is not None,
        "afxdp": verified_inputs["afxdp"] is not None,
        "udp": verified_inputs["udp_native"] is not None,
    }
    if presence != expected_presence:
        raise InputError("summary source presence disagrees with input artifacts")

    row_count, values = load_joined_metrics(joined)
    validate_summary_metrics(summary, row_count, values)
    return RunData(
        run_id=run_id,
        marker_path=marker,
        summary_path=summary_path,
        joined_path=joined,
        manifest_path=manifest_path,
        marker_digest=file_digest(marker),
        summary_digest=file_digest(summary_path),
        joined_digest=file_digest(joined),
        manifest_digest=file_digest(manifest_path),
        input_artifacts=cast(Mapping[str, object], marker_inputs),
        joined_rows=row_count,
        metric_values=values,
        parameters=parameters,
        cohort_identity=manifest_cohort_identity(manifest),
        analysis_policy=analysis_policy(summary),
    )


def validate_runs(runs: Sequence[RunData]) -> list[RunData]:
    if len(runs) < 2:
        raise InputError("at least two completed runs are required")
    run_ids = [run.run_id for run in runs]
    if len(run_ids) != len(set(run_ids)):
        raise InputError("duplicate run_id in aggregation inputs")
    artifact_paths = [
        path
        for run in runs
        for path in (
            run.marker_path, run.summary_path, run.joined_path,
            run.manifest_path,
        )
    ]
    if len(artifact_paths) != len(set(artifact_paths)):
        raise InputError("an input artifact is reused by more than one run")

    first = runs[0]
    for run in runs[1:]:
        if run.parameters != first.parameters:
            raise InputError(
                f"mixed manifest parameters: {first.run_id} and {run.run_id}"
            )
        if run.cohort_identity != first.cohort_identity:
            raise InputError(
                f"mixed software/system cohort: {first.run_id} and {run.run_id}"
            )
        if run.analysis_policy != first.analysis_policy:
            raise InputError(
                f"mixed analyzer policy: {first.run_id} and {run.run_id}"
            )
    return sorted(runs, key=lambda run: run.run_id)


def aggregate_runs(
    runs: Sequence[RunData], deadline_ns: int
) -> tuple[list[dict[str, object]], dict[str, object]]:
    metric_rows: list[dict[str, object]] = []
    run_summaries: list[dict[str, object]] = []

    # 먼저 run마다 각 지연 성분의 분위수와 miss rate를 계산해 보관한다.
    # 이후 패킷을 합쳐도 이 run 경계가 남아 있어 반복 사이의 변동을 볼 수 있다.
    # 어떤 run에 특정 metric 표본이 없으면 그 metric의 CI 표본 수도 줄어든다.
    per_run_statistics: dict[str, dict[str, dict[str, object]]] = {}
    for run in runs:
        metric_map = {
            name: metric_statistics(run.metric_values[name], deadline_ns)
            for name in METRIC_FIELDS
        }
        per_run_statistics[run.run_id] = metric_map
        run_summaries.append(
            {
                "run_id": run.run_id,
                "completion_marker": {
                    "path": str(run.marker_path), **run.marker_digest,
                },
                "summary_json": {
                    "path": str(run.summary_path), **run.summary_digest,
                },
                "joined_csv": {
                    "path": str(run.joined_path), **run.joined_digest,
                },
                "manifest": {
                    "path": str(run.manifest_path),
                    **run.manifest_digest,
                },
                "input_artifacts": run.input_artifacts,
                "joined_rows": run.joined_rows,
                "metrics": metric_map,
            }
        )

    aggregated_metrics: dict[str, dict[str, object]] = {}
    for metric in METRIC_FIELDS:
        # pooled는 모든 run의 패킷을 한 배열로 모은 분포다. 표본이 많은 run은
        # 이 분포에서 자연스럽게 더 큰 비중을 차지한다.
        # '전체 관측 패킷 중 99%가 어느 지연 이하인가'를 보고 싶을 때 사용한다.
        # 아래 run_estimates의 동일 run 가중치 평균과는 답하는 질문이 다르다.
        pooled = [
            value
            for run in runs
            for value in run.metric_values[metric]
        ]
        packet_level = metric_statistics(pooled, deadline_ns)
        runs_with_samples = sum(bool(run.metric_values[metric]) for run in runs)
        runs_with_misses = sum(
            cast(
                int,
                per_run_statistics[run.run_id][metric]["deadline_miss_count"],
            ) > 0
            for run in runs
        )
        # 같은 run의 패킷들은 부하/스케줄링 상태를 공유할 수 있다.
        # 따라서 패킷 수를 독립 반복 수로 삼지 않고 run별 요약값에 CI를 붙인다.
        # p99 CI는 run별 p99 평균의 불확실성이고, miss rate CI도 같은 방식이다.
        run_estimates = {
            field: mean_confidence_interval_95([
                cast(
                    int | float,
                    per_run_statistics[run.run_id][metric][field],
                )
                for run in runs
                if per_run_statistics[run.run_id][metric][field] is not None
            ])
            for field in (
                "p50_ns", "p95_ns", "p99_ns", "deadline_miss_rate"
            )
        }
        # packet-level miss rate: 유효 패킷 중 deadline을 넘긴 비율.
        # run-level with_deadline_miss_rate: 표본이 있는 run 중 한 번이라도 넘긴 비율.
        # 둘은 분모와 의미가 달라서 결과에 나란히 남긴다.
        run_level = {
            "total_count": len(runs),
            "with_samples_count": runs_with_samples,
            "without_samples_count": len(runs) - runs_with_samples,
            "with_deadline_miss_count": runs_with_misses,
            "with_deadline_miss_rate": (
                runs_with_misses / runs_with_samples
                if runs_with_samples else None
            ),
            "independent_run_estimates": run_estimates,
        }
        aggregated_metrics[metric] = {
            "unit": "ns",
            "deadline_ns": deadline_ns,
            "run_level": run_level,
            "packet_level": packet_level,
        }
        metric_rows.append(
            {
                "metric": metric,
                "unit": "ns",
                "deadline_ns": deadline_ns,
                "run_total_count": run_level["total_count"],
                "run_with_samples_count": run_level["with_samples_count"],
                "run_without_samples_count": run_level["without_samples_count"],
                "run_with_deadline_miss_count": run_level[
                    "with_deadline_miss_count"
                ],
                "run_with_deadline_miss_rate": run_level[
                    "with_deadline_miss_rate"
                ],
                "run_p50_mean_ns": run_estimates["p50_ns"]["mean"],
                "run_p50_ci95_low_ns": run_estimates["p50_ns"]["ci95_low"],
                "run_p50_ci95_high_ns": run_estimates["p50_ns"]["ci95_high"],
                "run_p95_mean_ns": run_estimates["p95_ns"]["mean"],
                "run_p95_ci95_low_ns": run_estimates["p95_ns"]["ci95_low"],
                "run_p95_ci95_high_ns": run_estimates["p95_ns"]["ci95_high"],
                "run_p99_mean_ns": run_estimates["p99_ns"]["mean"],
                "run_p99_ci95_low_ns": run_estimates["p99_ns"]["ci95_low"],
                "run_p99_ci95_high_ns": run_estimates["p99_ns"]["ci95_high"],
                "run_deadline_miss_rate_mean": run_estimates[
                    "deadline_miss_rate"
                ]["mean"],
                "run_deadline_miss_rate_ci95_low": run_estimates[
                    "deadline_miss_rate"
                ]["ci95_low"],
                "run_deadline_miss_rate_ci95_high": run_estimates[
                    "deadline_miss_rate"
                ]["ci95_high"],
                "packet_count": packet_level["count"],
                "negative_count": packet_level["negative_count"],
                "p50_ns": packet_level["p50_ns"],
                "p95_ns": packet_level["p95_ns"],
                "p99_ns": packet_level["p99_ns"],
                "max_ns": packet_level["max_ns"],
                "deadline_miss_count": packet_level["deadline_miss_count"],
                "deadline_miss_rate": packet_level["deadline_miss_rate"],
            }
        )

    summary = {
        "aggregation_schema_version": AGGREGATION_SCHEMA_VERSION,
        "run_count": len(runs),
        "run_ids": [run.run_id for run in runs],
        "deadline": {
            "deadline_ns": deadline_ns,
            "miss_operator": ">",
            "percentile_method": "nearest-rank",
        },
        "confidence_intervals": {
            "confidence_level": 0.95,
            "method": "two-sided-student-t-mean",
            "unit_of_independence": "run",
            "run_weighting": "equal",
            "pooled_packet_ci": False,
            "insufficient_sample_policy": "ci-null-when-run-count-less-than-2",
        },
        "cohort": {
            "manifest_parameters": runs[0].parameters,
            "identity": runs[0].cohort_identity,
            "analysis_policy": runs[0].analysis_policy,
        },
        "runs": run_summaries,
        "metrics": aggregated_metrics,
    }
    return metric_rows, summary


def output_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def resolve_outputs(paths: Sequence[Path]) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for path in paths:
        try:
            parent = path.parent.resolve(strict=True)
        except OSError as error:
            raise InputError(
                f"cannot resolve output parent {path.parent}: {error}"
            ) from error
        if not parent.is_dir():
            raise InputError(f"output parent is not a directory: {path.parent}")
        candidate = path.resolve(strict=False)
        if candidate.parent != parent:
            raise InputError(f"output path escapes its resolved parent: {path}")
        if output_exists(path):
            raise InputError(f"refusing to overwrite output {path}")
        resolved.append(candidate)
    if len(resolved) != len(set(resolved)):
        raise InputError(
            "aggregate CSV, summary JSON, and completion paths must differ"
        )
    if len({path.parent for path in resolved}) != 1:
        raise InputError("aggregate outputs must share one resolved directory")
    return tuple(resolved)


def reserve_output(path: Path) -> int:
    return os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644
    )


def fsync_directories(paths: Sequence[Path]) -> None:
    for directory in sorted({path.parent for path in paths}, key=str):
        descriptor = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_outputs(
    csv_path: Path,
    json_path: Path,
    marker_path: Path,
    rows: Sequence[Mapping[str, object]],
    summary: Mapping[str, object],
    run_ids: Sequence[str],
) -> None:
    created: list[Path] = []
    descriptor = -1
    try:
        descriptor = reserve_output(csv_path)
        created.append(csv_path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
            descriptor = -1
            writer = csv.DictWriter(file, fieldnames=AGGREGATE_FIELDS)
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
        # 집계에서도 CSV/JSON 기록이 끝난 뒤 complete 표시를 만든다.
        # 이 표시는 통계가 파일로 완성됐다는 뜻이며 deadline 충족 여부는
        # summary의 각 metric 값과 유효 관측 수를 보고 해석한다.
        completion = {
            "completion_schema_version": AGGREGATION_COMPLETION_SCHEMA_VERSION,
            "artifact_kind": "ace-repeated-run-aggregation",
            "aggregation_schema_version": AGGREGATION_SCHEMA_VERSION,
            "run_ids": list(run_ids),
            "outputs": {
                "aggregate_csv": {
                    "path": str(csv_path),
                    **file_digest(csv_path),
                },
                "summary_json": {
                    "path": str(json_path),
                    **file_digest(json_path),
                },
            },
        }
        descriptor = reserve_output(marker_path)
        created.append(marker_path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            descriptor = -1
            json.dump(completion, file, indent=2, sort_keys=True, allow_nan=False)
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


def decimal_deadline(value: str) -> int:
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", value) is None:
        raise argparse.ArgumentTypeError("must be an unsigned decimal integer")
    parsed = int(value, 10)
    if parsed > 0x7FFFFFFFFFFFFFFF:
        raise argparse.ArgumentTypeError("must fit in signed 64-bit nanoseconds")
    return parsed


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate completed ACE run metrics and evaluate one explicit "
            "nanosecond deadline"
        )
    )
    parser.add_argument(
        "--run-completion", "--run", action="append", required=True, type=Path,
        help="repeatable join_results completion marker (at least two)",
    )
    parser.add_argument(
        "--deadline-ns", required=True, type=decimal_deadline,
        help="a latency strictly greater than this value is a deadline miss",
    )
    parser.add_argument(
        "--output-csv", required=True, type=Path,
        help="new aggregate metric CSV",
    )
    parser.add_argument(
        "--summary-json", required=True, type=Path,
        help="new aggregate summary JSON",
    )
    parser.add_argument(
        "--completion-marker", type=Path,
        help="new completion JSON (default: SUMMARY_JSON.complete)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = argument_parser()
    args = parser.parse_args(argv)
    if args.completion_marker is None:
        args.completion_marker = Path(f"{args.summary_json}.complete")
    try:
        runs = validate_runs([load_run(path) for path in args.run_completion])
        csv_path, json_path, marker_path = resolve_outputs(
            (args.output_csv, args.summary_json, args.completion_marker)
        )
        input_paths = {
            path
            for run in runs
            for path in (
                run.marker_path,
                run.summary_path,
                run.joined_path,
                run.manifest_path,
            )
        }
        if any(path in input_paths for path in (csv_path, json_path, marker_path)):
            raise InputError("an aggregate output aliases an input artifact")
        rows, summary = aggregate_runs(runs, args.deadline_ns)
        summary["completion_marker"] = str(marker_path)
        write_outputs(
            csv_path, json_path, marker_path, rows, summary,
            [run.run_id for run in runs],
        )
    except (InputError, OSError, UnicodeError, csv.Error) as error:
        parser.exit(1, f"error: {error}\n")
    print(
        f"aggregated {len(runs)} completed run(s): {csv_path}; "
        f"summary: {json_path}; complete: {marker_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
