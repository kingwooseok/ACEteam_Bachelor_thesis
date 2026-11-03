"""Environment setup failures must not silently start an invalid measurement.

IRQ/governor fixtures are private files, never the host's proc/sys. Physical Pi
application and restoration are recorded separately in the validation report.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MONITOR = ROOT / 'monitor'


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ace-environment-')
        self.root = Path(self.temp.name)
        (self.root / 'sys/class/net/eth0').mkdir(parents=True)
        cpu = self.root / 'sys/devices/system/cpu'
        cpu.mkdir(parents=True)
        (cpu / 'online').write_text('0-3\n')
        self.proc = self.root / 'proc'
        self.proc.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def irq(self, number, requested='0-3', effective='2'):
        base = self.proc / 'irq' / str(number)
        base.mkdir(parents=True)
        (base / 'smp_affinity_list').write_text(requested + '\n')
        (base / 'effective_affinity_list').write_text(effective + '\n')

    def call_irq(self, *args):
        command = 'source "$1"; PROC_ROOT="$2/proc"; SYS_ROOT="$2/sys"; shift 2; main "$@"'
        return subprocess.run(['bash', '-c', command, 'fixture', str(MONITOR / 'set_irq_affinity.sh'),
                               str(self.root), *args], text=True, capture_output=True, timeout=5)

    def test_exact_interface_match_ignores_eth01_and_veth0(self):
        (self.proc / 'interrupts').write_text(
            ' CPU0 CPU1 CPU2 CPU3\n'
            ' 8: 1 0 0 0 chip eth0\n'
            ' 9: 1 0 0 0 chip eth01\n'
            '10: 1 0 0 0 chip veth0\n')
        self.irq(8, '2'); self.irq(9); self.irq(10)
        p = self.call_irq('eth0', '2', '--check')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('IRQ 8:', p.stdout)
        self.assertNotIn('IRQ 9:', p.stdout)

    def test_check_refuses_effective_mismatch_without_writing(self):
        (self.proc / 'interrupts').write_text('8: 1 0 0 0 chip eth0\n')
        self.irq(8, '2', '0-3')
        p = self.call_irq('eth0', '2', '--check')
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual((self.proc / 'irq/8/smp_affinity_list').read_text(), '2\n')

    def test_failed_second_irq_restores_first_request(self):
        (self.proc / 'interrupts').write_text('8: 1 0 0 0 chip eth0-rx-0\n9: 1 0 0 0 chip eth0-rx-1\n')
        self.irq(8); self.irq(9, effective='0-3')
        p = self.call_irq('eth0', '2')
        self.assertNotEqual(p.returncode, 0)
        for n in [8, 9]:
            self.assertEqual((self.proc / f'irq/{n}/smp_affinity_list').read_text(), '0-3\n')

    def test_msi_irqs_are_used_even_without_interface_labels(self):
        msi = self.root / 'sys/class/net/eth0/device/msi_irqs'
        msi.mkdir(parents=True)
        (msi / '12').touch()
        (self.proc / 'interrupts').write_text('12: 1 0 0 0 chip unrelated-label\n')
        self.irq(12, '2')
        p = self.call_irq('eth0', '2', '--check')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('IRQ 12:', p.stdout)

    def test_offline_cpu_is_rejected_before_mutation(self):
        (self.proc / 'interrupts').write_text('8: 1 0 0 0 chip eth0\n')
        self.irq(8)
        p = self.call_irq('eth0', '7')
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual((self.proc / 'irq/8/smp_affinity_list').read_text(), '0-3\n')

    def policy(self, index=0):
        policy = self.root / f'sys/devices/system/cpu/cpufreq/policy{index}'
        policy.mkdir(parents=True)
        (policy / 'scaling_governor').write_text('ondemand\n')
        (policy / 'scaling_available_governors').write_text('ondemand performance\n')
        return policy

    def call_prepare(self, role, governor='performance'):
        command = 'source "$1"; SYS_ROOT="$2/sys"; SCRIPT_DIR="$2/helpers"; main "$3" eth0 "$4" 2'
        return subprocess.run(['bash', '-c', command, 'fixture', str(MONITOR / 'prepare_env.sh'),
                               str(self.root), role, governor], text=True, capture_output=True, timeout=5)

    def test_sender_governor_preparation_does_not_touch_irq(self):
        policy = self.policy()
        p = self.call_prepare('sender')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((policy / 'scaling_governor').read_text(), 'performance\n')

    def test_irq_failure_restores_all_changed_governors(self):
        policies = [self.policy(0), self.policy(1)]
        helper = self.root / 'helpers'
        helper.mkdir()
        (helper / 'set_irq_affinity.sh').write_text('#!/bin/bash\nexit 1\n')
        p = self.call_prepare('receiver')
        self.assertNotEqual(p.returncode, 0)
        for policy in policies:
            self.assertEqual((policy / 'scaling_governor').read_text(), 'ondemand\n')

    def test_unsupported_governor_never_changes_policy(self):
        policy = self.policy()
        p = self.call_prepare('sender', 'unsupported')
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual((policy / 'scaling_governor').read_text(), 'ondemand\n')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux state collector')
    def test_state_collector_does_not_overwrite_existing_evidence(self):
        destination = self.root / 'state.txt'
        destination.write_text('keep this evidence\n')
        p = subprocess.run(['bash', str(MONITOR / 'capture_state.sh'), 'lo', str(destination)],
                           text=True, capture_output=True, timeout=5)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(destination.read_text(), 'keep this evidence\n')


if __name__ == '__main__':
    unittest.main()
