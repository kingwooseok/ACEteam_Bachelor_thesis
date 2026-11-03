from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from analysis import aggregate_runs as aggregate
from analysis import analyze_experiment as experiment
from analysis import join_results as join
from analysis.tests.test_join_results import INGRESS_HEADER, CPUMAP_HEADER, AFXDP_HEADER, CALIBRATION_HEADER


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        # Windows can exercise pure analysis; Linux CI also exercises directory fsync.
        if os.name == "nt":
            self.stack.enter_context(mock.patch.object(os, "O_CLOEXEC", 0, create=True))
            self.stack.enter_context(mock.patch.object(join, "fsync_directories"))
            self.stack.enter_context(mock.patch.object(aggregate, "fsync_directories"))

    def make_run(self, root: Path, name: str, *, smoke=False, complete=True) -> Path:
        run = root / name
        run.mkdir(parents=True)
        descriptor = {"schema_version": 1, "run_id": name, "case_id": "load0",
                      "mode": "socket-only" if smoke else "mixed", "repeat": 1,
                      "state": "completed" if complete else "running", "purpose": "smoke" if smoke else "measurement",
                      "duration_s": 1, "load": {"cpu_percent": 0},
                      "flows": [{"flow_id": 1, "port": 9000, "requested_path": "pass", "period_ns": 1000000, "count": 3, "deadline_ns": None},
                                {"flow_id": 2, "port": 9001, "requested_path": "cpumap", "period_ns": 1000000, "count": 3, "deadline_ns": 250}]}
        (run / "run.json").write_text(json.dumps(descriptor), encoding="utf-8")
        (run / "manifest.json").write_text(json.dumps({"manifest_version": 1, "run_id": name, "parameters": {"case": "load0"}}), encoding="utf-8")
        if complete:
            (run / ".complete").touch()
        (run / "xdp-ingress_records.csv").write_text(INGRESS_HEADER + "1,0,0,1100,1100,19,0,0\n1,1,0,2100,2100,19,0,0\n2,0,0,1300,1300,19,1,0\n", encoding="utf-8")
        (run / "xdp-cpumap_records.csv").write_text(CPUMAP_HEADER + "2,0,3,1400,23,1\n", encoding="utf-8")
        (run / "udp-flow1.bin").write_bytes(join.UDP_RECORD.pack(0, 100, 1200, 5000, 1100, 1, 96, 1, 0, 0))
        (run / "udp-flow2.bin").write_bytes(join.UDP_RECORD.pack(0, 100, 1500, 5000, 0, 2, 96, 0, 0, 0))
        for flow in (1, 2):
            (run / f"sender-flow{flow}.json").write_text(json.dumps({"flow_id": flow, "sent": 3, "expected_count": 3, "errors": 0,
                            "start_mono_ns": 1000000000, "end_mono_ns": 2000000000, "elapsed_ns": 1000000000}), encoding="utf-8")
        (run / "phc-before.csv").write_text(CALIBRATION_HEADER + "0,0,490,600,510,20,100,1\n", encoding="utf-8")
        (run / "phc-after.csv").write_text(CALIBRATION_HEADER + "0,0,4990,5100,5010,20,100,1\n", encoding="utf-8")
        monitor = run / "monitor"; monitor.mkdir()
        (monitor / "mpstat.log").write_text("00:00:00 CPU %usr %sys %idle\nAverage: all 10.00 5.00 85.00\nAverage: 0 20.00 5.00 75.00\n", encoding="utf-8")
        (monitor / "cpu-samples.csv").write_text(
            "monotonic_ns,cpu,user,nice,system,idle,iowait,irq,softirq,steal\n"
            "1000,all,0,0,0,0,0,0,0,0\n1000,0,0,0,0,0,0,0,0,0\n"
            "2000,all,10,0,5,85,0,0,0,0\n2000,0,20,0,5,75,0,0,0,0\n"
            "3000,all,20,0,10,170,0,0,0,0\n3000,0,40,0,10,150,0,0,0,0\n",
            encoding="utf-8")
        (monitor / "network.log").write_text("00:00:00 IFACE rxpck/s txpck/s rxkB/s txkB/s\nAverage: eth0 3.00 0.00 0.30 0.00\n", encoding="utf-8")
        return run

    def test_multiple_udp_join_flow_deadline_loss_and_html(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = [self.make_run(root, name) for name in ("run1", "run2")]
            before = {str(path): path.read_bytes() for run in runs for path in run.glob("*.bin")}
            report = experiment.build_report(runs)
            self.assertEqual([item["status"] for item in report["runs"]], ["completed", "completed"])
            condition = report["conditions"][0]
            self.assertEqual(condition["status"], "completed")
            flows = condition["statistics"]["flows"]
            self.assertEqual(flows["1"]["metrics"]["user-hw"]["packet_level"]["p99_ns"], 200)
            self.assertIsNone(flows["1"]["metrics"]["user-hw"]["packet_level"]["deadline_miss_count"])
            self.assertEqual(flows["2"]["metrics"]["user-hw"]["packet_level"]["p99_ns"], 300)
            self.assertEqual(flows["2"]["metrics"]["user-hw"]["packet_level"]["deadline_miss_rate"], 1)
            self.assertEqual(condition["delivery"]["1"]["sent"], 6)
            self.assertEqual(condition["delivery"]["1"]["loss_count"], 4)
            self.assertEqual(condition["cpu_busy_pct"]["mean"], 15)
            self.assertEqual(report["runs"][0]["cpu_measurement"]["window"]["end_ns"], 2100)
            self.assertEqual(report["runs"][0]["cpu_measurement"]["window"]["start_ns"], 1100)
            self.assertIn("monitor/cpu-samples.csv", report["runs"][0]["input_artifacts"])
            for run in runs:
                validated = aggregate.load_run(run / "analysis/summary.json.complete")
                self.assertEqual(len(validated.input_artifacts["udp_native"]), 2)
            output = experiment.render_html(report)
            self.assertIn("<svg", output)
            self.assertIn("수신 측정 구간 CPU 사용률", output)
            self.assertNotIn("<script src", output)
            self.assertEqual(before, {str(path): path.read_bytes() for run in runs for path in run.glob("*.bin")})

    def test_three_modes_keep_cpu_placements_and_distinct_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = []
            for mode in ("all-pass", "pass-xsk", "mixed"):
                for repeat in (1, 2):
                    run = self.make_run(root, f'{mode}-{repeat}')
                    data = json.loads((run / "run.json").read_text())
                    data.update(mode=mode, repeat=repeat)
                    data["flows"][0]["rx_cpu"] = 0
                    data["flows"][1].update(rx_cpu=1 if mode == "all-pass" else 3,
                                            requested_path="cpumap" if mode == "mixed" else "pass")
                    data["flows"].append(dict(flow_id=3, port=9002, count=3, period_ns=1000000,
                                              deadline_ns=250, requested_path="pass" if mode == "all-pass" else "xsk", rx_cpu=1))
                    ingress = (run / "xdp-ingress_records.csv").read_text()
                    if mode != "mixed":
                        ingress = ingress.replace("2,0,0,1300,1300,19,1,0", "2,0,0,1300,1300,19,0,0")
                        (run / "xdp-cpumap_records.csv").write_text(CPUMAP_HEADER)
                    ingress += f'3,0,0,1700,1700,19,{0 if mode == "all-pass" else 2},0\n'
                    (run / "xdp-ingress_records.csv").write_text(ingress)
                    if mode == "all-pass":
                        (run / "udp-flow3.bin").write_bytes(join.UDP_RECORD.pack(0, 100, 1900, 5000, 1700, 3, 96, 1, 0, 0))
                    else:
                        (run / "afxdp.csv").write_text(AFXDP_HEADER + "3,0,0,100,1900,5000,1700,1700,19,2,0,96,7\n")
                    (run / "sender-flow3.json").write_text(json.dumps(dict(flow_id=3, sent=3, expected_count=3,
                        errors=0, start_mono_ns=1000000000, end_mono_ns=2000000000, elapsed_ns=1000000000)))
                    (run / "run.json").write_text(json.dumps(data))
                    (run / "manifest.json").write_text(json.dumps(dict(manifest_version=1, run_id=data["run_id"],
                        parameters=dict(mode=mode, flows=data["flows"]))))
                    runs.append(run)
            single = self.make_run(root, "single-mixed")
            data = json.loads((single / "run.json").read_text())
            data["case_id"] = "single-2"
            (single / "run.json").write_text(json.dumps(data))
            runs.append(single)
            report = experiment.build_report(runs)
            self.assertTrue(all(run["status"] == "completed" for run in report["runs"]), report)
            self.assertEqual(len(report["conditions"]), 4)
            for condition in report["conditions"]:
                self.assertEqual(condition["status"], "completed", condition)
                if condition["case_id"] == "single-2":
                    self.assertEqual(condition["statistics"]["flows"]["2"]["run_count"], 1)
                    continue
                self.assertEqual(condition["flows"][1]["rx_cpu"], 1 if condition["mode"] == "all-pass" else 3)
                self.assertEqual(condition["statistics"]["flows"]["2"]["run_count"], 2)
                self.assertEqual(len(condition["decomposition"]), 2)
                self.assertEqual(condition["decomposition"][0]["max_abs_residual_ns"], 0)
            page = experiment.render_html(report)
            self.assertIn("pass-xsk", page)
            self.assertIn("구간별 지연과 실제 수신 배치", page)
            self.assertIn("같은 패킷의 구간 합 검사", page)

    def test_smoke_and_incomplete_runs_cannot_contribute_hardware_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            smoke = self.make_run(root, "smoke", smoke=True)
            pending = self.make_run(root, "pending", complete=False)
            report = experiment.build_report([smoke, pending])
            self.assertEqual(report["runs"][0]["status"], "smoke-completed")
            self.assertFalse(report["runs"][0]["hardware_latency"])
            self.assertEqual(report["runs"][1]["status"], "incomplete")
            self.assertEqual(report["conditions"], [])
            self.assertFalse((smoke / "analysis").exists())

    def test_cross_file_overlap_and_raw_tampering_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.make_run(Path(directory), "run1")
            path = run / "udp-flow1.bin"
            with self.assertRaisesRegex(join.InputError, "overlap"):
                join.load_udp_sources([path, path])
            self.assertEqual(experiment.build_report([run])["runs"][0]["status"], "completed")
            path.write_bytes(path.read_bytes() + path.read_bytes())
            changed = experiment.build_report([run])
            self.assertEqual(changed["runs"][0]["status"], "analysis-failed")
            self.assertIn("digest mismatch", changed["runs"][0]["reason"])

    def test_per_flow_cli_and_optional_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = [self.make_run(root, name) for name in ("run1", "run2")]
            experiment.build_report(runs)
            args = ["--by-flow", "--flow-deadline", "2=250", "--output-csv", str(root/"aggregate.csv"), "--summary-json", str(root/"aggregate.json")]
            for run in runs:
                args += ["--run", str(run / "analysis/summary.json.complete")]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(aggregate.main(args), 0)
            value = json.loads((root/"aggregate.json").read_text())
            self.assertIsNone(value["flows"]["1"]["deadline"]["deadline_ns"])
            self.assertEqual(value["flows"]["2"]["deadline"]["deadline_ns"], 250)

    def test_two_pi_collection_is_exclusive_and_checks_identity_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = self.make_run(root, "receiver")
            sender = self.make_run(root, "sender")
            tx = json.loads((sender/"run.json").read_text())
            tx["run_id"] = "receiver"
            (sender/"run.json").write_text(json.dumps(tx))
            for path in receiver.glob("sender-flow*.json"):
                path.unlink()
            target = root / "collected"
            experiment.collect_results(receiver, sender, target)
            self.assertEqual((target/"udp-flow1.bin").read_bytes(), (receiver/"udp-flow1.bin").read_bytes())
            self.assertEqual((target/"sender-flow1.json").read_bytes(), (sender/"sender-flow1.json").read_bytes())
            self.assertEqual(experiment.build_report([target])["runs"][0]["status"], "completed")
            with self.assertRaises(FileExistsError):
                experiment.collect_results(receiver, sender, target)
            (target/"sender-flow1.json").write_text("{}")
            result = experiment.build_report([target])["runs"][0]
            self.assertEqual(result["status"], "analysis-failed")
            self.assertIn("collected artifact digest mismatch", result["reason"])
            tx["flows"][0]["period_ns"] = 2000000
            (sender/"run.json").write_text(json.dumps(tx))
            with self.assertRaisesRegex(experiment.ExperimentError, "conditions disagree"):
                experiment.collect_results(receiver, sender, root/"mismatch")
            self.assertFalse((root/"mismatch").exists())

    def test_warmup_is_preserved_but_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.make_run(Path(directory), "warmup")
            descriptor = json.loads((run/"run.json").read_text())
            descriptor["purpose"] = "warmup"
            (run/"run.json").write_text(json.dumps(descriptor))
            report = experiment.build_report([run])
            self.assertEqual(report["runs"][0]["status"], "warmup-completed")
            self.assertEqual(report["conditions"], [])
            self.assertFalse((run/"analysis").exists())

    def test_suite_collection_and_failed_source_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rx = root / "rx"; tx = root / "tx"
            receiver = self.make_run(rx / "runs", "run1")
            sender = self.make_run(tx / "runs", "run1")
            for path in receiver.glob("sender-flow*.json"):
                path.unlink()
            original_failure = b"sender stopped\n"
            (sender / ".failed").write_bytes(original_failure)
            destination = root / "collected"
            experiment.collect_suite(rx, tx, destination)
            suite = json.loads((destination / "suite.json").read_text())
            self.assertEqual(suite["runs"][0]["state"], "failed")
            run = destination / suite["runs"][0]["dir"]
            self.assertEqual((run / "sender/.failed").read_bytes(), original_failure)
            self.assertEqual(experiment.build_report([run])["runs"][0]["status"], "failed")

    def test_pps_uses_actual_sender_elapsed_not_scheduled_start(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.make_run(Path(directory), "run1", smoke=True)
            path = run / "sender-flow1.json"
            sender = json.loads(path.read_text())
            sender["scheduled_start_mono_ns"] = 750000000
            path.write_text(json.dumps(sender))
            report = experiment.build_report([run])
            delivery = report["runs"][0]["delivery"][0]
            self.assertEqual(delivery["sender_elapsed_ns"], 1000000000)
            self.assertEqual(delivery["traffic_elapsed_ns"], 1000000000)
            self.assertEqual(delivery["offered_pps"], 3)

    def test_all_pass_uses_recorded_effective_path_for_every_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.make_run(Path(directory), "allpass")
            descriptor = json.loads((run / "run.json").read_text())
            descriptor["mode"] = "all-pass"
            descriptor["flows"][1]["requested_path"] = "pass"
            (run / "run.json").write_text(json.dumps(descriptor))
            ingress = run / "xdp-ingress_records.csv"
            ingress.write_text(ingress.read_text().replace("1300,1300,19,1,0", "1300,1300,19,0,0"))
            (run / "xdp-cpumap_records.csv").write_text(CPUMAP_HEADER)
            report = experiment.build_report([run])
            self.assertEqual(report["runs"][0]["status"], "completed")
            flow = report["conditions"][0]["statistics"]["flows"]["2"]
            self.assertEqual(flow["metrics"]["user-hw"]["packet_level"]["count"], 1)

    def test_mixed_afxdp_stats_without_flow_id_uses_packet_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.make_run(Path(directory), "mixed3")
            descriptor = json.loads((run / "run.json").read_text())
            descriptor["flows"].append({"flow_id": 3, "port": 9002, "requested_path": "xsk",
                                         "period_ns": 1000000, "count": 3, "deadline_ns": None})
            (run / "run.json").write_text(json.dumps(descriptor))
            with (run / "xdp-ingress_records.csv").open("a") as file:
                file.write("3,0,0,2100,2100,19,2,0\n")
            (run / "afxdp.csv").write_text(AFXDP_HEADER + "3,0,0,100,2200,5000,2100,2100,19,2,0,138,7\n")
            sender = json.loads((run / "sender-flow1.json").read_text())
            sender["flow_id"] = 3
            (run / "sender-flow3.json").write_text(json.dumps(sender))
            (run / "receiver-flow3.json").write_text(json.dumps({"role": "afxdp_recv", "recorded_packets": 1}))
            report = experiment.build_report([run])
            self.assertEqual(report["runs"][0]["status"], "completed")
            delivery = report["runs"][0]["delivery"][2]
            self.assertEqual(delivery["received_unique"], 1)
            self.assertEqual(delivery["loss_count"], 2)
            self.assertEqual(delivery["receiver_statistics"]["role"], "afxdp_recv")

    def test_cpu_average_excludes_preparation_and_weights_boundary_intervals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cpu.csv"
            path.write_text(
                "monotonic_ns,cpu,user,nice,system,idle,iowait,irq,softirq,steal\n"
                "1,all,0,0,0,0,0,0,0,0\n1,0,0,0,0,0,0,0,0,0\n"
                "1000000001,all,100,0,0,0,0,0,0,0\n1000000001,0,100,0,0,0,0,0,0,0\n"
                "2000000001,all,120,0,0,70,10,0,0,0\n2000000001,0,110,0,0,90,0,0,0,0\n"
                "3000000001,all,160,0,0,120,20,0,0,0\n3000000001,0,200,0,0,100,0,0,0,0\n"
                "4000000001,all,260,0,0,120,20,0,0,0\n4000000001,0,300,0,0,100,0,0,0,0\n",
                encoding="utf-8")
            window = {"status": "available", "start_ns": 1500000001,
                      "end_ns": 2500000001, "duration_ns": 1000000000}
            cpu, detail = experiment.measurement_cpu_summary(path, window)
            self.assertEqual(detail["status"], "complete")
            self.assertEqual(detail["coverage"]["all"]["fraction"], 1)
            self.assertEqual(detail["sample_interval_ns"], {"min": 1000000000, "max": 1000000000})
            # Half of the 20%-busy interval plus half of the 40%-busy interval.
            # The 100%-busy preparation/post-capture periods contribute nothing.
            self.assertAlmostEqual(cpu["all"]["busy_pct"], 30)
            self.assertAlmostEqual(cpu["all"]["%iowait"], 10)
            self.assertAlmostEqual(cpu["0"]["busy_pct"], 50)

    def test_cpu_partial_or_invalid_samples_do_not_supply_measurement_average(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cpu.csv"
            header = "monotonic_ns,cpu,user,nice,system,idle,iowait,irq,softirq,steal\n"
            path.write_text(header + "2000,all,20,0,0,80,0,0,0,0\n"
                            "3000,all,40,0,0,160,0,0,0,0\n", encoding="utf-8")
            window = {"status": "available", "start_ns": 1000, "end_ns": 3000,
                      "duration_ns": 2000}
            cpu, detail = experiment.measurement_cpu_summary(path, window)
            self.assertEqual(cpu, {})
            self.assertEqual(detail["status"], "partial-coverage")
            self.assertEqual(detail["coverage"]["all"]["fraction"], .5)
            path.write_text(header + "1000,all,20,0,0,80,0,0,0,0\n"
                            "3000,all,10,0,0,160,0,0,0,0\n", encoding="utf-8")
            cpu, detail = experiment.measurement_cpu_summary(path, window)
            self.assertEqual(cpu, {})
            self.assertEqual(detail["status"], "invalid-samples")
            self.assertIn("decreased", detail["reason"])

    def test_legacy_mpstat_is_preserved_without_replacing_missing_measurement_cpu(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.make_run(Path(directory), "legacy")
            (run / "monitor/cpu-samples.csv").unlink()
            report = experiment.build_report([run])
            result = report["runs"][0]
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["cpu"], {})
            self.assertEqual(result["cpu_measurement"]["status"], "missing-samples")
            self.assertEqual(result["cpu_observation"]["all"]["busy_pct"], 15)
            self.assertIsNone(report["conditions"][0]["cpu_busy_pct"]["mean"])

    def test_window_includes_dropped_tail_and_later_cpumap_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.make_run(Path(directory), "tail")
            # Last packet reaches XDP/CPUMAP but never reaches userspace.
            with (run / "xdp-ingress_records.csv").open("a") as file:
                file.write("2,2,0,2400,2400,19,1,0\n")
            with (run / "xdp-cpumap_records.csv").open("a") as file:
                file.write("2,2,3,2600,23,1\n")
            result = experiment.build_report([run])["runs"][0]
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["cpu_measurement"]["window"]["end_ns"], 2600)
            self.assertEqual(result["cpu_measurement"]["window"]["duration_ns"], 1500)
            self.assertEqual(result["cpu_measurement"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
