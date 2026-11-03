"""Read-only device partition checks use private sysfs fixtures."""
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'monitor/check_cpu_partition.sh'


class CpuPartitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ace-partition-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cpu = self.root / 'devices/system/cpu'
        self.cpu.mkdir(parents=True)
        (self.cpu / 'online').write_text('0-3\n')
        (self.cpu / 'isolated').write_text('3\n')
        self.net = self.root / 'class/net/eth0'
        (self.net / 'queues/rx-0').mkdir(parents=True)
        (self.net / 'queues/rx-0/rps_cpus').write_text('00000000\n')
        (self.net / 'threaded').write_text('0\n')

    def check(self, shared='0,1,2', dedicated='3'):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = subprocess.run(
            ['bash', '-c', 'source "$1"; SYS_ROOT="$2"; main eth0 "$3" "$4"',
             'fixture', str(SCRIPT), str(self.root), shared, dedicated],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        return result

    def test_partition_accepts_shared_range_and_reserved_core(self):
        result = self.check('0-2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('scheduler-domain isolated', result.stdout)

    def test_unisolated_cpu_refuses_measurement(self):
        (self.cpu / 'isolated').write_text('\n')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not scheduler-domain isolated', result.stderr)

    def test_shared_or_offline_dedicated_cpu_rejected(self):
        for shared, dedicated in [('0-3', '3'), ('0-2', '4')]:
            with self.subTest(shared=shared, dedicated=dedicated):
                self.assertNotEqual(self.check(shared, dedicated).returncode, 0)
        (self.cpu / 'isolated').write_text('2-3\n')
        self.assertIn('Shared CPU2', self.check().stderr)

    def test_rps_must_not_redirect_the_shared_stack(self):
        (self.net / 'queues/rx-0/rps_cpus').write_text('f\n')
        self.assertIn('RPS must be disabled', self.check().stderr)

    def test_separate_threaded_napi_rejected(self):
        (self.net / 'threaded').write_text('1\n')
        self.assertIn('Separate threaded NAPI', self.check().stderr)

    def test_bad_cpu_syntax_is_rejected_without_writes(self):
        for shared in ['0,,1', '2-0', '0,1,', '0-99999', '-1']:
            with self.subTest(shared=shared):
                self.assertNotEqual(self.check(shared).returncode, 0)


if __name__ == '__main__':
    unittest.main()
