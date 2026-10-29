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
import shutil
import statistics
import sys
from pathlib import Path
from typing import Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis import aggregate_runs as aggregate
from analysis import join_results as join


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
            ("case_id", "mode", "duration_s", "flows", "load", "config")}


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
    if descriptor.get("mode") not in ("mixed", "all-pass", "socket-only"):
        raise ExperimentError("invalid run mode")
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
    return descriptor


def join_completed_run(directory: Path, descriptor: Mapping) -> aggregate.RunData:
    output = directory / "analysis"
    output.mkdir(exist_ok=True)
    marker = output / "summary.json.complete"
    if marker.exists():
        run = aggregate.load_run(marker)
        if run.run_id != descriptor["run_id"]:
            raise ExperimentError("join run_id disagrees with run.json")
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
    for name in ("phc-before.csv", "phc-after.csv"):
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
        result["delivery"] = delivery_statistics(directory, descriptor, run)
        result["cpu"] = sysstat_summary(directory / "monitor" / "mpstat.log", "cpu")
        result["network"] = sysstat_summary(directory / "monitor" / "network.log", "network")
        sender_manifest = directory / "sender" / "manifest.json"
        result["sender_cohort"] = (aggregate.manifest_cohort_identity(read_json(sender_manifest))
                                    if sender_manifest.is_file() else None)
        # Bind the report to each consumed raw sidecar as well as the joiner's inputs.
        artifact_names = ["run.json", ".complete"]
        artifact_names += [f"sender-flow{flow['flow_id']}.json" for flow in descriptor["flows"]]
        artifact_names += [path.name for path in directory.glob("receiver-flow*.json")]
        artifact_names += [path.name for path in directory.glob("udp-flow*.bin")]
        artifact_names += [name for name in ("monitor/mpstat.log", "monitor/network.log")
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
    groups: dict[tuple[str, str], list[tuple[dict, aggregate.RunData, dict]]] = {}
    for directory in directories:
        result, loaded, descriptor = inspect_run(directory)
        runs.append(result)
        if loaded is not None and descriptor is not None:
            key = (str(descriptor.get("case_id", "default")), descriptor["mode"])
            groups.setdefault(key, []).append((result, loaded, descriptor))
    conditions = []
    for (case_id, mode), entries in sorted(groups.items()):
        condition = {"case_id": case_id, "mode": mode, "status": "completed"}
        try:
            descriptors = [entry[2] for entry in entries]
            comparable = lambda d: {key: d.get(key) for key in ("flows", "load", "duration_s", "mode")}
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
                             cpu_busy_pct=aggregate.mean_confidence_interval_95([
                                 item for item in cpu if item is not None]))
        except (ExperimentError, aggregate.InputError, OSError, ValueError) as error:
            condition.update(status="aggregation-failed", reason=str(error))
        conditions.append(condition)
    return {"schema_version": 1, "artifact_kind": "ace-experiment-report",
            "runs": runs, "conditions": conditions,
            "methods": {"latency": "NIC hardware RX to userspace, PHC/MONOTONIC calibrated",
                        "percentiles": "nearest-rank", "ci": "equal-run-weight two-sided Student-t 95%",
                        "delivery_window": "first attempted send to sender end; includes delivery during final drain",
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
        label = f'{condition["case_id"]} / {condition["mode"]}'
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
    parts.extend((bars("Run p99 평균", latency_bars, "µs"), bars("패킷 손실률", loss_bars, "%"), bars("전체 CPU 평균 사용률", cpu_bars, "%")))
    parts.append('<section><h2>Run별 송수신·부하</h2><table><tr><th>Run / flow</th><th>계획 / 실제 송신 / 수신</th><th>송신 오류</th><th>손실</th><th>실제 송신 PPS</th><th>CPU 사용률</th></tr>')
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
