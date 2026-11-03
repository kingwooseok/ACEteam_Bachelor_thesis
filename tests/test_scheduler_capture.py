"""Read scheduler state from private proc/sys fixtures without changing policy."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'monitor/capture_scheduler.sh'


@unittest.skipUnless(shutil.which('bash'), 'requires Bash')
class SchedulerCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ace-scheduler-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'proc').mkdir()
        (self.root / 'sys/class/net/eth0').mkdir(parents=True)
        (self.root / 'proc/interrupts').write_text(
            ' CPU0 CPU1 CPU2 CPU3\n106: 0 0 1 0 chip eth0\n107: 0 1 0 0 chip eth01\n')
        self.output = self.root / 'scheduler.json'

    def thread(self, tid, name, *, policy=1, priority=50, cpu=2, cpus='2', kernel=True):
        directory = self.root / 'proc' / str(tid)
        directory.mkdir()
        (directory / 'comm').write_text(name + '\n')
        fields = ['0'] * 50
        fields[0] = 'S'
        fields[6] = str(2097152 if kernel else 0)
        fields[19] = '123456'
        fields[36:39] = [str(cpu), str(priority), str(policy)]
        (directory / 'stat').write_text(f'{tid} ({name}) ' + ' '.join(fields) + '\n')
        (directory / 'status').write_text(f'Name:\t{name}\nCpus_allowed_list:\t{cpus}\n')
        return directory

    def run_capture(self):
        command = 'source "$1"; PROC_ROOT="$2/proc"; SYS_ROOT="$2/sys"; capture_scheduler eth0 "$3"'
        return subprocess.run(['bash', '-c', command, 'fixture', str(SCRIPT), str(self.root),
                               str(self.output)], text=True, capture_output=True, timeout=5)

    def test_records_matching_kernel_threads_and_scheduler_fields(self):
        self.thread(1001, 'irq/106-eth0')
        self.thread(1002, 'cpumap/3/map:42', policy=0, priority=0, cpu=3, cpus='3')
        self.thread(1003, 'irq/107-eth01')
        self.thread(1004, 'cpumap/1/map:99', kernel=False)
        result = self.run_capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.output.read_text())
        self.assertEqual(document['irq_ids'], [106])
        rows = {row['tid']: row for row in document['threads']}
        self.assertEqual(set(rows), {1001, 1002})
        self.assertEqual((rows[1001]['policy'], rows[1001]['rt_priority']), ('SCHED_FIFO', 50))
        self.assertEqual((rows[1002]['policy'], rows[1002]['cpus_allowed_list']), ('SCHED_OTHER', '3'))
        self.assertEqual(document['status'], 'captured')
        self.assertEqual((document['irq_thread_count'], document['cpumap_thread_count']), (1, 1))

    def test_empty_snapshot_is_explicit_and_valid(self):
        (self.root / 'proc/interrupts').write_text(' CPU0 CPU1 CPU2 CPU3\n')
        result = self.run_capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.output.read_text())
        self.assertEqual(document['threads'], [])
        self.assertEqual(document['status'], 'not_present')
        self.assertEqual(document['irq_status'], 'no_irq_found')
        self.assertEqual(document['cpumap_status'], 'no_matching_thread')
        self.assertTrue(document['complete'])

    def test_names_with_parenthesis_and_quote_do_not_corrupt_stat_or_json(self):
        self.thread(1001, 'irq/106-a) "b', policy=2, priority=70, cpu=1, cpus='1-2')
        result = self.run_capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(self.output.read_text())['threads'][0]
        self.assertEqual(row['name'], 'irq/106-a) "b')
        self.assertEqual((row['policy'], row['rt_priority'], row['last_cpu']), ('SCHED_RR', 70, 1))

    def test_unreadable_thread_state_is_preserved_as_partial(self):
        directory = self.thread(1001, 'cpumap/3/map:42')
        (directory / 'status').unlink()
        result = self.run_capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.output.read_text())
        self.assertEqual(document['status'], 'partial')
        self.assertEqual(document['threads'][0]['status'], 'affinity_unavailable')

    def test_existing_snapshot_is_not_overwritten(self):
        self.output.write_text('original')
        result = self.run_capture()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
