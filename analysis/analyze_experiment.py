#!/usr/bin/env python3
"""Offline experiment join, flow statistics, and a self-contained HTML report.

Run this after all capture processes have stopped. Raw capture artifacts are
never rewritten. A runner completion marker and completed run.json are both
required for performance aggregation; socket-only runs are operational smoke
checks and never supply hardware latency results.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import shutil
import statistics
import sys
from pathlib import Path
from typing import Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis import aggregate_runs as aggregate
from analysis import join_results as join
from analysis.clock_validation import validate_clock


class ExperimentError(ValueError):
    pass


def read_json(path: Path) -> dict:
    return aggregate.load_json(path, str(path))


def contained_file(directory: Path, name: str) -> Path:
    path = (directory / name).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(directory):
        raise ExperimentError(f"artifact escapes run directory: {name}")
    return path


def unsigned(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExperimentError(f"{label} must be a nonnegative integer")
    return value


def condition_identity(descriptor: Mapping) -> dict:
    return {key: descriptor.get(key) for key in
            ("case_id", "mode", "duration_s", "flows", "load", "environment", "config")}


def canonical_cpu_list(value: object, label: str) -> str:
    """Normalize an explicit Linux CPU list without inventing an inherited mask."""
    if not isinstance(value, str) or not value:
        raise ExperimentError(f"{label} must be a CPU list")
    if value == "inherit":
        return value
    cpus = set()
    for field in value.split(","):
        if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?", field):
            raise ExperimentError(f"{label} has an invalid CPU list")
        bounds = [int(item) for item in field.split("-")]
        first, last = bounds[0], bounds[-1]
        if first > last or last >= 1024:
            raise ExperimentError(f"{label} has an out-of-range CPU")
        cpus.update(range(first, last + 1))
    return ",".join(str(cpu) for cpu in sorted(cpus))


def receiver_cpu_list(flow: Mapping) -> str:
    if "rx_cpus" in flow:
        return canonical_cpu_list(flow["rx_cpus"], "rx_cpus")
    cpu = flow.get("rx_cpu")
    if cpu is None or cpu == -1:
        return "inherit"
    if isinstance(cpu, bool) or not isinstance(cpu, int) or not 0 <= cpu < 1024:
        raise ExperimentError("rx_cpu must be -1 or a valid CPU")
    return str(cpu)


def receiver_placement(descriptor: Mapping) -> dict:
    environment = descriptor.get("environment") or {}
    shared = environment.get("receiver_shared_cpus", "")
    if not isinstance(shared, str):
        raise ExperimentError("receiver_shared_cpus must be a CPU list")
    if shared:
        shared = canonical_cpu_list(shared, "receiver_shared_cpus")
        if shared == "inherit":
            raise ExperimentError("receiver_shared_cpus must name its CPUs")
    target = environment.get("receiver_cpumap_cpu")
    if target is not None and (isinstance(target, bool) or not isinstance(target, int)
                               or not 0 <= target < 1024):
        raise ExperimentError("receiver_cpumap_cpu must be a valid CPU")
    placement = {"shared_cpus": shared, "cpumap_cpu": target,
                 "flows": [{"flow_id": flow["flow_id"], "rx_cpus": receiver_cpu_list(flow)}
                           for flow in descriptor["flows"]]}
    available = environment.get("receiver_available_cpus", "")
    if not isinstance(available, str):
        raise ExperimentError("receiver_available_cpus must be a CPU list")
    if available:
        available = canonical_cpu_list(available, "receiver_available_cpus")
        if available == "inherit" or target is None or str(target) not in available.split(","):
            raise ExperimentError("receiver_available_cpus must include the CPUMAP CPU")
        active = environment.get("receiver_cpumap_active")
        expected_active = descriptor["mode"] == "mixed" and any(
            flow["flow_id"] == 2 and flow.get("requested_path") == "cpumap"
            for flow in descriptor["flows"])
        if type(active) is not bool or active != expected_active:
            raise ExperimentError("receiver_cpumap_active disagrees with active flows")
        expected_shared = ",".join(cpu for cpu in available.split(",")
                                   if not active or cpu != str(target))
        if not expected_shared or shared != expected_shared:
            raise ExperimentError("receiver_shared_cpus disagrees with the mode's CPU reservation")
        for flow in descriptor["flows"]:
            expected = str(target) if active and flow["flow_id"] == 2 else shared
            if receiver_cpu_list(flow) != expected:
                raise ExperimentError("receiver flow CPU set disagrees with the mode's CPU reservation")
        placement.update(available_cpus=available, cpumap_active=active)
    return placement


def receiver_partition_evidence(directory: Path, placement: Mapping) -> dict:
    evidence = {}
    for state in ("active", "restored"):
        name = f"cpu-partition-{state}.json"
        if (directory / name).exists():
            evidence[state] = read_json(contained_file(directory, name))
    if "cpumap_active" not in placement:
        return evidence
    if not placement["cpumap_active"]:
        if evidence:
            raise ExperimentError("CPU reservation evidence exists for a shared-only run")
        return evidence
    if set(evidence) != {"active", "restored"}:
        raise ExperimentError("completed CPUMAP run requires reservation and restoration evidence")
    active, restored = evidence["active"], evidence["restored"]
    owner = active.get("owner_pid")
    if isinstance(owner, bool) or not isinstance(owner, int) or owner <= 0 or restored.get("owner_pid") != owner:
        raise ExperimentError("CPU partition evidence has inconsistent owner")
    for state, record in evidence.items():
        if (record.get("verified") is not True or record.get("reserved_cpu") != placement["cpumap_cpu"] or
                canonical_cpu_list(record.get("available_cpus"), "partition available_cpus") != placement["available_cpus"]):
            raise ExperimentError(f"CPU partition {state} evidence disagrees with configured CPUs")
    if (active.get("partition") != "isolated" or
            canonical_cpu_list(active.get("partition_cpus"), "partition_cpus") != str(placement["cpumap_cpu"]) or
            canonical_cpu_list(active.get("general_cpus"), "partition general_cpus") != placement["shared_cpus"] or
            canonical_cpu_list(active.get("root_cpus"), "partition root_cpus") != placement["shared_cpus"]):
        raise ExperimentError("CPU partition was not isolated with the recorded general domain")
    if (restored.get("partition") != "released" or
            canonical_cpu_list(restored.get("root_cpus"), "restored root_cpus") != placement["available_cpus"]):
        raise ExperimentError("CPU partition restoration did not return all available CPUs")
    return evidence


def receiver_cpu_label(flow: Mapping) -> str:
    cpus = receiver_cpu_list(flow)
    if cpus == "inherit":
        return "상위 프로세스 설정 상속"
    return f"CPU {cpus}" + (" · 공유 허용 범위" if "," in cpus else "")


def condition_label(condition: Mapping) -> str:
    label = f'{condition["case_id"]} / {condition["mode"]}'
    if condition.get("placement_variant") is not None:
        label += f' · 배치 {condition["placement_variant"]}'
    return label


def verify_collection(directory: Path) -> None:
    if not (directory / "collection.json").exists():
        return
    collection = read_json(contained_file(directory, "collection.json"))
    for relative, expected in collection.get("artifacts", {}).items():
        path = contained_file(directory, relative)
        if join.file_digest(path) != expected:
            raise ExperimentError(f"collected artifact digest mismatch: {relative}")


def collect_results(receiver: Path, sender: Path, output: Path) -> Path:
    """Copy completed Pi artifacts once, without changing either source tree."""
    receiver = receiver.resolve(strict=True)
    sender = sender.resolve(strict=True)
    output = output.resolve()
    if output == receiver or output == sender or output.is_relative_to(receiver) or output.is_relative_to(sender):
        raise ExperimentError("collection output must be outside both source directories")
    rx = load_descriptor(receiver)
    tx = load_descriptor(sender)
    if rx["run_id"] != tx["run_id"] or condition_identity(rx) != condition_identity(tx):
        raise ExperimentError("receiver and sender run_id/config conditions disagree")
    completed = all(descriptor.get("state") == "completed" and
                    (directory / ".complete").is_file() and not (directory / ".failed").exists()
                    for directory, descriptor in ((receiver, rx), (sender, tx)))
    for flow in rx["flows"] if completed else []:
        stats = read_json(contained_file(sender, f"sender-flow{flow['flow_id']}.json"))
        if stats.get("flow_id", flow["flow_id"]) != flow["flow_id"] or stats.get("expected_count") != flow["count"]:
            raise ExperimentError("sender statistics disagree with run flow")
    files: list[tuple[Path, str]] = []
    for source, prefix in ((receiver, ""), (sender, "sender/")):
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if relative.parts[0] in ("analysis", "analysis-report"):
                continue
            if path.is_symlink():
                raise ExperimentError(f"symlink in collection source: {path}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ExperimentError(f"nonregular collection artifact: {path}")
            if source == receiver and relative.as_posix() == ".complete":
                continue
            target = prefix + relative.as_posix()
            files.append((path, target))
    for flow in rx["flows"]:
        name = f"sender-flow{flow['flow_id']}.json"
        if (sender / name).is_file():
            files.append((contained_file(sender, name), name))
    names = [name for _, name in files]
    if len(set(names)) != len(names) or any(name in ("collection.json", ".collection-failed") for name in names):
        raise ExperimentError("collection destination names collide")
    # mkdir is the exclusive reservation; no existing result directory is reused.
    output.mkdir(parents=False, exist_ok=False)
    hashes = {}
    try:
        for source, relative in files:
            expected = join.file_digest(source)
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as incoming, destination.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if join.file_digest(destination) != expected or join.file_digest(source) != expected:
                raise ExperimentError(f"source changed during collection: {source}")
            hashes[relative] = expected
        payload = {"schema_version": 1, "run_id": rx["run_id"],
                   "receiver_source": str(receiver), "sender_source": str(sender),
                   "source_runs_completed": completed,
                   "artifacts": hashes}
        with (output / "collection.json").open("x", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n"); file.flush(); os.fsync(file.fileno())
        marker = ".complete" if completed else ".failed"
        # Preserve an already copied failure marker byte for byte.
        if not (output / marker).exists():
            with (output / marker).open("xb") as file:
                file.write((receiver / ".complete").read_bytes() if completed else b"source run incomplete or failed\n")
                file.flush(); os.fsync(file.fileno())
        return output
    except BaseException:
        (output / ".collection-failed").write_text("collection did not complete\n", encoding="utf-8")
        raise


def collect_suite(receiver: Path, sender: Path, output: Path) -> Path:
    receiver = receiver.resolve(strict=True); sender = sender.resolve(strict=True)
    if (receiver / "run.json").is_file():
        return collect_results(receiver, sender, output)
    rx_runs = sorted((receiver / "runs").glob("*/run.json"))
    tx_runs = {}
    for path in (sender / "runs").glob("*/run.json"):
        descriptor = load_descriptor(path.parent.resolve())
        if descriptor["run_id"] in tx_runs:
            raise ExperimentError("duplicate sender run_id")
        tx_runs[descriptor["run_id"]] = path.parent
    if not rx_runs:
        raise ExperimentError("receiver directory has no run.json or runs/*/run.json")
    rx_descriptors = [load_descriptor(path.parent.resolve()) for path in rx_runs]
    rx_ids = [descriptor["run_id"] for descriptor in rx_descriptors]
    if len(set(rx_ids)) != len(rx_ids):
        raise ExperimentError("duplicate receiver run_id")
    output = output.resolve()
    if output.is_relative_to(receiver) or output.is_relative_to(sender):
        raise ExperimentError("collection output must be outside source directories")
    output.mkdir(parents=False, exist_ok=False)
    (output / "runs").mkdir()
    entries = []; errors = []
    for path, descriptor in zip(rx_runs, rx_descriptors):
        run_id = descriptor["run_id"]
        destination = output / "runs" / run_id
        state = "completed"
        try:
            if run_id not in tx_runs:
                raise ExperimentError("matching sender run is absent")
            collect_results(path.parent, tx_runs[run_id], destination)
            if (destination / ".failed").exists():
                state = "failed"
        except (ExperimentError, aggregate.InputError, OSError, ValueError) as error:
            state = "failed"
            destination.mkdir(exist_ok=True)
            # Keep an explicit failed entry; never synthesize packet measurements.
            (destination / ".collection-failed").write_text(str(error)+"\n", encoding="utf-8")
            errors.append({"run_id": run_id, "reason": str(error), "receiver_source": str(path.parent)})
        entries.append({"run_id": run_id, "case_id": descriptor.get("case_id"),
                        "mode": descriptor["mode"], "repeat": descriptor.get("repeat"),
                        "state": state, "dir": f"runs/{run_id}"})
    for run_id in sorted(set(tx_runs) - set(rx_ids)):
        destination = output / "runs" / run_id
        destination.mkdir()
        reason = "matching receiver run is absent"
        (destination / ".collection-failed").write_text(reason+"\n", encoding="utf-8")
        errors.append({"run_id": run_id, "reason": reason, "sender_source": str(tx_runs[run_id])})
        entries.append({"run_id": run_id, "state": "failed", "dir": f"runs/{run_id}"})
    with (output / "suite.json").open("x", encoding="utf-8") as file:
        json.dump({"schema_version": 1, "runs": entries, "collection_errors": errors}, file,
                  ensure_ascii=False, indent=2)
    return output


def load_descriptor(directory: Path) -> dict:
    descriptor = read_json(contained_file(directory, "run.json"))
    if descriptor.get("schema_version") != 1:
        raise ExperimentError("run.json schema_version must be 1")
    if not isinstance(descriptor.get("run_id"), str) or not aggregate.RUN_ID_PATTERN.fullmatch(descriptor["run_id"]):
        raise ExperimentError("invalid run_id")
    if descriptor.get("mode") not in ("mixed", "all-pass", "pass-xsk", "socket-only"):
        raise ExperimentError("invalid run mode")
    if descriptor.get("environment") is not None and not isinstance(descriptor["environment"], dict):
        raise ExperimentError("environment must be an object")
    flows = descriptor.get("flows")
    if not isinstance(flows, list) or not flows:
        raise ExperimentError("run.json requires flows")
    ids = set()
    for flow in flows:
        if not isinstance(flow, dict):
            raise ExperimentError("flow must be an object")
        flow_id = unsigned(flow.get("flow_id"), "flow_id")
        if flow_id > 0xFFFFFFFF or flow_id in ids:
            raise ExperimentError("flow_id is repeated or out of range")
        ids.add(flow_id)
        unsigned(flow.get("count"), "flow count")
        if not unsigned(flow.get("period_ns"), "period_ns"):
            raise ExperimentError("period_ns must be positive")
        if flow.get("deadline_ns") is not None:
            unsigned(flow["deadline_ns"], "deadline_ns")
        cpus = receiver_cpu_list(flow)
        if "rx_cpus" in flow and "rx_cpu" in flow:
            expected = -1 if cpus == "inherit" or "," in cpus else int(cpus)
            if (isinstance(flow["rx_cpu"], bool) or not isinstance(flow["rx_cpu"], int)
                    or flow["rx_cpu"] != expected):
                raise ExperimentError("rx_cpu disagrees with rx_cpus")
    receiver_placement(descriptor)
    return descriptor


def join_completed_run(directory: Path, descriptor: Mapping) -> aggregate.RunData:
    output = directory / "analysis"
    output.mkdir(exist_ok=True)
    marker = output / "summary.json.complete"
    tracking = bool((descriptor.get("environment") or {}).get("phc_tracking"))
    if marker.exists():
        run = aggregate.load_run(marker)
        if run.run_id != descriptor["run_id"]:
            raise ExperimentError("join run_id disagrees with run.json")
        if run.analysis_policy["hw_timestamp_policy"] != join.HW_TIMESTAMP_POLICY:
            raise ExperimentError("existing join uses the previous HW clock policy; collect into a new output directory and analyze again")
        if tracking:
            summary = read_json(run.summary_path)
            if [Path(p).name for p in summary["inputs"]["phc_calibration"]] != ["phc-during.csv"]:
                raise ExperimentError("existing join does not use continuous PHC observations")
        return run
    args = ["--manifest", str(contained_file(directory, "manifest.json")),
            "--ingress", str(contained_file(directory, "xdp-ingress_records.csv")),
            "--output-csv", str(output / "joined.csv"),
            "--summary-json", str(output / "summary.json")]
    for option, name in (("--cpumap", "xdp-cpumap_records.csv"),
                         ("--afxdp", "afxdp.csv")):
        if (directory / name).exists():
            args.extend((option, str(contained_file(directory, name))))
    for path in sorted(directory.glob("udp-flow*.bin")):
        args.extend(("--udp-native", str(contained_file(directory, path.name))))
    for name in (("phc-during.csv",) if tracking else ("phc-before.csv", "phc-after.csv")):
        args.extend(("--phc-calibration", str(contained_file(directory, name))))
    parsed = join.argument_parser().parse_args(args)
    rows, summary = join.analyze(parsed)
    if summary["run"]["run_id"] != descriptor["run_id"]:
        raise ExperimentError("manifest run_id disagrees with run.json")
    join.write_outputs(parsed.output_csv, parsed.summary_json, parsed.completion_marker,
                       rows, summary, descriptor["run_id"])
    return aggregate.load_run(marker)


def sysstat_summary(path: Path, kind: str) -> dict:
    """Read LC_ALL=C mpstat/sar output; prefer final Average rows."""
    if not path.exists():
        return {}
    columns: list[str] = []
    samples: dict[str, list[dict[str, float]]] = {}
    averages: dict[str, dict[str, float]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        marker = "CPU" if kind == "cpu" else "IFACE"
        if marker in parts:
            columns = parts[parts.index(marker) + 1:]
            continue
        if not columns or len(parts) < len(columns) + 2:
            continue
        key = parts[-len(columns) - 1]
        if kind == "cpu" and key != "all" and not key.isdigit():
            continue
        try:
            values = dict(zip(columns, (float(item) for item in parts[-len(columns):])))
        except ValueError:
            continue
        if not all(math.isfinite(item) for item in values.values()):
            continue
        if kind == "cpu" and "%idle" in values:
            values["busy_pct"] = 100.0 - values["%idle"]
        if parts[0] == "Average:":
            averages[key] = values
        else:
            samples.setdefault(key, []).append(values)
    result = {}
    for key in sorted(set(samples) | set(averages)):
        values = averages.get(key)
        if values is None:
            values = {column: statistics.fmean(row[column] for row in samples[key])
                      for column in samples[key][0]}
        result[key] = values
    return result


CPU_COUNTERS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
NETWORK_COUNTERS = ("rx_bytes", "rx_packets", "rx_errors", "rx_dropped",
                    "tx_bytes", "tx_packets", "tx_errors", "tx_dropped")


def receiver_measurement_window(directory: Path, run: aggregate.RunData | None) -> dict:
    """Use only timestamps on the receiver's MONOTONIC clock."""
    first = last = None
    source = "first_xdp_to_last_receive_observation" if run is not None else "first_user_to_last_user"
    if run is not None:
        with run.joined_path.open(encoding="utf-8", newline="") as file:
            for row in csv.DictReader(file):
                ingress = int(row["initial_xdp_ns"]) if row.get("initial_xdp_ns") else 0
                user = int(row["user_rx_mono_ns"]) if row.get("user_rx_mono_ns") else 0
                cpumap = int(row["cpumap_ns"]) if row.get("cpumap_ns") else 0
                if ingress > 0:
                    first = ingress if first is None else min(first, ingress)
                    last = ingress if last is None else max(last, ingress)
                if cpumap > 0:
                    last = cpumap if last is None else max(last, cpumap)
                if user > 0 and row.get("user_source") in ("udp", "afxdp"):
                    last = user if last is None else max(last, user)
    else:
        for path in sorted(directory.glob("udp-flow*.bin")):
            table = join.load_udp_records(contained_file(directory, path.name))
            for record in table.records.values():
                timestamp = record["user_rx_mono_ns"]
                if timestamp > 0:
                    first = timestamp if first is None else min(first, timestamp)
                    last = timestamp if last is None else max(last, timestamp)
    valid = first is not None and last is not None and last > first
    return {"status": "available" if valid else "unavailable",
            "clock": "CLOCK_MONOTONIC", "source": source,
            "start_ns": first, "end_ns": last,
            "duration_ns": last - first if valid else None}


def measurement_cpu_summary(path: Path, window: Mapping) -> tuple[dict, dict]:
    """Crop cumulative CPU counters to the observed packet-processing window.

    Boundary intervals assume a constant tick rate within that sample interval;
    all complete intervals retain their measured counter deltas unchanged.
    """
    detail = {"status": "missing-samples", "window": dict(window),
              "method": "counter-deltas-weighted-by-overlapping-monotonic-time",
              "busy_definition": "total minus idle and iowait; guest ticks not counted twice",
              "sample_interval_ns": None, "coverage": {}}
    if not path.is_file():
        return {}, detail
    if window.get("status") != "available":
        detail["status"] = "unavailable-window"
        return {}, detail
    try:
        by_cpu: dict[str, list[tuple[int, tuple[int, ...]]]] = {}
        snapshots: dict[int, set[str]] = {}
        with path.open(encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames != ["monotonic_ns", "cpu", *CPU_COUNTERS]:
                raise ExperimentError("unexpected CPU sample columns")
            for row in reader:
                cpu = row["cpu"]
                if cpu != "all" and (not cpu or not cpu.isascii() or not cpu.isdigit()):
                    raise ExperimentError("invalid CPU sample identifier")
                values = [row["monotonic_ns"], *(row[key] for key in CPU_COUNTERS)]
                if any(value is None or not value.isascii() or not value.isdigit() for value in values):
                    raise ExperimentError("CPU samples require nonnegative integer counters")
                timestamp, *counters = map(int, values)
                samples = by_cpu.setdefault(cpu, [])
                if samples and timestamp <= samples[-1][0]:
                    raise ExperimentError("CPU sample timestamps must increase per CPU")
                samples.append((timestamp, tuple(counters)))
                snapshots.setdefault(timestamp, set()).add(cpu)
        if "all" not in by_cpu or len(snapshots) < 2:
            raise ExperimentError("CPU samples need two snapshots including all CPUs")
        if any(cpus != set(by_cpu) for cpus in snapshots.values()):
            raise ExperimentError("CPU snapshot is missing a CPU row")
        start, end = window["start_ns"], window["end_ns"]
        span = end - start
        result = {}
        intervals = []
        for cpu, samples in by_cpu.items():
            totals = [0.0] * len(CPU_COUNTERS)
            covered = 0
            for (before_ns, before), (after_ns, after) in zip(samples, samples[1:]):
                elapsed = after_ns - before_ns
                intervals.append(elapsed)
                overlap = max(0, min(end, after_ns) - max(start, before_ns))
                if not overlap:
                    continue
                delta = [new - old for old, new in zip(before, after)]
                if any(value < 0 for value in delta):
                    raise ExperimentError("CPU counter decreased inside measurement window")
                if sum(delta) <= 0:
                    continue
                covered += overlap
                for index, value in enumerate(delta):
                    totals[index] += value * (overlap / elapsed)
            detail["coverage"][cpu] = {"covered_ns": covered, "fraction": covered / span}
            if covered == span:
                total = sum(totals)
                percentages = {"%" + name: value * 100 / total
                               for name, value in zip(CPU_COUNTERS, totals)}
                percentages["busy_pct"] = (total - totals[3] - totals[4]) * 100 / total
                result[cpu] = percentages
        detail["sample_interval_ns"] = {"min": min(intervals), "max": max(intervals)}
        detail["snapshot_count"] = len(snapshots)
        detail["status"] = "complete" if len(result) == len(by_cpu) else "partial-coverage"
        return result, detail
    except (ExperimentError, ValueError, TypeError, KeyError, csv.Error) as error:
        detail.update(status="invalid-samples", reason=str(error))
        return {}, detail


def measurement_network_summary(path: Path, window: Mapping) -> tuple[dict, dict]:
    """Use interface counter deltas only over the receiver's capture window.

    Boundary counter values are linearly interpolated between observations.
    Whole intervals keep the measured deltas; idle intervals are valid data.
    """
    detail = {"status": "missing-samples", "window": dict(window),
              "scope": "receiver-measurement-window", "clock": "CLOCK_MONOTONIC",
              "method": "counter-deltas-weighted-by-overlapping-monotonic-time",
              "source": "/proc/net/dev", "sample_interval_ns": None, "coverage": {}}
    if not path.is_file():
        return {}, detail
    if window.get("status") != "available":
        detail["status"] = "unavailable-window"
        return {}, detail
    try:
        start, end = window["start_ns"], window["end_ns"]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in (start, end)) or end <= start:
            raise ExperimentError("invalid network measurement window")
        span = end - start
        by_interface: dict[str, list[tuple[int, tuple[int, ...]]]] = {}
        snapshots: dict[int, set[str]] = {}
        with path.open(encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames != ["monotonic_ns", "interface", *NETWORK_COUNTERS]:
                raise ExperimentError("unexpected network sample columns")
            for row in reader:
                interface = row["interface"]
                if interface is None or re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface) is None:
                    raise ExperimentError("invalid network interface identifier")
                values = [row["monotonic_ns"], *(row[key] for key in NETWORK_COUNTERS)]
                if None in row or any(value is None or not value.isascii() or not value.isdigit()
                                      for value in values):
                    raise ExperimentError("network samples require nonnegative integer counters")
                timestamp, *counters = map(int, values)
                if any(value > 2**64 - 1 for value in (timestamp, *counters)):
                    raise ExperimentError("network sample exceeds uint64 range")
                samples = by_interface.setdefault(interface, [])
                if samples and timestamp <= samples[-1][0]:
                    raise ExperimentError("network sample timestamps must increase per interface")
                if samples and any(new < old for old, new in zip(samples[-1][1], counters)):
                    raise ExperimentError("network counter decreased between samples")
                samples.append((timestamp, tuple(counters)))
                snapshots.setdefault(timestamp, set()).add(interface)
        if not by_interface or len(snapshots) < 2:
            raise ExperimentError("network samples need two snapshots")
        if any(interfaces != set(by_interface) for interfaces in snapshots.values()):
            raise ExperimentError("network snapshot is missing an interface row")
        result, intervals = {}, []
        for interface, samples in by_interface.items():
            totals = [0.0] * len(NETWORK_COUNTERS)
            covered = 0
            for (before_ns, before), (after_ns, after) in zip(samples, samples[1:]):
                elapsed = after_ns - before_ns
                intervals.append(elapsed)
                overlap = max(0, min(end, after_ns) - max(start, before_ns))
                if not overlap:
                    continue
                covered += overlap
                for index, (old, new) in enumerate(zip(before, after)):
                    totals[index] += (new - old) * (overlap / elapsed)
            detail["coverage"][interface] = {"covered_ns": covered, "fraction": covered / span}
            if covered == span:
                values = dict(zip(NETWORK_COUNTERS, totals))
                for direction in ("rx", "tx"):
                    values[f"{direction}_bps"] = values[f"{direction}_bytes"] * 8e9 / span
                    values[f"{direction}_pps"] = values[f"{direction}_packets"] * 1e9 / span
                result[interface] = values
        detail["sample_interval_ns"] = {"min": min(intervals), "max": max(intervals)}
        detail["snapshot_count"] = len(snapshots)
        detail["status"] = "complete" if len(result) == len(by_interface) else "partial-coverage"
        return result, detail
    except (ExperimentError, ValueError, TypeError, KeyError, csv.Error) as error:
        detail.update(status="invalid-samples", reason=str(error))
        return {}, detail


def read_capture_keys(directory: Path, run: aggregate.RunData | None) -> dict[int, set[int]]:
    keys: dict[int, set[int]] = {}
    if run is not None:
        with run.joined_path.open(encoding="utf-8", newline="") as file:
            for row in csv.DictReader(file):
                if row["user_source"] in ("udp", "afxdp"):
                    keys.setdefault(int(row["flow_id"]), set()).add(int(row["sequence"]))
    else:
        for path in sorted(directory.glob("udp-flow*.bin")):
            table = join.load_udp_records(contained_file(directory, path.name))
            for flow_id, sequence in table.records:
                keys.setdefault(flow_id, set()).add(sequence)
    return keys


def delivery_statistics(directory: Path, descriptor: Mapping, run: aggregate.RunData | None) -> list[dict]:
    received = read_capture_keys(directory, run)
    configured_ids = {flow["flow_id"] for flow in descriptor["flows"]}
    observed_ids = aggregate.run_flow_ids(run) if run is not None else set(received)
    if observed_ids - configured_ids:
        raise ExperimentError("capture contains an unconfigured flow")
    results = []
    for flow in descriptor["flows"]:
        flow_id = flow["flow_id"]
        name = f"sender-flow{flow_id}.json"
        sender = read_json(contained_file(directory, name))
        if "flow_id" in sender and sender["flow_id"] != flow_id:
            raise ExperimentError(f"{name}: flow_id mismatch")
        if "period_ns" in sender and sender["period_ns"] != flow["period_ns"]:
            raise ExperimentError(f"{name}: period_ns mismatch")
        expected = unsigned(sender.get("expected_count"), f"{name} expected_count")
        sent = unsigned(sender.get("sent"), f"{name} sent")
        errors = unsigned(sender.get("errors"), f"{name} errors")
        if expected != flow["count"] or sent + errors > expected:
            raise ExperimentError(f"{name}: counts disagree with run.json")
        elapsed = unsigned(sender.get("elapsed_ns"), f"{name} elapsed_ns")
        if "start_mono_ns" in sender and "end_mono_ns" in sender:
            start = unsigned(sender["start_mono_ns"], f"{name} start_mono_ns")
            end = unsigned(sender["end_mono_ns"], f"{name} end_mono_ns")
            if end < start or elapsed != end - start:
                raise ExperimentError(f"{name}: elapsed time disagrees with timestamps")
        traffic_elapsed = elapsed
        sequence_set = received.get(flow_id, set())
        if any(sequence >= expected for sequence in sequence_set):
            raise ExperimentError(f"flow {flow_id}: received sequence exceeds configured range")
        count = len(sequence_set)
        if count > sent:
            raise ExperimentError(f"flow {flow_id}: received count exceeds actual sent count")
        receiver_name = f"receiver-flow{flow_id}.json"
        receiver = None
        if (directory / receiver_name).exists():
            receiver = read_json(contained_file(directory, receiver_name))
            if "flow_id" in receiver and receiver["flow_id"] != flow_id:
                raise ExperimentError(f"{receiver_name}: flow_id mismatch")
        loss = sent - count
        results.append({
            "flow_id": flow_id, "requested_path": flow.get("requested_path"),
            "expected_count": expected, "sent": sent, "send_errors": errors,
            "unsent": expected - sent, "received_unique": count,
            "loss_count": loss, "loss_rate": loss / sent if sent else None,
            "sender_elapsed_ns": elapsed,
            "traffic_elapsed_ns": traffic_elapsed,
            "offered_pps": sent * 1e9 / traffic_elapsed if traffic_elapsed else None,
            "delivered_pps_over_sender_window": count * 1e9 / traffic_elapsed if traffic_elapsed else None,
            "deadline_ns": flow.get("deadline_ns"),
            "sender_statistics": sender, "receiver_statistics": receiver,
        })
    return results


def inspect_run(directory: Path) -> tuple[dict, aggregate.RunData | None, dict | None]:
    result = {"directory": str(directory), "run_id": directory.name,
              "status": "incomplete", "mode": None, "reason": None}
    try:
        if (directory / ".collection-failed").exists():
            result.update(status="failed", reason="result collection did not complete")
            return result, None, None
        if not (directory / "run.json").is_file():
            result.update(status="failed" if (directory / ".failed").exists() else "incomplete",
                          reason="run.json is absent")
            return result, None, None
        descriptor = load_descriptor(directory)
        result.update({key: descriptor.get(key) for key in
                       ("run_id", "case_id", "mode", "repeat", "purpose", "duration_s")})
        if (directory / ".failed").exists() or descriptor.get("state") == "failed":
            result.update(status="failed", reason="runner marked this run failed")
            return result, None, descriptor
        if not (directory / ".complete").is_file() or descriptor.get("state") != "completed":
            result["reason"] = "runner completion is absent"
            return result, None, descriptor
        verify_collection(directory)
        if descriptor.get("purpose") == "warmup":
            result.update(status="warmup-completed", hardware_latency=False,
                          reason="warmup capture excluded from measurement aggregation")
            return result, None, descriptor
        run = None if descriptor["mode"] == "socket-only" else join_completed_run(directory, descriptor)
        if run is not None:
            summary = read_json(run.summary_path)
            if (descriptor.get("environment") or {}).get("phc_tracking"):
                result["clock_validation"] = validate_clock(
                    run.joined_path, contained_file(directory, "phc-during.csv"))
                if not result["clock_validation"]["accepted"]:
                    raise ExperimentError("continuous clock validation failed: " + json.dumps(result["clock_validation"]))
            result["decomposition"] = summary.get("decomposition")
            result["hw_timestamp_policy"] = run.analysis_policy["hw_timestamp_policy"]
        result["flows"] = descriptor["flows"]
        result["environment"] = descriptor.get("environment")
        result["receiver_placement"] = receiver_placement(descriptor)
        result["cpu_partition"] = receiver_partition_evidence(directory, result["receiver_placement"])
        result["receiver_placements"] = {}
        for flow in descriptor["flows"]:
            name = f"receiver-flow{flow['flow_id']}.placement.json"
            if not (directory / name).exists():
                continue
            observed = read_json(contained_file(directory, name))
            cpus = observed.get("cpus")
            if (not isinstance(cpus, list) or not cpus or
                    any(isinstance(cpu, bool) or not isinstance(cpu, int) or
                        not 0 <= cpu < 1024 for cpu in cpus)):
                raise ExperimentError(f"{name}: invalid allowed CPU set")
            actual = canonical_cpu_list(",".join(str(cpu) for cpu in cpus), name)
            expected = receiver_cpu_list(flow)
            if observed.get("verified") is not True or expected != "inherit" and expected != actual:
                raise ExperimentError(f"{name}: receiver affinity verification failed")
            result["receiver_placements"][str(flow["flow_id"])] = observed
        result["delivery"] = delivery_statistics(directory, descriptor, run)
        result["cpu_observation"] = sysstat_summary(directory / "monitor" / "mpstat.log", "cpu")
        measurement_window = receiver_measurement_window(directory, run)
        result["cpu"], result["cpu_measurement"] = measurement_cpu_summary(
            directory / "monitor" / "cpu-samples.csv", measurement_window)
        result["network"], result["network_measurement"] = measurement_network_summary(
            directory / "monitor" / "network-samples.csv", measurement_window)
        result["network_observation"] = {
            "scope": "full-monitor", "source": "sar network.log",
            "interfaces": sysstat_summary(directory / "monitor" / "network.log", "network")}
        sender_manifest = directory / "sender" / "manifest.json"
        result["sender_cohort"] = (aggregate.manifest_cohort_identity(read_json(sender_manifest))
                                    if sender_manifest.is_file() else None)
        # Bind the report to each consumed raw sidecar as well as the joiner's inputs.
        artifact_names = ["run.json", ".complete"]
        artifact_names += [f"sender-flow{flow['flow_id']}.json" for flow in descriptor["flows"]]
        artifact_names += [path.name for path in directory.glob("receiver-flow*.json")]
        artifact_names += [path.name for path in directory.glob("udp-flow*.bin")]
        artifact_names += [f"cpu-partition-{state}.json" for state in result["cpu_partition"]]
        artifact_names += [name for name in ("monitor/mpstat.log", "monitor/cpu-samples.csv", "monitor/network.log", "monitor/network-samples.csv")
                           if (directory / name).is_file()]
        result["input_artifacts"] = {
            name: join.file_digest(contained_file(directory, name)) for name in artifact_names
        }
        result["status"] = "smoke-completed" if run is None else "completed"
        result["hardware_latency"] = run is not None
        return result, run, descriptor
    except (ExperimentError, aggregate.InputError, join.InputError, OSError,
            ValueError, TypeError, KeyError, csv.Error) as error:
        result.update(status="analysis-failed", reason=str(error))
        return result, None, None


def build_report(directories: Sequence[Path]) -> dict:
    runs = []
    groups: dict[tuple[str, str, str], list[tuple[dict, aggregate.RunData, dict]]] = {}
    for directory in directories:
        result, loaded, descriptor = inspect_run(directory)
        runs.append(result)
        if loaded is not None and descriptor is not None:
            placement = json.dumps(receiver_placement(descriptor), sort_keys=True)
            key = (str(descriptor.get("case_id", "default")), descriptor["mode"], placement)
            groups.setdefault(key, []).append((result, loaded, descriptor))
    conditions = []
    variant_counts = {}
    for case_id, mode, _ in groups:
        variant_counts[(case_id, mode)] = variant_counts.get((case_id, mode), 0) + 1
    variant_indices = {}
    for (case_id, mode, placement), entries in sorted(groups.items()):
        variant_indices[(case_id, mode)] = variant_indices.get((case_id, mode), 0) + 1
        condition = {"case_id": case_id, "mode": mode, "status": "completed",
                     "receiver_placement": json.loads(placement),
                     "placement_variant": variant_indices[(case_id, mode)]
                     if variant_counts[(case_id, mode)] > 1 else None}
        try:
            descriptors = [entry[2] for entry in entries]
            comparable = lambda d: {key: d.get(key) for key in ("flows", "load", "duration_s", "mode", "environment")}
            if any(comparable(item) != comparable(descriptors[0]) for item in descriptors[1:]):
                raise ExperimentError("repeated runs have different flow/load conditions")
            if any(entry[0]["sender_cohort"] != entries[0][0]["sender_cohort"] for entry in entries[1:]):
                raise ExperimentError("repeated runs have different sender software/system cohorts")
            loaded = [entry[1] for entry in entries]
            if len(loaded) > 1:
                loaded = aggregate.validate_runs(loaded)
            flows = descriptors[0]["flows"]
            deadlines = {flow["flow_id"]: flow["deadline_ns"] for flow in flows
                         if flow.get("deadline_ns") is not None}
            _, statistics_by_flow = aggregate.aggregate_by_flow(
                loaded, deadlines, flow_ids=[flow["flow_id"] for flow in flows])
            delivery = {}
            for flow in flows:
                flow_id = flow["flow_id"]
                records = [next(item for item in entry[0]["delivery"] if item["flow_id"] == flow_id)
                           for entry in entries]
                sent = sum(item["sent"] for item in records)
                received = sum(item["received_unique"] for item in records)
                delivery[str(flow_id)] = {
                    "sent": sent, "received_unique": received, "loss_count": sent - received,
                    "loss_rate": (sent - received) / sent if sent else None,
                    "delivered_pps_run_mean": aggregate.mean_confidence_interval_95([
                        item["delivered_pps_over_sender_window"] for item in records
                        if item["delivered_pps_over_sender_window"] is not None]),
                    "loss_rate_run_mean": aggregate.mean_confidence_interval_95([
                        item["loss_rate"] for item in records if item["loss_rate"] is not None]),
                }
            cpu = [entry[0]["cpu"].get("all", {}).get("busy_pct") for entry in entries]
            condition.update(statistics=statistics_by_flow, delivery=delivery,
                             flows=flows, load=descriptors[0].get("load"),
                             environment=descriptors[0].get("environment"),
                             decomposition=[{"run_id": entry[0]["run_id"], **entry[0]["decomposition"]}
                                            for entry in entries if entry[0].get("decomposition")],
                             cpu_busy_pct=aggregate.mean_confidence_interval_95([
                                 item for item in cpu if item is not None]))
        except (ExperimentError, aggregate.InputError, OSError, ValueError) as error:
            condition.update(status="aggregation-failed", reason=str(error))
        conditions.append(condition)
    return {"schema_version": 1, "artifact_kind": "ace-experiment-report",
            "runs": runs, "conditions": conditions,
            "methods": {"latency": "NIC hardware RX to userspace, PHC/MONOTONIC calibrated",
                        "hw_timestamp_policy": join.HW_TIMESTAMP_POLICY,
                        "percentiles": "nearest-rank", "ci": "equal-run-weight two-sided Student-t 95%",
                        "delivery_window": "first attempted send to sender end; includes delivery during final drain",
                        "cpu_window": "receiver first XDP to last XDP/CPUMAP/userspace observation, CLOCK_MONOTONIC",
                        "cpu_average": "counter deltas weighted by overlapping time at sample boundaries; full window coverage required",
                        "cpu_observation": "legacy mpstat whole-observation average, excluded from measurement CPU aggregation",
                        "network_window": "same receiver MONOTONIC window as CPU; full counter-sample coverage required",
                        "network_rates": "NIC interface byte/packet counter deltas with linear interpolation at window boundaries; includes measurement and background traffic",
                        "network_observation": "legacy sar whole-monitor average, excluded from measurement NIC rates",
                        "receiver_affinity": "allowed CPU sets; separate affinity and CPU reservation layouts are aggregated separately",
                        "deadline": "only explicitly supplied per-flow values",
                        "socket_only": "operational smoke validation only"}}


def number(value: object, scale: float = 1, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{float(value) * scale:,.3f}{suffix}"


def bars(title: str, entries: list[tuple[str, float | None]], unit: str) -> str:
    values = [(label, value) for label, value in entries if value is not None and math.isfinite(value)]
    if not values:
        return ""
    maximum = max([value for _, value in values] + [1e-12])
    height = 45 + len(values) * 35
    parts = [f'<section><h2>{html.escape(title)}</h2><svg role="img" aria-label="{html.escape(title)}" viewBox="0 0 900 {height}">']
    for index, (label, value) in enumerate(values):
        y = 12 + index * 35
        width = max(0.0, value) / maximum * 440
        parts.append(f'<text x="0" y="{y+17}">{html.escape(label)}</text>'
                     f'<rect x="275" y="{y}" width="{width:.2f}" height="23" rx="3" fill="#236b9a"/>'
                     f'<text x="{285+width:.2f}" y="{y+17}">{number(value)} {html.escape(unit)}</text>')
    return "".join(parts) + "</svg></section>"


def render_html(report: Mapping) -> str:
    escape = lambda value: html.escape(str(value))
    parts = ['<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>ACE 실험 분석</title><style>body{font:15px system-ui,sans-serif;background:#f5f7fa;color:#172a3a;margin:0;padding:30px;line-height:1.6}main{max-width:1250px;margin:auto}h1{margin:0}h2{font-size:20px}section{background:white;padding:24px;margin:20px 0;border-radius:10px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:9px;border-bottom:1px solid #dce4eb;text-align:left;vertical-align:top}th{background:#edf3f7}svg{width:100%;font:13px system-ui}code{overflow-wrap:anywhere}.note{color:#526879}.bad{color:#a52a32}.scroll{overflow:auto}</style><main>',
             '<h1>ACE 혼합 중요도 패킷 실험</h1><p class="note">완료된 run의 실제 기록을 경로·flow별로 분석합니다. 원시 자료는 그대로 보존합니다.</p>',
             '<section><h2>실행 상태</h2><table><tr><th>Run</th><th>조건 / 구성</th><th>상태</th><th>내용</th></tr>']
    for run in report["runs"]:
        detail = ("소켓 송수신 운영 검증 — 하드웨어 지연 통계 제외" if run["status"] == "smoke-completed" else
                  "워밍업 기록 보존 — 본 측정 집계 제외" if run["status"] == "warmup-completed" else run.get("reason") or "기록 결합·시계 보정 완료")
        parts.append(f'<tr><td>{escape(run["run_id"])}</td><td>{escape(run.get("case_id"))} / {escape(run.get("mode"))}</td><td>{escape(run["status"])}</td><td>{escape(detail)}</td></tr>')
    parts.append('</table></section><section><h2>조건별·flow별 결과</h2><div class="scroll"><table><tr><th>조건</th><th>Flow</th><th>Run 수</th><th>평균 지연 µs</th><th>p99 µs</th><th>관측 최대 µs</th><th>Run p99 평균 [95% CI] µs</th><th>수신 / 송신</th><th>손실률</th><th>수신 PPS</th><th>Deadline µs / 초과율</th></tr>')
    latency_bars = []; loss_bars = []; cpu_bars = []
    for condition in report["conditions"]:
        label = condition_label(condition)
        if condition["status"] != "completed":
            parts.append(f'<tr><td colspan="11" class="bad">{escape(label)}: {escape(condition.get("reason"))}</td></tr>')
            continue
        cpu_bars.append((label, condition["cpu_busy_pct"]["mean"]))
        for flow_id, flow in condition["statistics"]["flows"].items():
            metric = flow["metrics"]["user-hw"]
            pooled = metric["packet_level"]
            ci = metric["run_level"]["independent_run_estimates"]["p99_ns"]
            delivery = condition["delivery"][flow_id]
            confidence = f'{number(ci["mean"], .001)} [{number(ci["ci95_low"], .001)}, {number(ci["ci95_high"], .001)}]'
            deadline = "미지정" if metric["deadline_ns"] is None else f'{number(metric["deadline_ns"], .001)} / {number(pooled["deadline_miss_rate"], 100, "%")}'
            values = [label, flow_id, flow["run_count"], number(pooled["mean_ns"], .001), number(pooled["p99_ns"], .001), number(pooled["max_ns"], .001), confidence,
                      f'{delivery["received_unique"]} / {delivery["sent"]}', number(delivery["loss_rate"], 100, "%"), number(delivery["delivered_pps_run_mean"]["mean"]), deadline]
            parts.append('<tr>'+''.join(f'<td>{escape(value)}</td>' for value in values)+'</tr>')
            latency_bars.append((label+f' / flow {flow_id}', None if ci["mean"] is None else ci["mean"] / 1000))
            loss_bars.append((label+f' / flow {flow_id}', None if delivery["loss_rate"] is None else delivery["loss_rate"] * 100))
    parts.append('</table></div><p class="note">지연은 NIC 하드웨어 RX → 사용자 수신 구간입니다. Deadline 미지정 시 초과율은 계산하지 않습니다. 신뢰구간은 독립 run 통계이며 1회 실행은 CI 없이 표시합니다. 수신 PPS의 분모는 첫 송신 시도부터 마지막 송신 종료까지이며, 수신 개수에는 종료 후 drain 구간의 도착분도 포함됩니다.</p></section>')
    layouts = set()
    for condition in report["conditions"]:
        placement = condition.get("receiver_placement", {})
        if condition["status"] == "completed" and "available_cpus" in placement:
            layouts.add((condition["mode"], placement["available_cpus"], placement["shared_cpus"],
                         placement["cpumap_cpu"] if placement["cpumap_active"] else None))
    if layouts:
        parts.append('<section><h2>구성별 CPU 사용 영역</h2><table><tr><th>구성</th><th>전체 CPU</th><th>일반 작업·PASS·AF_XDP 공유</th><th>CPUMAP 중요 수신 전용</th></tr>')
        for mode, available, shared, reserved in sorted(layouts, key=lambda row: tuple(str(value) for value in row)):
            values = [mode, f'CPU {available}', f'CPU {shared}',
                      f'CPU {reserved}' if reserved is not None else '없음 · 전체 CPU 공유']
            parts.append('<tr>'+''.join(f'<td>{escape(value)}</td>' for value in values)+'</tr>')
        parts.append('</table><p class="note">CPUMAP 중요 흐름을 실행하는 동안 전용 CPU를 예약하고, 실행이 끝나면 일반 공유 영역에 반환합니다.</p></section>')
    parts.append('<section><h2>구간별 지연과 수신 앱 실행 범위</h2><div class="scroll"><table><tr><th>조건 / 구성</th><th>Flow / 경로</th><th>수신 앱 허용 CPU</th><th>d₁ p99 µs</th><th>d₂ p99 µs</th><th>d₃ p99 µs</th><th>XDP 이후 p99 µs</th></tr>')
    for condition in report["conditions"]:
        if condition["status"] != "completed":
            continue
        for flow in condition.get("flows", []):
            path = flow.get("requested_path")
            metrics = condition["statistics"]["flows"][str(flow["flow_id"])]["metrics"]
            p99 = lambda name: metrics[name]["packet_level"]["p99_ns"]
            d2 = p99("cpumap-xdp") if path == "cpumap" else 0 if path in ("pass", "xsk") else None
            d3 = p99("user-cpumap") if path == "cpumap" else p99("user-xdp") if path in ("pass", "xsk") else None
            values = [condition_label(condition), f'{flow["flow_id"]} / {path}',
                      receiver_cpu_label(flow), number(p99("xdp-hw"), .001), number(d2, .001),
                      number(d3, .001), number(p99("user-xdp"), .001)]
            parts.append('<tr>'+''.join(f'<td>{escape(value)}</td>' for value in values)+'</tr>')
    parts.append('</table></div><p class="note">허용 CPU는 수신 앱이 스케줄링될 수 있는 범위입니다. 여러 CPU를 허용한 앱들은 그 실행 영역을 공유합니다. 같은 조건명이어도 실행 범위가 다르면 별도 배치로 집계합니다.</p><p class="note">d₁: NIC→최초 XDP · d₂: CPUMAP CPU 전달(PASS/AF_XDP는 0) · d₃: 마지막 XDP→사용자 수신. 각 p99는 해당 구간의 분포에서 계산하며 총지연 p99는 패킷별 총지연에서 직접 계산합니다. XDP 이후 지연은 PASS·AF_XDP의 d₃와 CPUMAP의 d₂+d₃를 같은 관측 경계에서 비교합니다.</p></section>')
    parts.append('<section><h2>같은 패킷의 구간 합 검사</h2><table><tr><th>Run</th><th>일치</th><th>관측 부족</th><th>무효</th><th>불일치</th><th>최대 절대 잔차 ns</th></tr>')
    for run in report["runs"]:
        decomposition = run.get("decomposition")
        if decomposition is None:
            continue
        counts = decomposition["counts"]
        values = [run["run_id"], *(counts[name] for name in ("complete", "incomplete", "invalid", "mismatch")),
                  decomposition["max_abs_residual_ns"] if decomposition["max_abs_residual_ns"] is not None else "—"]
        parts.append('<tr>'+''.join(f'<td>{escape(value)}</td>' for value in values)+'</tr>')
    parts.append('</table><p class="note">패킷별 보정 HW 시각을 d₁과 총지연에 공통 사용합니다. 잔차는 총지연−(d₁+d₂+d₃)이며 음수 구간·메타데이터 오류는 무효로 표시합니다.</p></section>')
    parts.extend((bars("Run p99 평균", latency_bars, "µs"), bars("패킷 손실률", loss_bars, "%"), bars("수신 측정 구간 CPU 사용률", cpu_bars, "%")))
    parts.append('<p class="note">CPU 사용률은 수신 Pi의 최초 XDP부터 최종 수신 관측(XDP·CPUMAP·사용자 수신)까지 계산합니다. 경계의 1초 표본은 측정창과 겹친 시간으로 가중합니다. 준비 대기와 저장 단계는 집계에서 제외하며, 표본이 전체 측정창을 덮지 못하면 값을 표시하지 않습니다. 소켓 운영 검증은 최초·최종 사용자 수신 사이를 사용합니다.</p>')
    if any(run.get("network") for run in report["runs"]):
        parts.append('<section><h2>수신 측정 구간 NIC 트래픽</h2><div class="scroll"><table><tr><th>Run / NIC</th><th>RX Mbps</th><th>RX PPS</th><th>TX Mbps</th><th>RX 드롭 증가</th><th>RX 오류 증가</th></tr>')
        for run in report["runs"]:
            for interface, traffic in run.get("network", {}).items():
                values = [f'{run["run_id"]} / {interface}', number(traffic["rx_bps"], 1e-6),
                          number(traffic["rx_pps"]), number(traffic["tx_bps"], 1e-6),
                          number(traffic["rx_dropped"]), number(traffic["rx_errors"])]
                parts.append('<tr>'+''.join(f'<td>{escape(value)}</td>' for value in values)+'</tr>')
        parts.append('</table></div><p class="note">NIC 누적 카운터를 CPU와 같은 수신 측정창으로 잘라 계산합니다. 계측 패킷과 배경 트래픽을 함께 포함하며, 경계 표본의 증가량은 겹친 시간으로 가중합니다. 원본 카운터와 측정창 범위는 report.json에 기록합니다.</p></section>')
    parts.append('<section><h2>Run별 송수신·부하</h2><table><tr><th>Run / flow</th><th>계획 / 실제 송신 / 수신</th><th>송신 오류</th><th>손실</th><th>실제 송신 PPS</th><th>수신 측정 구간 CPU</th></tr>')
    for run in report["runs"]:
        for delivery in run.get("delivery", []):
            values = [f'{run["run_id"]} / {delivery["flow_id"]}', f'{delivery["expected_count"]} / {delivery["sent"]} / {delivery["received_unique"]}', delivery["send_errors"], delivery["loss_count"], number(delivery["offered_pps"]), number(run.get("cpu", {}).get("all", {}).get("busy_pct"), 1, "%")]
            parts.append('<tr>'+''.join(f'<td>{escape(value)}</td>' for value in values)+'</tr>')
    parts.append('</table></section><p class="note">세부 구간별 지연·CPU별 사용률·NIC 관측값·입력 파일 hash는 report.json에 함께 저장됩니다.</p></main></html>')
    return "".join(parts)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "collect":
        parser = argparse.ArgumentParser(description="Copy and match the two Pi result directories after measurement")
        parser.add_argument("--receiver-dir", type=Path, required=True)
        parser.add_argument("--sender-dir", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(arguments[1:])
        try:
            print(collect_suite(args.receiver_dir, args.sender_dir, args.output))
        except (ExperimentError, aggregate.InputError, OSError, ValueError) as error:
            parser.exit(1, f"error: {error}\n")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-dir", type=Path)
    source.add_argument("--suite-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, help="default: SOURCE/analysis-report")
    args = parser.parse_args(arguments)
    root = (args.run_dir or args.suite_dir).resolve(strict=True)
    if args.run_dir:
        directories = [root]
    else:
        suite_path = root / "suite.json"
        if suite_path.is_file():
            suite = read_json(suite_path)
            directories = sorted({(root / item["dir"]).resolve()
                                  for item in suite.get("runs", [])})
        else:
            directories = sorted(path.resolve() for path in (root / "runs").iterdir()
                                 if path.is_dir()) if (root / "runs").is_dir() else []
        if not directories:
            parser.error("suite contains no runs/*/run.json")
        if any(not path.is_relative_to(root) for path in directories):
            parser.error("run directory escapes suite")
    report = build_report(directories)
    output = (args.output_dir or root / "analysis-report").resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name, content in (("report.json", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n"),
                          ("report.html", render_html(report))):
        destination = output / name
        temporary = output / f".{name}.{os.getpid()}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    print(output / "report.html")
    failed = any(run["status"] not in ("completed", "smoke-completed", "warmup-completed") for run in report["runs"])
    failed |= any(condition["status"] != "completed" for condition in report["conditions"])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
