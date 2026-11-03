"""Linux loopback checks for run barriers, bounded shutdown and saved records.

Run after `make -C udp_socket`: python3 -m unittest -v tests.test_udp_automation
No NIC reconfiguration or hardware timestamps are required.
"""
import json
import os
from pathlib import Path
import signal
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "udp_socket" / "bin"


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux socket/memlock test")
class UDPAutomationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ace-automation-")
        self.directory = Path(self.tmp.name)
        self.staging = tempfile.TemporaryDirectory(prefix="ace-automation-", dir="/dev/shm")
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
        self.staging.cleanup()
        self.tmp.cleanup()

    def launch(self, name, *arguments, pass_fds=()):
        process = subprocess.Popen([str(BIN / name), *map(str, arguments)],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, pass_fds=pass_fds)
        self.processes.append(process)
        return process

    def ready(self, process, path):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if path.exists():
                data = json.loads(path.read_text())  # first observation must be complete
                self.assertEqual(data["pid"], process.pid)
                self.assertGreater(data["ready_mono_ns"], 0)
                self.assertIsNone(process.poll())
                return data
            if process.poll() is not None:
                self.fail(f"exited before READY: {process.communicate()[0]}")
            time.sleep(0.005)
        self.fail("ready marker was not published")

    def complete(self, process, expected=0):
        output = process.communicate(timeout=4)[0]
        self.assertEqual(process.returncode, expected, output)
        return output

    def receiver(self, count=10, *extra):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ready = self.directory / "receiver.ready"
        stats = self.directory / "receiver.json"
        records = self.directory / "records.bin"
        process = self.launch("receiver", "--bind", "127.0.0.1", "--port", port,
                              "--flow-id", 42, "--count", count,
                              "--tmpfs", Path(self.staging.name) / "records.tmp",
                              "--output", records, "--ready-file", ready,
                              "--stats-json", stats, *extra)
        self.ready(process, ready)
        return process, port, stats, records

    def sender(self, port, count=1, *extra):
        return self.launch("sender", "--port", port, "--flow-id", 42,
                           "--count", count, *extra, "127.0.0.1")

    def test_start_barrier_and_diagnostics(self):
        receiver, port, rxstats, records = self.receiver(3)
        start_file = self.directory / "start.ns"
        ready = self.directory / "sender.ready"
        stats = self.directory / "sender.json"
        sender = self.sender(port, 3, "--period-ns", 2000000,
                             "--start-file", start_file, "--ready-file", ready,
                             "--stats-json", stats)
        self.ready(sender, ready)
        self.assertIsNone(sender.poll())
        deadline = int(subprocess.check_output([str(BIN / "ace_clock"), "--after-ms", "100"]))
        temporary = self.directory / "start.tmp"
        temporary.write_text(f"{deadline}\n")
        os.replace(temporary, start_file)
        self.complete(sender)
        self.complete(receiver)
        tx = json.loads(stats.read_text())
        rx = json.loads(rxstats.read_text())
        self.assertEqual((tx["attempted"], tx["sent"], tx["errors"]), (3, 3, 0))
        self.assertEqual(tx["scheduled_start_mono_ns"], deadline)
        self.assertGreaterEqual(tx["first_send_mono_ns"], deadline)
        self.assertEqual(tx["interval_count"], 2)
        self.assertLessEqual(tx["interval_min_ns"], tx["interval_mean_ns"])
        self.assertLessEqual(tx["interval_mean_ns"], tx["interval_max_ns"])
        self.assertEqual(rx["received"], 3)
        self.assertTrue(rx["records_saved"])
        self.assertTrue(rx["capacity_reached"])
        self.assertEqual(records.stat().st_size, 3 * 56)
        self.assertEqual([struct.unpack_from("=Q", records.read_bytes(), i * 56)[0]
                          for i in range(3)], [0, 1, 2])

    def test_signal_drain_preserves_late_packet(self):
        receiver, port, stats, records = self.receiver(10, "--drain-ms", 200)
        self.complete(self.sender(port))
        receiver.send_signal(signal.SIGTERM)
        time.sleep(0.03)
        self.complete(self.sender(port))
        self.complete(receiver)
        data = json.loads(stats.read_text())
        self.assertEqual(data["received"], 2)
        self.assertEqual(data["termination_reason"], "signal_drain")
        self.assertFalse(data["capacity_reached"])
        self.assertTrue(data["records_saved"])
        self.assertEqual(records.stat().st_size, 112)

    def test_idle_timeout_without_traffic(self):
        receiver, _, stats, records = self.receiver(10, "--idle-timeout-ms", 30)
        self.complete(receiver)
        data = json.loads(stats.read_text())
        self.assertEqual(data["termination_reason"], "idle_timeout")
        self.assertEqual(data["received"], 0)
        self.assertTrue(data["records_saved"])
        self.assertEqual(records.stat().st_size, 0)

    def test_defer_flush_waits_for_supervisor_stop(self):
        receiver, port, stats, records = self.receiver(2, "--defer-flush")
        self.complete(self.sender(port, 2))
        time.sleep(0.04)
        self.assertIsNone(receiver.poll())
        self.assertEqual(records.stat().st_size, 0)
        self.assertEqual(stats.stat().st_size, 0)
        receiver.send_signal(signal.SIGTERM)
        self.complete(receiver)
        self.assertEqual(records.stat().st_size, 112)
        data = json.loads(stats.read_text())
        self.assertTrue(data["records_saved"])
        self.assertEqual(data["termination_reason"], "count")

    def test_sender_completion_barrier_defers_statistics(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        stats = self.directory / "sender.json"
        sender = self.launch("sender", "--flow-id", 42, "--count", 2,
                             "--completion-fd", write_fd, "--defer-flush",
                             "--stats-json", stats, "127.0.0.1", pass_fds=(write_fd,))
        os.close(write_fd)
        self.assertTrue(select.select([read_fd], [], [], 3)[0])
        self.assertEqual(os.read(read_fd, 1), b"\x00")
        self.assertIsNone(sender.poll())
        self.assertEqual(stats.stat().st_size, 0)
        sender.send_signal(signal.SIGUSR1)
        self.complete(sender)
        data = json.loads(stats.read_text())
        self.assertEqual(data["sent"], 2)
        self.assertEqual(data["start_mono_ns"], data["first_send_mono_ns"])

    def test_receiver_drain_completion_then_flush_barrier(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        ready = self.directory / "rx.ready"
        stats = self.directory / "rx.json"
        records = self.directory / "rx.bin"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        receiver = self.launch("receiver", "--flow-id", 42, "--port", port,
                               "--count", 10, "--tmpfs", Path(self.staging.name) / "rx.tmp",
                               "--output", records, "--stats-json", stats, "--ready-file", ready,
                               "--drain-ms", 20, "--completion-fd", write_fd,
                               "--defer-flush", pass_fds=(write_fd,))
        os.close(write_fd)
        self.ready(receiver, ready)
        self.complete(self.sender(port, 2))
        receiver.send_signal(signal.SIGTERM)
        self.assertTrue(select.select([read_fd], [], [], 3)[0])
        self.assertEqual(os.read(read_fd, 1), b"\x00")
        self.assertIsNone(receiver.poll())
        self.assertEqual(records.stat().st_size, 0)
        receiver.send_signal(signal.SIGUSR1)
        self.complete(receiver)
        self.assertEqual(records.stat().st_size, 112)
        self.assertTrue(json.loads(stats.read_text())["records_saved"])

    def test_sigint_during_empty_receive_is_bounded(self):
        receiver, _, stats, records = self.receiver(10, "--drain-ms", 0)
        receiver.send_signal(signal.SIGINT)
        self.complete(receiver)
        self.assertEqual(json.loads(stats.read_text())["termination_reason"], "signal_drain")
        self.assertEqual(records.stat().st_size, 0)

    def test_sender_stop_while_waiting_for_start(self):
        ready = self.directory / "sender.ready"
        stats = self.directory / "sender.json"
        sender = self.sender(9000, 3, "--start-file", self.directory / "absent.ns",
                             "--ready-file", ready, "--stats-json", stats)
        self.ready(sender, ready)
        sender.send_signal(signal.SIGTERM)
        self.complete(sender)
        data = json.loads(stats.read_text())
        self.assertEqual(data["sent"], 0)
        self.assertEqual(data["termination_reason"], "signal")

    def test_three_senders_share_one_start_barrier(self):
        start = self.directory / "common.ns"
        senders = []
        for flow in range(3):
            ready = self.directory / f"flow{flow}.ready"
            stats = self.directory / f"flow{flow}.json"
            sender = self.launch("sender", "--flow-id", flow, "--count", 2,
                                 "--start-file", start, "--ready-file", ready,
                                 "--stats-json", stats, "127.0.0.1")
            self.ready(sender, ready)
            senders.append((sender, stats))
        deadline = time.monotonic_ns() + 100_000_000
        temporary = self.directory / "common.tmp"
        temporary.write_text(f"{deadline}\n")
        os.replace(temporary, start)
        for sender, stats in senders:
            self.complete(sender)
            data = json.loads(stats.read_text())
            self.assertEqual(data["scheduled_start_mono_ns"], deadline)
            self.assertGreaterEqual(data["first_send_mono_ns"], deadline)
            self.assertEqual(data["sent"], 2)

    def test_bad_or_expired_start_file_rejected(self):
        for value in ("1\n", "not-a-deadline\n", "123\n456\n"):
            with self.subTest(value=value):
                start = self.directory / "bad.ns"
                start.write_text(value)
                sender = self.sender(9000, 1, "--start-file", start)
                self.complete(sender, 1)

    def test_ready_collision_keeps_existing_file(self):
        ready = self.directory / "existing.ready"
        ready.write_text("original")
        self.complete(self.sender(9000, 1, "--ready-file", ready), 1)
        self.assertEqual(ready.read_text(), "original")
        self.assertEqual(list(self.directory.glob("*.tmp.*")), [])

    def test_stats_collision_keeps_existing_file(self):
        stats = self.directory / "existing.json"
        stats.write_text("original")
        self.complete(self.sender(9000, 1, "--stats-json", stats), 1)
        self.assertEqual(stats.read_text(), "original")

    def test_start_options_exclusive_and_numeric(self):
        self.complete(self.sender(9000, 1, "--start-file", "anything", "--start-at-ns", 1), 2)
        self.complete(self.launch("ace_clock", "--after-ms", "-1"), 2)
        self.complete(self.launch("receiver", "--flow-id", 42, "--drain-ms", "-1"), 2)


if __name__ == "__main__":
    unittest.main()
