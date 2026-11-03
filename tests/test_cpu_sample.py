"""Native monotonic CPU snapshots; private fixtures and read-only /proc/stat.

Run after make -C udp_socket. No device configuration or root access is used.
"""
import csv
import io
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SAMPLER = ROOT / 'udp_socket/bin/cpu_sample'
HEADER = ['monotonic_ns', 'cpu', 'user', 'nice', 'system', 'idle',
          'iowait', 'irq', 'softirq', 'steal']
FIXTURE = ('cpu  101 102 103 104 105 106 107 108 9999 8888\n'
           'cpu0 11 12 13 14 15 16 17 18 7777 6666\n'
           'intr 100 1 2 3\nctxt 123\n')


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux native CPU observer')
class CPUSampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ace-cpu-sample-')
        self.directory = Path(self.temp.name)
        self.processes = []
        self.files = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        for file in self.files:
            file.close()
        self.temp.cleanup()

    def launch(self, *, fixture=FIXTURE, interval=3600000):
        stat = self.directory / 'stat'
        stat.write_text(fixture)
        output = self.directory / 'samples.csv'
        file = output.open('wb')
        self.files.append(file)
        process = subprocess.Popen([str(SAMPLER), '--interval-ms', str(interval),
                                    '--stat-file', str(stat)], stdout=file,
                                   stderr=subprocess.PIPE)
        self.processes.append(process)
        return process, output, stat

    def wait_rows(self, process, output, count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            text = output.read_text()
            rows = list(csv.DictReader(io.StringIO(text)))
            if len(rows) >= count and text.endswith('\n'):
                return rows
            if process.poll() is not None:
                self.fail(f'sampler stopped early: {process.communicate()[1].decode()}')
            time.sleep(.01)
        self.fail('sampler did not publish the requested snapshots')

    def test_final_snapshot_and_guest_fields_are_not_double_counted(self):
        process, output, stat = self.launch()
        first = self.wait_rows(process, output, 2)
        self.assertEqual(list(first[0]), HEADER)
        self.assertEqual(first[0]['cpu'], 'all')
        self.assertEqual(first[1]['cpu'], '0')
        self.assertEqual(first[0]['monotonic_ns'], first[1]['monotonic_ns'])
        self.assertEqual([int(first[0][key]) for key in HEADER[2:]], list(range(101, 109)))
        updated = self.directory / 'next-stat'
        updated.write_text(FIXTURE.replace('101 ', '201 ').replace('11 ', '21 '))
        updated.replace(stat)
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        _, error = process.communicate(timeout=2)
        self.assertEqual(process.returncode, 0, error.decode())
        self.assertLess(time.monotonic() - started, 1)
        rows = list(csv.DictReader(io.StringIO(output.read_text())))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[-2]['user'], '201')
        self.assertEqual(rows[-1]['user'], '21')
        self.assertEqual(rows[-2]['monotonic_ns'], rows[-1]['monotonic_ns'])
        self.assertGreater(int(rows[-1]['monotonic_ns']), int(first[0]['monotonic_ns']))

    def test_periodic_samples_and_sigint(self):
        process, output, _ = self.launch(interval=20)
        self.wait_rows(process, output, 6)
        process.send_signal(signal.SIGINT)
        _, error = process.communicate(timeout=2)
        self.assertEqual(process.returncode, 0, error.decode())
        rows = list(csv.DictReader(io.StringIO(output.read_text())))
        times = [int(row['monotonic_ns']) for row in rows if row['cpu'] == 'all']
        self.assertGreaterEqual(len(times), 4)
        self.assertTrue(all(a < b for a, b in zip(times, times[1:])))

    def test_bad_snapshot_fails_without_publishing_partial_cpu_rows(self):
        for fixture in ('cpu 1 2 3 4\n', 'cpu0 1 2 3 4 5 6 7 8\n',
                        'cpu 1 2 3 4 5 6 7 8\ncpu0 1 2 -3 4 5 6 7 8\n',
                        'cpu 1 2 3 4 5 6 7 8\ncpu0 1 2 3 4 5 6 7 8\ncpu0 1 2 3 4 5 6 7 8\n'):
            with self.subTest(fixture=fixture):
                process, output, _ = self.launch(fixture=fixture)
                _, error = process.communicate(timeout=2)
                self.assertEqual(process.returncode, 1)
                self.assertIn('CPU snapshot', error.decode())
                self.assertEqual(list(csv.DictReader(io.StringIO(output.read_text()))), [])

    def test_invalid_arguments(self):
        for value in ('0', '-1', '+1', 'abc', '3600001'):
            with self.subTest(value=value):
                result = subprocess.run([str(SAMPLER), '--interval-ms', value], capture_output=True)
                self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
