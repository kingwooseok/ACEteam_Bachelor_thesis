"""Read-only preflight and partition lifecycle using private filesystem fixtures."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

MONITOR = Path(__file__).resolve().parents[1] / 'monitor'

# Emulate only the cgroup virtual-file transitions. Production functions use
# kernel files directly; lifecycle, ownership checks and rollback are unchanged.
EMULATE = r'''
cg_mkdir() {
    mkdir -- "$1"
    for file in cgroup.procs cgroup.subtree_control cpuset.cpus cpuset.mems cpuset.cpus.exclusive; do : > "$1/$file"; done
    printf member > "$1/cpuset.cpus.partition"
    printf 0-3 > "$1/cpuset.cpus.effective"
}
cg_rmdir() {
    rm -- "$1/cgroup.procs" "$1/cgroup.subtree_control" "$1/cpuset.cpus" "$1/cpuset.mems" "$1/cpuset.cpus.exclusive" "$1/cpuset.cpus.partition" "$1/cpuset.cpus.effective"
    rmdir -- "$1"
}
cg_write() {
    local path=$1 value=$2
    if [[ $path == "$CGROUP_ROOT/cgroup.subtree_control" ]]; then
        if [[ $value == +cpuset ]]; then printf 'cpu pids cpuset' > "$path"; else printf 'cpu pids' > "$path"; fi
    elif [[ $path == */cpuset.cpus.partition ]]; then
        if [[ $value == isolated ]]; then
            printf isolated > "$path"
            printf 3 > "${path%/*}/cpuset.cpus.effective"
            printf 0-2 > "$CGROUP_ROOT/cpuset.cpus.effective"
            if [[ ${FAIL_PARTITION:-} == invalid ]]; then printf 'isolated invalid (fixture)' > "$path"; fi
            if [[ ${FAIL_PARTITION:-} == signal ]]; then kill -TERM "$BASHPID"; fi
        else
            printf member > "$path"
            printf 0-3 > "$CGROUP_ROOT/cpuset.cpus.effective"
        fi
    else
        printf '%s' "$value" > "$path"
    fi
}
'''


class CpuPartitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ace-partition-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sys = self.root / 'sys'
        self.cpu = self.sys / 'devices/system/cpu'
        self.cpu.mkdir(parents=True)
        (self.cpu / 'online').write_text('0-3\n')
        (self.cpu / 'isolated').write_text('\n')
        self.net = self.sys / 'class/net/eth0'
        (self.net / 'queues/rx-0').mkdir(parents=True)
        (self.net / 'queues/rx-0/rps_cpus').write_text('00000000\n')
        (self.net / 'threaded').write_text('0\n')
        self.cgroup = self.root / 'cgroup'
        self.cgroup.mkdir()
        (self.cgroup / 'cgroup.controllers').write_text('cpuset cpu pids\n')
        (self.cgroup / 'cgroup.subtree_control').write_text('cpu pids\n')
        (self.cgroup / 'cpuset.cpus.effective').write_text('0-3\n')
        self.state = self.root / 'run'
        self.state.mkdir()
        self.group = self.cgroup / 'ace-rx-4242'

    def check(self, available='0,1,2,3', dedicated='3'):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = subprocess.run(
            ['bash', '-c', 'source "$1"; SYS_ROOT="$2"; CGROUP_ROOT="$3"; main eth0 "$4" "$5"',
             'fixture', str(MONITOR / 'check_cpu_partition.sh'), str(self.sys),
             str(self.cgroup), available, dedicated], capture_output=True, text=True, timeout=10)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        return result

    def lifecycle(self, action, prefix=''):
        script = 'source "$1"; SYS_ROOT="$2"; CGROUP_ROOT="$3"; STATE="$4"\n' + EMULATE + '\n' + prefix + '\n' + action
        return subprocess.run(['bash', '-c', script, 'fixture', str(MONITOR / 'cpu_partition.sh'),
                               str(self.sys), str(self.cgroup), str(self.state)],
                              capture_output=True, text=True, timeout=15)

    def test_preflight_accepts_full_cpu_sharing_without_boot_isolation(self):
        result = self.check('0-3')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('shared=0,1,2,3', result.stdout)

    def test_fixed_boot_isolation_refuses_baseline(self):
        (self.cpu / 'isolated').write_text('3\n')
        self.assertIn('fixed boot isolation', self.check().stderr)

    def test_offline_missing_or_reserved_cpu_rejected(self):
        for available, dedicated in [('0-2', '3'), ('0-4', '3'), ('3', '3')]:
            with self.subTest(available=available):
                self.assertNotEqual(self.check(available, dedicated).returncode, 0)
        (self.cgroup / 'cpuset.cpus.effective').write_text('0-2\n')
        self.assertIn('complete available CPU set', self.check().stderr)

    def test_controller_rps_and_threaded_napi_checks(self):
        (self.cgroup / 'cgroup.controllers').write_text('cpu pids\n')
        self.assertIn('controller is unavailable', self.check().stderr)
        (self.cgroup / 'cgroup.controllers').write_text('cpuset cpu pids\n')
        (self.net / 'queues/rx-0/rps_cpus').write_text('f\n')
        self.assertIn('RPS must be disabled', self.check().stderr)
        (self.net / 'queues/rx-0/rps_cpus').write_text('0\n')
        (self.net / 'threaded').write_text('1\n')
        self.assertIn('Separate threaded NAPI', self.check().stderr)

    def test_bad_cpu_syntax_is_rejected_without_writes(self):
        for available in ['0,,1', '2-0', '0,1,', '0-99999', '-1', '0,0,3']:
            with self.subTest(available=available):
                self.assertNotEqual(self.check(available).returncode, 0)

    def test_acquire_release_restores_all_cpus_and_controller(self):
        result = self.lifecycle('partition_main acquire 4242 0-3 3 "$STATE"\npartition_main release 4242 "$STATE"\npartition_main release 4242 "$STATE"')
        self.assertEqual(result.returncode, 0, result.stderr)
        active = json.loads((self.state / 'cpu-partition-active.json').read_text())
        restored = json.loads((self.state / 'cpu-partition-restored.json').read_text())
        self.assertEqual(active['general_cpus'], '0,1,2')
        self.assertEqual(active['partition_cpus'], '3')
        self.assertEqual(active['partition'], 'isolated')
        self.assertTrue(active['verified'])
        self.assertEqual(restored['root_cpus'], '0,1,2,3')
        self.assertFalse(restored['root_cpuset_enabled'])
        self.assertFalse(self.group.exists())
        self.assertEqual((self.cgroup / 'cgroup.subtree_control').read_text(), 'cpu pids')

    def test_invalid_partition_and_signal_roll_back(self):
        for failure in ['invalid', 'signal']:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=self.root) as state:
                result = self.lifecycle('partition_main acquire 4242 0-3 3 "$STATE"',
                                        f'STATE={str(state)!r}\nFAIL_PARTITION={failure}')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.group.exists())
                self.assertEqual((self.cgroup / 'cpuset.cpus.effective').read_text(), '0-3')
                self.assertNotIn('cpuset', (self.cgroup / 'cgroup.subtree_control').read_text())

    def test_release_refuses_live_children_until_they_exit(self):
        result = self.lifecycle('partition_main acquire 4242 0-3 3 "$STATE"\nprintf 1234 > "$CGROUP_ROOT/ace-rx-4242/cgroup.procs"\npartition_main release 4242 "$STATE"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('live processes', result.stderr)
        self.assertTrue(self.group.exists())
        (self.group / 'cgroup.procs').write_text('')
        result = self.lifecycle('partition_main release 4242 "$STATE"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.group.exists())

    def test_preserves_preexisting_controller_and_other_consumers(self):
        (self.cgroup / 'cgroup.subtree_control').write_text('cpu pids cpuset\n')
        result = self.lifecycle('partition_main acquire 4242 0-3 3 "$STATE"\npartition_main release 4242 "$STATE"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cpuset', (self.cgroup / 'cgroup.subtree_control').read_text())
        self.assertFalse(json.loads((self.state / 'cpu-partition-active.json').read_text())['root_cpuset_added'])

    def test_new_external_controller_use_is_preserved(self):
        result = self.lifecycle('partition_main acquire 4242 0-3 3 "$STATE"\nmkdir "$CGROUP_ROOT/external"\nprintf 0 > "$CGROUP_ROOT/external/cpuset.cpus"\npartition_main release 4242 "$STATE"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cpuset', (self.cgroup / 'cgroup.subtree_control').read_text())
        self.assertEqual((self.cgroup / 'external/cpuset.cpus').read_text(), '0')
        self.assertEqual((self.cgroup / 'cpuset.cpus.effective').read_text(), '0-3')

    def test_refuses_unowned_group_and_existing_state(self):
        self.group.mkdir()
        result = self.lifecycle('partition_main release 4242 "$STATE"')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.group.exists())
        result = self.lifecycle('partition_main acquire 4242 0-3 3 "$STATE"')
        self.assertNotEqual(result.returncode, 0)
        self.group.rmdir()
        (self.state / 'cpu-partition.state').write_text('other owner\n')
        result = self.lifecycle('partition_main acquire 4242 0-3 3 "$STATE"')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.group.exists())
        self.assertEqual((self.state / 'cpu-partition.state').read_text(), 'other owner\n')

    def test_release_without_acquire_is_idempotent(self):
        result = self.lifecycle('partition_main release 4242 "$STATE"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.state.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
