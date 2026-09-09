from __future__ import annotations

import contextlib
import csv
import io
import json
import statistics
import tempfile
import unittest
from pathlib import Path
from typing import cast

from analysis import aggregate_runs


class AggregateRunsTest(unittest.TestCase):
    def run_ok(self, arguments: list[str]) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return aggregate_runs.main(arguments)

    def assert_fails(self, arguments: list[str], expected: str) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as context:
                aggregate_runs.main(arguments)
        self.assertEqual(context.exception.code, 1)
        self.assertIn(expected, stderr.getvalue())

    def metric_summary(self, values: list[int], rows: int) -> dict[str, object]:
        ordered = sorted(values)
        return {
            "unit": "ns",
            "count": len(ordered),
            "missing_count": rows - len(ordered),
            "negative_count": sum(value < 0 for value in ordered),
            "min_ns": ordered[0] if ordered else None,
            "max_ns": ordered[-1] if ordered else None,
            "mean_ns": statistics.fmean(ordered) if ordered else None,
            "median_ns": statistics.median(ordered) if ordered else None,
            "p95_ns": (
                aggregate_runs.percentile_nearest_rank(ordered, 95)
                if ordered else None
            ),
            "p99_ns": (
                aggregate_runs.percentile_nearest_rank(ordered, 99)
                if ordered else None
            ),
        }

    def make_run(
        self,
        root: Path,
        directory_name: str,
        run_id: str,
        rows: list[dict[str, int | None]],
        *,
        condition: str = "rt",
        nested_outputs: bool = False,
    ) -> Path:
        run_directory = root / directory_name
        run_directory.mkdir()
        manifest_path = run_directory / "manifest.json"
        output_directory = run_directory / "analysis" if nested_outputs else run_directory
        output_directory.mkdir(exist_ok=True)
        joined_path = output_directory / "joined.csv"
        summary_path = output_directory / "summary.json"
        marker_path = output_directory / "summary.json.complete"

        manifest = {
            "manifest_version": 1,
            "run_id": run_id,
            "parameters": {"condition": condition, "instrumentation": "on"},
            "interface": {"name": "eth0"},
            "system": {"kernel_release": "6.18.0-rt"},
            "project_repository": {"head": "project-head", "status": ""},
            "kernel_repository": {"head": "kernel-head", "status": ""},
            "artifact_hashes": [],
            "artifact_tree_hashes": [],
        }
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
        )
        ingress_path = run_directory / "ingress.csv"
        calibration_path = run_directory / "phc.csv"
        ingress_path.write_text("fixture ingress\n", encoding="utf-8")
        calibration_path.write_text("fixture calibration\n", encoding="utf-8")
        inputs = {
            "manifest": str(manifest_path.resolve()),
            "ingress": str(ingress_path.resolve()),
            "cpumap": None,
            "afxdp": None,
            "udp_native": None,
            "phc_calibration": [str(calibration_path.resolve())],
        }
        input_artifacts = {
            name: (
                None
                if value is None
                else [
                    {"path": item, **aggregate_runs.file_digest(Path(item))}
                    for item in value
                ]
                if isinstance(value, list)
                else {"path": value, **aggregate_runs.file_digest(Path(value))}
            )
            for name, value in inputs.items()
        }

        metric_columns = list(aggregate_runs.METRIC_FIELDS.values())
        fieldnames = ["flow_id", "sequence", *metric_columns]
        with joined_path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for sequence, supplied in enumerate(rows, start=1):
                row: dict[str, object] = {
                    "flow_id": 7,
                    "sequence": sequence,
                    **{field: "" for field in metric_columns},
                }
                row.update(
                    {
                        field: "" if value is None else value
                        for field, value in supplied.items()
                    }
                )
                writer.writerow(row)

        values_by_metric = {
            metric: [
                cast(int, row[field])
                for row in rows
                if row.get(field) is not None
            ]
            for metric, field in aggregate_runs.METRIC_FIELDS.items()
        }
        summary = {
            "schema_version": 3,
            "run": {
                "manifest": str(manifest_path.resolve()),
                "manifest_version": 1,
                "run_directory": str(run_directory.resolve()),
                "run_id": run_id,
            },
            "join_key": ["flow_id", "sequence"],
            "joined_rows": len(rows),
            "inputs": inputs,
            "input_artifacts": input_artifacts,
            "completion_marker": str(marker_path.resolve()),
            "sources": {
                "ingress": {"provided": True},
                "cpumap": {"provided": False},
                "afxdp": {"provided": False},
                "udp": {"provided": False},
            },
            "calibration": {
                "outside_range_policy": "exclude-clock-crossing-metrics",
                "inside_range_policy": "linear-interpolation",
            },
            "metrics": {
                metric: self.metric_summary(values, len(rows))
                for metric, values in values_by_metric.items()
            },
        }
        summary_path.write_text(
            json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8"
        )
        marker = {
            "schema_version": 2,
            "completion_schema_version": 2,
            "artifact_kind": "ace-joined-run",
            "run_id": run_id,
            "summary_schema_version": 3,
            "inputs": input_artifacts,
            "outputs": {
                "joined_csv": {
                    "path": str(joined_path.resolve()),
                    **aggregate_runs.file_digest(joined_path),
                },
                "summary_json": {
                    "path": str(summary_path.resolve()),
                    **aggregate_runs.file_digest(summary_path),
                },
            },
        }
        marker_path.write_text(
            json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8"
        )
        return marker_path

    def test_aggregate_percentiles_deadlines_and_exclusive_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_run(
                root,
                "one",
                "run-a",
                [
                    {
                        "xdp_hw_ns": -10,
                        "user_xdp_ns": 80,
                        "user_hw_ns": 100,
                    },
                    {
                        "xdp_hw_ns": 20,
                        "user_xdp_ns": 90,
                        "user_hw_ns": 300,
                    },
                ],
            )
            second = self.make_run(
                root,
                "two",
                "run-b",
                [
                    {"xdp_hw_ns": 30, "user_hw_ns": 200},
                    {"xdp_hw_ns": 40, "user_hw_ns": 500},
                ],
                nested_outputs=True,
            )
            output_csv = root / "aggregate.csv"
            output_json = root / "aggregate.json"
            marker = root / "aggregate.json.complete"

            result = self.run_ok([
                "--run", str(second),
                "--run", str(first),
                "--deadline-ns", "200",
                "--output-csv", str(output_csv),
                "--summary-json", str(output_json),
            ])
            self.assertEqual(result, 0)
            summary = json.loads(output_json.read_text(encoding="utf-8"))
            self.assertEqual(summary["run_ids"], ["run-a", "run-b"])
            self.assertEqual(summary["run_count"], 2)
            self.assertEqual(
                summary["confidence_intervals"]["unit_of_independence"],
                "run",
            )

            xdp = summary["metrics"]["xdp-hw"]
            self.assertEqual(xdp["run_level"]["with_samples_count"], 2)
            self.assertEqual(xdp["packet_level"]["count"], 4)
            self.assertEqual(xdp["packet_level"]["negative_count"], 1)
            self.assertEqual(xdp["packet_level"]["p50_ns"], 20)
            self.assertEqual(xdp["packet_level"]["p95_ns"], 40)
            self.assertEqual(xdp["packet_level"]["p99_ns"], 40)
            self.assertEqual(xdp["packet_level"]["max_ns"], 40)

            user_hw = summary["metrics"]["user-hw"]
            self.assertEqual(user_hw["packet_level"]["p50_ns"], 200)
            self.assertEqual(user_hw["packet_level"]["deadline_miss_count"], 2)
            self.assertEqual(user_hw["packet_level"]["deadline_miss_rate"], 0.5)
            self.assertEqual(
                user_hw["run_level"]["with_deadline_miss_count"], 2
            )
            self.assertEqual(
                user_hw["run_level"]["with_deadline_miss_rate"], 1.0
            )
            estimates = user_hw["run_level"]["independent_run_estimates"]
            self.assertEqual(estimates["p99_ns"]["sample_count"], 2)
            self.assertEqual(estimates["p99_ns"]["mean"], 400.0)
            self.assertAlmostEqual(
                estimates["p99_ns"]["ci95_low"], -870.62047364
            )
            self.assertAlmostEqual(
                estimates["p99_ns"]["ci95_high"], 1670.62047364
            )
            self.assertEqual(estimates["deadline_miss_rate"]["mean"], 0.5)
            self.assertEqual(estimates["deadline_miss_rate"]["ci95_low"], 0.5)
            self.assertEqual(estimates["deadline_miss_rate"]["ci95_high"], 0.5)

            one_run_ci = summary["metrics"]["user-xdp"]["run_level"][
                "independent_run_estimates"
            ]["p99_ns"]
            self.assertEqual(one_run_ci["sample_count"], 1)
            self.assertEqual(one_run_ci["mean"], 90.0)
            self.assertIsNone(one_run_ci["sample_stddev"])
            self.assertIsNone(one_run_ci["ci95_low"])
            self.assertIsNone(one_run_ci["ci95_high"])
            empty = summary["metrics"]["cpumap-xdp"]
            self.assertEqual(empty["run_level"]["without_samples_count"], 2)
            self.assertIsNone(empty["packet_level"]["p99_ns"])
            self.assertIsNone(empty["packet_level"]["deadline_miss_rate"])

            with output_csv.open(encoding="utf-8", newline="") as file:
                csv_rows = {row["metric"]: row for row in csv.DictReader(file)}
            self.assertEqual(csv_rows["user-hw"]["deadline_miss_count"], "2")
            self.assertEqual(csv_rows["user-hw"]["run_p99_mean_ns"], "400.0")
            self.assertEqual(csv_rows["xdp-hw"]["p50_ns"], "20")

            completion = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(
                completion["artifact_kind"], "ace-repeated-run-aggregation"
            )
            self.assertEqual(completion["run_ids"], ["run-a", "run-b"])
            self.assertEqual(
                completion["outputs"]["aggregate_csv"]["sha256"],
                aggregate_runs.file_digest(output_csv)["sha256"],
            )

            original_json = output_json.read_bytes()
            self.assert_fails([
                "--run", str(first),
                "--run", str(second),
                "--deadline-ns", "200",
                "--output-csv", str(output_csv),
                "--summary-json", str(output_json),
            ], "refusing to overwrite")
            self.assertEqual(output_json.read_bytes(), original_json)

    def test_rejects_tampered_and_duplicate_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_run(
                root, "one", "same-id", [{"xdp_hw_ns": 10}]
            )
            second = self.make_run(
                root, "two", "same-id", [{"xdp_hw_ns": 20}]
            )
            output_csv = root / "duplicate.csv"
            output_json = root / "duplicate.json"
            self.assert_fails([
                "--run", str(first),
                "--run", str(second),
                "--deadline-ns", "10",
                "--output-csv", str(output_csv),
                "--summary-json", str(output_json),
            ], "duplicate run_id")
            self.assertFalse(output_csv.exists())
            self.assertFalse(output_json.exists())

            joined = root / "two" / "joined.csv"
            joined.write_text(joined.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            self.assert_fails([
                "--run", str(first),
                "--run", str(second),
                "--deadline-ns", "10",
                "--output-csv", str(output_csv),
                "--summary-json", str(output_json),
            ], "digest mismatch")
            self.assertFalse(output_csv.exists())

    def test_rejects_mixed_cohorts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_run(
                root, "one", "run-a", [{"user_hw_ns": 100}], condition="rt"
            )
            second = self.make_run(
                root, "two", "run-b", [{"user_hw_ns": 100}], condition="vanilla"
            )
            output_csv = root / "mixed.csv"
            output_json = root / "mixed.json"
            self.assert_fails([
                "--run", str(first),
                "--run", str(second),
                "--deadline-ns", "100",
                "--output-csv", str(output_csv),
                "--summary-json", str(output_json),
            ], "mixed manifest parameters")
            self.assertFalse(output_csv.exists())
            self.assertFalse(output_json.exists())

    def test_input_provenance_and_source_presence_are_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_run(root, "one", "run-a", [{"xdp_hw_ns": 10}])
            second = self.make_run(root, "two", "run-b", [{"xdp_hw_ns": 20}])

            def arguments(stem: str) -> list[str]:
                return [
                    "--run", str(first), "--run", str(second),
                    "--deadline-ns", "10",
                    "--output-csv", str(root / f"{stem}.csv"),
                    "--summary-json", str(root / f"{stem}.json"),
                ]

            manifest = root / "two" / "manifest.json"
            manifest_original = manifest.read_bytes()
            manifest.write_bytes(manifest_original + b" ")
            self.assert_fails(arguments("manifest-tamper"), "digest mismatch")
            self.assertFalse((root / "manifest-tamper.csv").exists())
            manifest.write_bytes(manifest_original)

            ingress = root / "two" / "ingress.csv"
            ingress_original = ingress.read_bytes()
            ingress.write_bytes(ingress_original + b"tampered\n")
            self.assert_fails(arguments("input-tamper"), "digest mismatch")
            ingress.write_bytes(ingress_original)

            marker_original = second.read_bytes()
            marker = json.loads(marker_original)
            marker["inputs"]["manifest"]["sha256"] = "0" * 64
            second.write_text(
                json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8"
            )
            self.assert_fails(arguments("marker-mismatch"), "disagree")
            second.write_bytes(marker_original)

            summary_path = root / "two" / "summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["sources"]["cpumap"]["provided"] = True
            summary_path.write_text(
                json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8"
            )
            marker = json.loads(second.read_text(encoding="utf-8"))
            marker["outputs"]["summary_json"].update(
                aggregate_runs.file_digest(summary_path)
            )
            second.write_text(
                json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8"
            )
            self.assert_fails(arguments("source-mismatch"), "source presence")


if __name__ == "__main__":
    unittest.main()
