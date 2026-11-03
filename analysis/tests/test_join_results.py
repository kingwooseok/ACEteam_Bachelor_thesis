from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path

from analysis import join_results


INGRESS_HEADER = (
    "flow_id,sequence,rx_queue,hw_rx_ns,initial_xdp_ns,flags,"
    "requested_path,timestamp_error\n"
)
CPUMAP_HEADER = "flow_id,sequence,cpu,cpumap_ns,flags,requested_path\n"
AFXDP_HEADER = (
    "flow_id,sequence,rx_queue,tx_realtime_ns,user_rx_mono_ns,"
    "user_rx_real_ns,hw_rx_ns,initial_xdp_ns,meta_flags,"
    "requested_path,timestamp_error,packet_length,validation_flags\n"
)
CALIBRATION_HEADER = (
    "iteration,sample,system_before_ns,phc_ns,system_after_ns,"
    "span_ns,offset_ns,selected\n"
)


class JoinResultsTest(unittest.TestCase):
    def test_reserved_output_preserves_bytes_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "output.csv"
            descriptor = join_results.reserve_output(path)
            try:
                self.assertFalse(os.get_inheritable(descriptor))
                os.write(descriptor, b"flow_id,sequence\r\n1,2\r\n")
            finally:
                os.close(descriptor)
            self.assertEqual(path.read_bytes(), b"flow_id,sequence\r\n1,2\r\n")
            with self.assertRaises(FileExistsError):
                join_results.reserve_output(path)
            self.assertEqual(path.read_bytes(), b"flow_id,sequence\r\n1,2\r\n")

    def write_text(self, path: Path, contents: str) -> None:
        path.write_text(contents, encoding="utf-8")

    def make_run(self, temporary: str, run_id: str = "run-001") -> Path:
        run = Path(temporary) / "run-001"
        run.mkdir()
        self.write_text(
            run / "manifest.json",
            json.dumps({"manifest_version": 1, "run_id": run_id}) + "\n",
        )
        return run

    def write_calibration(
        self, path: Path, points: list[tuple[int, int]]
    ) -> None:
        lines = [CALIBRATION_HEADER]
        for iteration, (midpoint, offset) in enumerate(points):
            before = midpoint - 10
            after = midpoint + 10
            lines.append(
                f"{iteration},0,{before},{midpoint + offset},{after},"
                f"20,{offset},1\n"
            )
        self.write_text(path, "".join(lines))

    def arguments(
        self,
        run: Path,
        ingress: Path,
        calibrations: list[Path],
        stem: str = "result",
        cpumap: Path | None = None,
        afxdp: Path | None = None,
        udp: Path | None = None,
        clamp: bool = False,
    ) -> list[str]:
        result = [
            "--manifest", str(run / "manifest.json"),
            "--ingress", str(ingress),
        ]
        if cpumap is not None:
            result.extend(("--cpumap", str(cpumap)))
        if afxdp is not None:
            result.extend(("--afxdp", str(afxdp)))
        if udp is not None:
            result.extend(("--udp-native", str(udp)))
        for calibration in calibrations:
            result.extend(("--phc-calibration", str(calibration)))
        if clamp:
            result.append("--allow-endpoint-clamp")
        result.extend((
            "--output-csv", str(run / f"{stem}.csv"),
            "--summary-json", str(run / f"{stem}.json"),
        ))
        return result

    def run_ok(self, arguments: list[str]) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return join_results.main(arguments)

    def assert_run_fails(
        self, arguments: list[str], text: str | None = None, code: int = 1
    ) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as context:
                join_results.main(arguments)
        self.assertEqual(context.exception.code, code)
        if text is not None:
            self.assertIn(text, stderr.getvalue())

    def read_rows(self, path: Path) -> list[dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as file:
            return list(csv.DictReader(file))

    def test_full_join_interpolation_metrics_duplicates_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = self.make_run(temporary)
            ingress = run / "ingress.csv"
            cpumap = run / "cpumap.csv"
            afxdp = run / "afxdp.csv"
            udp = run / "udp.bin"
            before = run / "phc-before.csv"
            after = run / "phc-after.csv"

            self.write_text(
                ingress,
                INGRESS_HEADER
                + "1,1,0,990,900,19,0,0\n"
                + "1,1,0,999999,900,19,0,0\n"
                + "1,2,0,1330,1200,19,1,0\n"
                + "1,3,0,2185,2000,19,2,0\n"
                + "1,4,0,2730,2500,19,0,0\n"
                + "1,5,0,2860,2600,19,1,0\n",
            )
            self.write_text(
                cpumap,
                CPUMAP_HEADER
                + "1,2,2,1190,23,1\n"
                + "1,5,2,2630,23,1\n"
                + "9,9,3,5000,22,1\n",
            )
            self.write_text(
                afxdp,
                AFXDP_HEADER
                + "1,3,0,10000,2020,20000,2185,2000,19,2,0,138,7\n",
            )
            udp.write_bytes(
                struct.pack(
                    "@QqqqqIIHHI", 4, 11000, 2540, 21000, 2730,
                    1, 96, 1, 0, 0,
                )
                + struct.pack(
                    "@QqqqqIIHHI", 5, 12000, 2650, 22000, 2860,
                    1, 96, 1, 0, 0,
                )
            )
            self.write_calibration(before, [(800, 100), (1000, 100)])
            self.write_calibration(after, [(3000, 300)])

            arguments = self.arguments(
                run, ingress, [before, after], cpumap=cpumap,
                afxdp=afxdp, udp=udp,
            )
            self.assertEqual(self.run_ok(arguments), 0)

            joined = run / "result.csv"
            summary_path = run / "result.json"
            marker_path = run / "result.json.complete"
            rows = self.read_rows(joined)
            by_key = {
                (int(row["flow_id"]), int(row["sequence"])): row for row in rows
            }
            self.assertEqual(len(rows), 6)
            self.assertEqual(by_key[(1, 1)]["xdp_hw_ns"], "10")
            self.assertEqual(by_key[(1, 2)]["xdp_hw_ns"], "-10")
            self.assertEqual(by_key[(1, 2)]["cpumap_xdp_ns"], "-10")
            self.assertEqual(by_key[(1, 3)]["user_source"], "afxdp")
            self.assertEqual(by_key[(1, 3)]["xdp_hw_ns"], "15")
            self.assertEqual(by_key[(1, 3)]["user_xdp_ns"], "20")
            self.assertEqual(by_key[(1, 3)]["user_hw_ns"], "35")
            self.assertEqual(by_key[(1, 4)]["user_source"], "udp")
            self.assertEqual(by_key[(1, 4)]["user_xdp_ns"], "40")
            self.assertEqual(by_key[(1, 4)]["user_hw_ns"], "60")
            self.assertEqual(by_key[(1, 5)]["cpumap_xdp_ns"], "30")
            self.assertEqual(by_key[(1, 5)]["user_cpumap_ns"], "20")
            self.assertEqual(by_key[(1, 5)]["user_hw_ns"], "50")
            self.assertEqual(by_key[(9, 9)]["ingress_present"], "0")
            self.assertEqual(by_key[(9, 9)]["cpumap_ns"], "5000")
            self.assertTrue(all(row["inconsistency_flags"] == "0" for row in rows))

            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["schema_version"], 4)
            self.assertEqual(summary["calibration"]["hw_timestamp_policy"], join_results.HW_TIMESTAMP_POLICY)
            self.assertEqual(summary["decomposition"]["counts"],
                             {"complete": 3, "incomplete": 2, "invalid": 1, "mismatch": 0})
            self.assertEqual(summary["decomposition"]["max_abs_residual_ns"], 0)
            self.assertEqual(summary["run"]["run_id"], "run-001")
            self.assertEqual(summary["joined_rows"], 6)
            self.assertEqual(summary["duplicates"]["ingress"], 1)
            self.assertEqual(summary["duplicates"]["total"], 1)
            self.assertEqual(summary["sources"]["ingress"]["conflicting_duplicate_rows"], 1)
            self.assertEqual(summary["metrics"]["xdp-hw"]["count"], 5)
            self.assertEqual(summary["metrics"]["xdp-hw"]["negative_count"], 1)
            self.assertEqual(summary["metrics"]["cpumap-xdp"]["count"], 2)
            self.assertEqual(summary["metrics"]["user-cpumap"]["count"], 1)
            self.assertEqual(summary["metrics"]["user-xdp"]["count"], 3)
            self.assertEqual(summary["metrics"]["user-hw"]["count"], 3)
            self.assertEqual(len(summary["calibration"]["files"]), 2)
            self.assertEqual(summary["calibration"]["unique_selected_times"], 3)
            self.assertEqual(summary["calibration"]["lookups"]["out_of_range"], 0)
            self.assertEqual(summary["inconsistencies"]["total"], 0)

            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["schema_version"], 2)
            self.assertEqual(marker["completion_schema_version"], 2)
            self.assertEqual(marker["artifact_kind"], "ace-joined-run")
            self.assertEqual(marker["run_id"], "run-001")
            self.assertEqual(marker["summary_schema_version"], 4)
            self.assertEqual(marker["inputs"], summary["input_artifacts"])
            self.assertEqual(len(marker["inputs"]["phc_calibration"]), 2)
            for name, path in (
                ("joined_csv", joined), ("summary_json", summary_path)
            ):
                contents = path.read_bytes()
                self.assertEqual(marker["outputs"][name]["bytes"], len(contents))
                self.assertEqual(
                    marker["outputs"][name]["sha256"],
                    hashlib.sha256(contents).hexdigest(),
                )

            self.assert_run_fails(arguments, "already complete")

    def test_out_of_range_default_exclusion_and_explicit_clamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = self.make_run(temporary)
            ingress = run / "ingress.csv"
            udp = run / "udp.bin"
            calibration = run / "phc.csv"
            self.write_text(
                ingress, INGRESS_HEADER + "1,1,0,600,500,19,0,0\n"
            )
            udp.write_bytes(struct.pack(
                "@QqqqqIIHHI", 1, 10, 3500, 20, 600, 1, 96, 1, 0, 0
            ))
            self.write_calibration(calibration, [(1000, 100), (3000, 300)])

            default_args = self.arguments(
                run, ingress, [calibration], stem="default", udp=udp
            )
            self.assertEqual(self.run_ok(default_args), 0)
            row = self.read_rows(run / "default.csv")[0]
            self.assertEqual(row["calibration_xdp_position"], "before")
            self.assertEqual(row["calibration_xdp_out_of_range"], "1")
            self.assertEqual(row["calibration_xdp_distance_ns"], "500")
            self.assertEqual(row["calibration_user_position"], "after")
            self.assertEqual(row["calibration_user_distance_ns"], "500")
            self.assertEqual(row["calibration_offset_at_xdp_ns"], "")
            self.assertEqual(row["calibration_offset_at_user_ns"], "")
            self.assertEqual(row["xdp_hw_ns"], "")
            self.assertEqual(row["user_hw_ns"], "")
            self.assertEqual(row["user_xdp_ns"], "3000")
            summary = json.loads((run / "default.json").read_text())
            self.assertEqual(
                summary["calibration"]["outside_range_policy"],
                "exclude-clock-crossing-metrics",
            )
            self.assertEqual(summary["calibration"]["lookups"]["before"], 1)
            self.assertEqual(summary["calibration"]["lookups"]["after"], 1)
            self.assertEqual(summary["calibration"]["lookups"]["out_of_range"], 2)
            self.assertEqual(summary["calibration"]["lookups"]["max_distance_ns"], 500)

            clamp_args = self.arguments(
                run, ingress, [calibration], stem="clamp", udp=udp, clamp=True
            )
            self.assertEqual(self.run_ok(clamp_args), 0)
            clamped = self.read_rows(run / "clamp.csv")[0]
            self.assertEqual(clamped["calibration_offset_at_xdp_ns"], "100")
            self.assertEqual(clamped["calibration_offset_at_user_ns"], "300")
            self.assertEqual(clamped["xdp_hw_ns"], "0")
            self.assertEqual(clamped["user_hw_ns"], "3000")
            self.assertEqual(clamped["hw_calibration_offset_ns"], "100")
            self.assertEqual(clamped["segment_residual_ns"], "0")
            clamp_summary = json.loads((run / "clamp.json").read_text())
            self.assertEqual(
                clamp_summary["calibration"]["outside_range_policy"],
                "endpoint-clamp",
            )

    def test_multiple_calibrations_merge_duplicates_and_reject_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.csv"
            duplicate = root / "duplicate.csv"
            conflict = root / "conflict.csv"
            self.write_calibration(first, [(1000, 100), (2000, 200)])
            self.write_calibration(duplicate, [(2000, 200), (3000, 300)])
            self.write_calibration(conflict, [(2000, 201)])

            calibration = join_results.load_calibration([first, duplicate])
            self.assertEqual(calibration.times, (1000, 2000, 3000))
            self.assertEqual(calibration.duplicate_selected_times, 1)
            self.assertEqual(calibration.rows, 4)
            self.assertEqual(calibration.lookup(2500).offset_ns, 250)
            with self.assertRaisesRegex(join_results.InputError, "conflicting selected"):
                join_results.load_calibration([first, conflict])

    def test_validated_afxdp_metadata_fallback_and_raw_invalid_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = self.make_run(temporary)
            ingress = run / "ingress.csv"
            afxdp = run / "afxdp.csv"
            calibration = run / "phc.csv"
            self.write_text(ingress, INGRESS_HEADER)
            self.write_text(
                afxdp,
                AFXDP_HEADER
                + "1,1,0,10,1600,20,1650,1500,19,2,0,128,7\n"
                + "1,2,0,11,1650,21,1700,1550,19,2,0,129,3\n"
                + "1,3,0,12,1750,22,0,1700,26,2,-95,130,7\n",
            )
            self.write_calibration(calibration, [(1000, 100), (2000, 200)])

            self.assertEqual(self.run_ok(self.arguments(
                run, ingress, [calibration], afxdp=afxdp
            )), 0)
            rows = self.read_rows(run / "result.csv")
            by_sequence = {int(row["sequence"]): row for row in rows}
            valid = by_sequence[1]
            self.assertEqual(valid["initial_xdp_source"], "afxdp")
            self.assertEqual(valid["initial_xdp_ns"], "1500")
            self.assertEqual(valid["xdp_hw_ns"], "0")
            self.assertEqual(valid["user_xdp_ns"], "100")
            self.assertEqual(valid["user_hw_ns"], "100")
            self.assertEqual(valid["hw_rx_source"], "afxdp")
            self.assertEqual(valid["hw_calibration_reference"], "xdp")
            self.assertEqual(valid["segment_status"], "complete")

            invalid = by_sequence[2]
            self.assertEqual(invalid["afxdp_initial_xdp_ns"], "1550")
            self.assertEqual(invalid["afxdp_hw_rx_ns"], "1700")
            self.assertEqual(invalid["initial_xdp_ns"], "")
            self.assertEqual(invalid["xdp_hw_ns"], "")
            self.assertTrue(
                int(invalid["inconsistency_flags"])
                & join_results.INCONSISTENT_AF_XDP_METADATA
            )

            timestamp_error = by_sequence[3]
            self.assertEqual(timestamp_error["initial_xdp_source"], "afxdp")
            self.assertEqual(timestamp_error["user_xdp_ns"], "50")
            self.assertEqual(timestamp_error["xdp_hw_ns"], "")
            self.assertEqual(timestamp_error["user_hw_ns"], "")
            summary = json.loads((run / "result.json").read_text())
            self.assertEqual(summary["inconsistencies"]["afxdp_metadata"], 1)
            self.assertEqual(summary["metrics"]["xdp-hw"]["count"], 1)
            self.assertEqual(summary["metrics"]["user-xdp"]["count"], 2)
            self.assertEqual(summary["metrics"]["user-hw"]["count"], 1)

    def test_semantic_inconsistency_bitmask_counts_and_raw_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = self.make_run(temporary)
            ingress = run / "ingress.csv"
            cpumap = run / "cpumap.csv"
            afxdp = run / "afxdp.csv"
            udp = run / "udp.bin"
            calibration = run / "phc.csv"
            self.write_text(
                ingress,
                INGRESS_HEADER
                + "1,1,0,1150,1000,19,9,0\n"
                + "1,2,0,1260,1100,19,1,0\n"
                + "1,3,0,1370,1200,19,0,0\n"
                + "1,4,0,1480,1300,19,2,0\n"
                + "1,5,0,0,1400,19,0,0\n",
            )
            self.write_text(
                cpumap, CPUMAP_HEADER + "1,2,2,1120,0,0\n"
            )
            self.write_text(
                afxdp,
                AFXDP_HEADER
                + "1,3,0,10,1250,20,1370,1200,19,0,0,128,7\n",
            )
            udp.write_bytes(
                struct.pack(
                    "@QqqqqIIHHI", 3, 10, 1260, 20, 1370, 1, 96, 1, 0, 0
                )
                + struct.pack(
                    "@QqqqqIIHHI", 4, 10, 1360, 20, 1480, 1, 96, 1, 0, 0
                )
                + struct.pack(
                    "@QqqqqIIHHI", 5, 10, 1460, 20, 0, 1, 96, 2, 0, 0
                )
            )
            self.write_calibration(calibration, [(900, 90), (2000, 200)])

            self.assertEqual(self.run_ok(self.arguments(
                run, ingress, [calibration], cpumap=cpumap,
                afxdp=afxdp, udp=udp,
            )), 0)
            rows = {int(row["sequence"]): row
                    for row in self.read_rows(run / "result.csv")}
            self.assertEqual(rows[1]["ingress_initial_xdp_ns"], "1000")
            self.assertEqual(rows[1]["initial_xdp_ns"], "")
            self.assertEqual(rows[2]["cpumap_ns"], "1120")
            self.assertEqual(rows[2]["cpumap_xdp_ns"], "")
            self.assertEqual(rows[3]["afxdp_user_rx_mono_ns"], "1250")
            self.assertEqual(rows[3]["udp_user_rx_mono_ns"], "1260")
            self.assertEqual(rows[3]["user_rx_mono_ns"], "")
            self.assertEqual(rows[4]["user_xdp_ns"], "")
            self.assertEqual(rows[5]["ingress_hw_rx_ns"], "0")
            self.assertEqual(rows[5]["xdp_hw_ns"], "")
            self.assertEqual(rows[5]["user_xdp_ns"], "60")

            summary = json.loads((run / "result.json").read_text())
            counts = summary["inconsistencies"]
            self.assertEqual(counts["ingress_path"], 1)
            self.assertEqual(counts["ingress_flags"], 2)
            self.assertEqual(counts["cpumap_ingress_path"], 1)
            self.assertEqual(counts["cpumap_path"], 1)
            self.assertEqual(counts["cpumap_flags"], 1)
            self.assertEqual(counts["afxdp_path"], 1)
            self.assertEqual(counts["afxdp_metadata"], 1)
            self.assertEqual(counts["ambiguous_user"], 1)
            self.assertEqual(counts["udp_path"], 1)
            self.assertEqual(counts["rows"], 5)
            self.assertEqual(counts["total"], 10)

    def test_manifest_validation_and_run_directory_containment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = self.make_run(temporary)
            ingress = run / "ingress.csv"
            calibration = run / "phc.csv"
            self.write_text(ingress, INGRESS_HEADER)
            self.write_calibration(calibration, [(1000, 100)])

            bad_version = run / "bad-version.json"
            self.write_text(
                bad_version, '{"manifest_version": 2, "run_id": "run-001"}\n'
            )
            arguments = self.arguments(run, ingress, [calibration])
            arguments[1] = str(bad_version)
            self.assert_run_fails(arguments, "manifest_version")

            bad_id = run / "bad-id.json"
            self.write_text(
                bad_id, '{"manifest_version": 1, "run_id": "../escape"}\n'
            )
            arguments = self.arguments(
                run, ingress, [calibration], stem="bad-id"
            )
            arguments[1] = str(bad_id)
            self.assert_run_fails(arguments, "invalid run_id")

            outside_ingress = root / "outside-ingress.csv"
            self.write_text(outside_ingress, INGRESS_HEADER)
            self.assert_run_fails(
                self.arguments(
                    run, outside_ingress, [calibration], stem="outside"
                ),
                "escapes manifest run directory",
            )

    def test_symlink_run_directory_containment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = self.make_run(temporary)
            ingress = run / "ingress.csv"
            calibration = run / "phc.csv"
            self.write_text(ingress, INGRESS_HEADER)
            self.write_calibration(calibration, [(1000, 100)])
            outside_ingress = root / "outside-ingress.csv"
            self.write_text(outside_ingress, INGRESS_HEADER)
            symlink_input = run / "symlink-ingress.csv"
            try:
                symlink_input.symlink_to(outside_ingress)
            except OSError as error:
                if getattr(error, "winerror", None) == 1314:
                    self.skipTest("Windows account cannot create symbolic links")
                raise
            self.assert_run_fails(
                self.arguments(
                    run, symlink_input, [calibration], stem="symlink-input"
                ),
                "escapes manifest run directory",
            )

            outside_directory = root / "outside-output"
            outside_directory.mkdir()
            output_link = run / "output-link"
            output_link.symlink_to(outside_directory, target_is_directory=True)
            output_arguments = self.arguments(
                run, ingress, [calibration], stem="safe-input"
            )
            output_index = output_arguments.index("--output-csv") + 1
            output_arguments[output_index] = str(output_link / "joined.csv")
            self.assert_run_fails(
                output_arguments, "escapes manifest run directory"
            )

            dangling_target = run / "eventual-output.csv"
            dangling_output = run / "dangling-output.csv"
            dangling_output.symlink_to(dangling_target)
            output_arguments = self.arguments(
                run, ingress, [calibration], stem="safe-output"
            )
            output_index = output_arguments.index("--output-csv") + 1
            output_arguments[output_index] = str(dangling_output)
            self.assert_run_fails(output_arguments, "must not be a symbolic link")
            self.assertFalse(dangling_target.exists())

    def test_manifest_is_mandatory_and_duplicate_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = self.make_run(temporary)
            ingress = run / "ingress.csv"
            calibration = run / "phc.csv"
            self.write_text(ingress, INGRESS_HEADER)
            self.write_calibration(calibration, [(1000, 100)])

            arguments = self.arguments(run, ingress, [calibration])
            del arguments[0:2]
            self.assert_run_fails(arguments, "--manifest", code=2)

            duplicate = run / "duplicate-manifest.json"
            self.write_text(
                duplicate,
                '{"manifest_version":1,"run_id":"a","run_id":"b"}\n',
            )
            arguments = self.arguments(
                run, ingress, [calibration], stem="duplicate"
            )
            arguments[1] = str(duplicate)
            self.assert_run_fails(arguments, "duplicate JSON key")

    def test_incomplete_output_set_is_detected_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = self.make_run(temporary)
            ingress = run / "ingress.csv"
            calibration = run / "phc.csv"
            self.write_text(ingress, INGRESS_HEADER)
            self.write_calibration(calibration, [(1000, 100)])
            partial = run / "partial.csv"
            self.write_text(partial, "crash residue\n")

            arguments = self.arguments(
                run, ingress, [calibration], stem="partial"
            )
            self.assert_run_fails(arguments, "incomplete or corrupt prior output")
            self.assertEqual(partial.read_text(), "crash residue\n")
            self.assertFalse((run / "partial.json").exists())
            self.assertFalse((run / "partial.json.complete").exists())

            corrupt_csv = run / "corrupt.csv"
            corrupt_json = run / "corrupt.json"
            corrupt_marker = run / "corrupt.json.complete"
            self.write_text(corrupt_csv, "csv residue\n")
            self.write_text(corrupt_json, "{}\n")
            self.write_text(
                corrupt_marker,
                '{"completion_schema_version":1,"run_id":"run-001",'
                '"outputs":{"joined_csv":"not-an-object"}}\n',
            )
            self.assert_run_fails(
                self.arguments(
                    run, ingress, [calibration], stem="corrupt"
                ),
                "incomplete or corrupt prior output",
            )
            self.assertEqual(corrupt_csv.read_text(), "csv residue\n")

    def test_input_abi_validation_rejects_bad_csv_udp_and_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad_csv = root / "bad.csv"
            self.write_text(
                bad_csv,
                INGRESS_HEADER + "1,1,0,100,90,19,0,0,unexpected\n",
            )
            with self.assertRaisesRegex(join_results.InputError, "extra CSV fields"):
                join_results.load_csv_table(
                    bad_csv, join_results.required_columns()["ingress"]
                )

            bad_udp = root / "bad.bin"
            bad_udp.write_bytes(struct.pack(
                "@QqqqqIIHHI", 1, 2, 3, 4, 5, 6, 7, 1, 1, 0
            ))
            with self.assertRaisesRegex(join_results.InputError, "reserved field"):
                join_results.load_udp_records(bad_udp)

            bad_calibration = root / "bad-calibration.csv"
            self.write_text(
                bad_calibration,
                CALIBRATION_HEADER + "0,0,990,1100,1010,19,100,1\n",
            )
            with self.assertRaisesRegex(join_results.InputError, "system span"):
                join_results.load_calibration(bad_calibration)


class SharedHardwareClockTest(unittest.TestCase):
    def setUp(self):
        self.calibration = join_results.Calibration(
            (join_results.CalibrationPoint(1000, 100), join_results.CalibrationPoint(3000, 300)),
            (1000, 3000), (), 2, 2, 0)

    def row(self, path=1, *, cpumap_time=1800, xdp=1500, user=2000, hw=1600,
            ingress_present=True, user_present=True):
        ingress = dict(requested_path=path, flags=19, initial_xdp_ns=xdp,
                       hw_rx_ns=hw, timestamp_error=0, rx_queue=0) if ingress_present else None
        cpumap = dict(requested_path=1, flags=23, cpumap_ns=cpumap_time, cpu=3) if cpumap_time is not None and path == 1 else None
        udp = dict(user_rx_mono_ns=user, hw_rx_ns=hw, flags=1, packet_length=96) if user_present else None
        return join_results.joined_row((2, 7), ingress, cpumap, None, udp, self.calibration)

    def test_drift_uses_one_corrected_hw_value_for_all_segments(self):
        row = self.row()
        self.assertEqual(row["calibration_offset_at_xdp_ns"], 150)
        self.assertEqual(row["calibration_offset_at_user_ns"], 200)
        self.assertEqual(row["hw_rx_mono_ns"], 1450)
        self.assertEqual(row["hw_calibration_reference"], "xdp")
        self.assertEqual((row["d1_ns"], row["d2_ns"], row["d3_ns"]), (50, 300, 200))
        self.assertEqual(row["user_hw_ns"], 550)
        self.assertEqual(row["segment_sum_ns"], row["user_hw_ns"])
        self.assertEqual(row["segment_residual_ns"], 0)
        self.assertEqual(row["segment_status"], "complete")

    def test_pass_zero_dispatch_and_missing_cpumap_stays_missing(self):
        row = self.row(path=0)
        self.assertEqual((row["d1_ns"], row["d2_ns"], row["d3_ns"]), (50, 0, 500))
        self.assertIsNone(row["cpumap_xdp_ns"])
        self.assertEqual(row["segment_status"], "complete")
        row = self.row(cpumap_time=None)
        self.assertIsNone(row["d2_ns"])
        self.assertIsNone(row["d3_ns"])
        self.assertIsNone(row["segment_residual_ns"])
        self.assertEqual(row["segment_status"], "incomplete")

    def test_udp_without_xdp_uses_user_reference_only_for_total(self):
        row = self.row(path=0, ingress_present=False)
        self.assertEqual(row["hw_rx_source"], "udp")
        self.assertEqual(row["hw_calibration_reference"], "user")
        self.assertEqual(row["hw_rx_mono_ns"], 1400)
        self.assertEqual(row["user_hw_ns"], 600)
        self.assertIsNone(row["d1_ns"])
        self.assertIsNone(row["d2_ns"])
        self.assertIsNone(row["d3_ns"])
        self.assertEqual(row["segment_status"], "incomplete")

    def test_outside_xdp_does_not_switch_to_in_range_user_offset(self):
        row = self.row(path=0, xdp=900)
        self.assertEqual(row["hw_calibration_reference"], "xdp")
        self.assertIsNone(row["hw_rx_mono_ns"])
        self.assertIsNone(row["user_hw_ns"])
        self.assertEqual(row["user_xdp_ns"], 1100)

    def test_negative_stage_is_invalid_even_when_sum_closes(self):
        row = self.row(cpumap_time=1400)
        self.assertEqual(row["d2_ns"], -100)
        self.assertEqual(row["segment_residual_ns"], 0)
        self.assertEqual(row["segment_status"], "invalid")
        summary = join_results.decomposition_summary([row, self.row(user_present=False)])
        self.assertEqual(summary["counts"]["invalid"], 1)
        self.assertEqual(summary["counts"]["incomplete"], 1)


if __name__ == "__main__":
    unittest.main()
