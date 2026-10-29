"""End-to-end native runner smoke and failure checks; Linux loopback only."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "experiment" / "ace-run"


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux native runner")
class NativeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ace-native-")
        self.directory = Path(self.temporary.name)
        self.processes = []
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.base = (ROOT / "experiment" / "socket-smoke.conf").read_text()
        self.base = self.base.replace("control_port=49090", f"control_port={self.port}")
        self.base = self.base.replace("output_root=results/experiment", f"output_root={self.directory}/result")
        self.config = self.directory / "run.conf"
        self.config.write_text(self.base)

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
        self.temporary.cleanup()

    def launch(self, role, config=None):
        process = subprocess.Popen([str(RUNNER), role, "--config", str(config or self.config)],
                                   cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        self.processes.append(process)
        return process

    def complete(self, process, success=True):
        output = process.communicate(timeout=20)[0]
        if success:
            self.assertEqual(process.returncode, 0, output)
        else:
            self.assertNotEqual(process.returncode, 0, output)
        return output

    def test_three_flow_two_repeat_socket_suite(self):
        receiver = self.launch("receiver")
        sender = self.launch("sender")
        self.complete(sender)
        self.complete(receiver)
        root = self.directory / "result" / "socket-smoke"
        for role in ("receiver", "sender"):
            runs = sorted((root / role / "runs").iterdir())
            self.assertEqual(len(runs), 2)
            for run in runs:
                data = json.loads((run / "run.json").read_text())
                self.assertEqual(data["state"], "completed")
                self.assertEqual(data["mode"], "socket-only")
                self.assertEqual(len(data["flows"]), 3)
                self.assertTrue((run / ".complete").exists())
                self.assertFalse((run / ".failed").exists())
                # socket-only도 상태 파일을 남기지만 governor/IRQ를 적용하지 않는다.
                for phase in ('before', 'after'):
                    self.assertIn('capture_complete=1', (run / f'system-{phase}.txt').read_text())
                self.assertFalse((run / 'environment-prepare.log').exists())
                self.assertEqual(data['environment']['governor'], 'performance')
                self.assertEqual(data['environment']['rx_irq_cpu'], 2)
                manifest = json.loads((run / "manifest.json").read_text())
                self.assertEqual(manifest["run_id"], data["run_id"])
                for flow in data["flows"]:
                    stats = json.loads((run / f'{role}-flow{flow["flow_id"]}.json').read_text())
                    self.assertEqual(stats["sent" if role == "sender" else "received"], flow["count"])
        # A second launch must refuse the same run directories and preserve data.
        files = list(root.rglob("run.json"))
        before = {file: file.read_bytes() for file in files}
        receiver = self.launch("receiver")
        sender = self.launch("sender")
        self.complete(sender, False)
        self.complete(receiver, False)
        self.assertEqual(before, {file: file.read_bytes() for file in files})

    def test_role_specific_supervisor_affinity(self):
        # 실행 호스트가 허용한 CPU에서 두 역할의 독립 affinity를 실제 확인한다.
        cpus = sorted(os.sched_getaffinity(0))
        self.config.write_text(self.base.replace("duration_ms=100", "duration_ms=1000") +
                               f"receiver.supervisor_cpu={cpus[0]}\n" +
                               f"sender.supervisor_cpu={cpus[-1]}\n" +
                               "alternate_mode_order=1\n")
        receiver = self.launch("receiver")
        sender = self.launch("sender")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if list((self.directory / "result").rglob("start.ns")):
                break
            if receiver.poll() is not None or sender.poll() is not None:
                self.fail("runner exited before affinity check")
            time.sleep(0.01)
        else:
            self.fail("no start barrier")
        self.assertEqual(os.sched_getaffinity(receiver.pid), {cpus[0]})
        self.assertEqual(os.sched_getaffinity(sender.pid), {cpus[-1]})
        self.complete(sender)
        self.complete(receiver)

    def test_generated_design_configs_are_accepted_by_native_parser(self):
        from tests.test_design_plan import DesignPlanTests, plan
        profile = DesignPlanTests().profile()
        for index, row in enumerate(plan.rows()):
            self.config.write_text(plan.config(profile, row, index))
            output = self.complete(self.launch("plan"))
            conditions = [json.loads(line) for line in output.splitlines()[1:]]
            self.assertTrue(conditions)
            self.assertEqual(conditions[0]['environment']['sender_network_cpu'], 3)
            self.assertEqual(conditions[0]['environment']['receiver_supervisor_cpu'], 0)

    def test_mismatched_configuration_is_rejected(self):
        other = self.directory / "other.conf"
        other.write_text(self.base.replace("duration_ms=100", "duration_ms=101"))
        receiver = self.launch("receiver")
        sender = self.launch("sender", other)
        self.complete(sender, False)
        self.complete(receiver, False)
        self.assertEqual(list((self.directory / "result").rglob(".complete")), [])

    @unittest.skipUnless(all(shutil.which(tool) for tool in ("iperf3", "mpstat", "sar")),
                         "real iperf3/mpstat/sar are required")
    def test_real_network_load_and_observation_two_repeat_suite(self):
        # The real client reports failure if RX stops its server before TX ACK.
        # Reserve a dynamically selected port in both transports while choosing it.
        while True:
            with socket.socket() as tcp, socket.socket(type=socket.SOCK_DGRAM) as udp:
                tcp.bind(("127.0.0.1", 0))
                port = tcp.getsockname()[1]
                if port == self.port:
                    continue
                try:
                    udp.bind(("127.0.0.1", port))
                except OSError:
                    continue
                break
        config = self.base.replace("duration_ms=100", "duration_ms=1500")
        config = config.replace("warmup_ms=0", "warmup_ms=100")
        config = config.replace("monitor=0", "monitor=1")
        config = config.replace("load.network_bps=0", "load.network_bps=1000000")
        cpus = sorted(os.sched_getaffinity(0))
        self.config.write_text(config + f"load.network_port={port}\n" +
                               f"receiver.network_cpu={cpus[0]}\n" +
                               f"sender.network_cpu={cpus[-1]}\n")
        receiver = self.launch("receiver")
        sender = self.launch("sender")
        def descendants(pid):
            try:
                children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            except FileNotFoundError:
                return []
            result = []
            for child in children:
                child = int(child)
                try:
                    command = Path(f"/proc/{child}/cmdline").read_bytes().replace(b"\0", b" ")
                except FileNotFoundError:
                    continue
                result.append((child, command))
                result.extend(descendants(child))
            return result
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            server = [pid for pid, command in descendants(receiver.pid)
                      if b"iperf3 -s" in command]
            client = [pid for pid, command in descendants(sender.pid)
                      if b"iperf3 -c" in command]
            if server and client:
                self.assertEqual(os.sched_getaffinity(server[0]), {cpus[0]})
                self.assertEqual(os.sched_getaffinity(client[0]), {cpus[-1]})
                break
            time.sleep(0.01)
        else:
            self.fail("no real iperf server/client for affinity check")
        self.complete(sender)
        self.complete(receiver)
        root = self.directory / "result" / "socket-smoke"
        for role in ("receiver", "sender"):
            runs = sorted((root / role / "runs").iterdir())
            self.assertEqual(len(runs), 2)
            for run in runs:
                data = json.loads((run / "run.json").read_text())
                self.assertEqual(data["state"], "completed")
                self.assertTrue((run / ".complete").exists())
                self.assertFalse((run / ".failed").exists())
                for flow in data["flows"]:
                    stats = json.loads((run / f'{role}-flow{flow["flow_id"]}.json').read_text())
                    self.assertEqual(stats["sent" if role == "sender" else "received"], flow["count"])
                if role == "receiver":
                    for name in ("mpstat.log", "network.log", "iperf3.log"):
                        self.assertGreater((run / "monitor" / name).stat().st_size, 0)

    def test_peer_loss_flushes_own_receivers(self):
        self.config.write_text(self.base.replace("duration_ms=100", "duration_ms=5000"))
        receiver = self.launch("receiver")
        sender = self.launch("sender")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            states = list((self.directory / "result").rglob("start.ns"))
            if states:
                break
            if receiver.poll() is not None or sender.poll() is not None:
                self.fail("runner exited before measurement")
            time.sleep(0.01)
        else:
            self.fail("no start barrier")
        sender.send_signal(signal.SIGINT)
        self.complete(sender, False)
        self.complete(receiver, False)
        rx = next((self.directory / "result" / "socket-smoke" / "receiver" / "runs").iterdir())
        self.assertTrue((rx / ".failed").exists())
        self.assertFalse((rx / ".complete").exists())
        for stats in rx.glob("receiver-flow*.json"):
            self.assertTrue(json.loads(stats.read_text())["records_saved"])

    def test_plan_rejects_unknown_duplicate_and_capacity(self):
        for text in (self.base + "unknown=1\n", self.base + "repeats=1\n",
                     self.base.replace("run_id=socket-smoke", "run_id=_bad"),
                     self.base.replace("flow1.period_ns=2000000", "flow1.period_ns=1"),
                     self.base + "environment.governor=bad/value\n",
                     self.base + "environment.rx_irq_cpu=-1\n",
                     self.base + "receiver.supervisor_cpu=-2\n",
                     self.base + "sender.network_cpu=bad\n",
                     self.base + "alternate_mode_order=2\n"):
            self.config.write_text(text)
            self.complete(self.launch("plan"), False)


if __name__ == "__main__":
    unittest.main()
