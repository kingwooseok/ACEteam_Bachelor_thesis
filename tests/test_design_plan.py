"""실행표 중복 제거·미확정 값 차단·공통 실험 조건 회귀 검사."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'experiment/design/build_plan.py'
spec = importlib.util.spec_from_file_location('design_plan', SCRIPT)
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


class DesignPlanTests(unittest.TestCase):
    def profile(self):
        # 회귀 검사용 값이다. 연구에서 확정한 정책/네트워크 세기가 아니다.
        return dict(receiver_address='192.168.10.10', receiver_cpus=[0, 3, 1],
                    receiver_policies=['other', 'rr', 'rr'], receiver_priorities=[0, 80, 80],
                    sender_policies=['other']*3, sender_priorities=[0]*3,
                    receiver_supervisor_cpu=0, sender_supervisor_cpu=3,
                    receiver_network_cpu=0, network_high_bps=800000000,
                    network_middle_bps=400000000, pass_high_pps=9000, settle_ms=100)

    def test_document_selected_starting_profile(self):
        profile = json.loads(plan.PROFILE.read_text())
        self.assertEqual(profile['receiver_cpus'], [0, 3, 1])
        for side in ('receiver', 'sender'):
            self.assertEqual(profile[f'{side}_policies'], ['other', 'fifo', 'fifo'])
            self.assertEqual(profile[f'{side}_priorities'], [0, 80, 80])
        # 네트워크 측정값을 임의로 채우지 않고 생성 차단을 유지한다.
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            plan.validate(profile)
        profile.update(network_high_bps=800000000, network_middle_bps=400000000,
                       pass_high_pps=9000, settle_ms=100)
        plan.validate(profile)
        values = dict(line.split('=', 1) for line in plan.config(profile, plan.rows()[3], 3).splitlines()
                      if line and not line.startswith('#'))
        self.assertEqual(values['flow2.rx_policy'], 'fifo')
        self.assertEqual(values['flow2.rx_priority'], '80')
        self.assertEqual(values['flow3.rx_cpu'], '1')

    def test_deduplicated_main_and_single_conditions(self):
        rows = plan.rows()
        self.assertEqual(len(rows), 18)
        main = [r for r in rows if not r['single_flow']]
        self.assertEqual(len(main), 15)
        signatures = {(r['pass_pps'], r['cpu_percent'], r['network'],
                       r['load_cpus'], r['pass_shares_cpu3']) for r in main}
        self.assertEqual(len(signatures), 15)
        shared_base = next(r for r in main if 'C-base' in r['references'])
        self.assertEqual(len(shared_base['references']), 3)

    def test_unresolved_profile_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            result = subprocess.run([sys.executable, str(SCRIPT), '--profile', str(plan.PROFILE),
                                     '--output', str(output)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unresolved', result.stderr)
            self.assertFalse(output.exists())

    def test_draft_has_no_executable_configs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            subprocess.run([sys.executable, str(SCRIPT), '--output', str(output)],
                           check=True, capture_output=True)
            self.assertEqual(list(output.glob('*.conf')), [])
            self.assertEqual(json.loads((output/'plan.json').read_text())['status'], 'draft-unresolved')

    def test_filled_plan_preserves_all_adopted_values_and_capacity(self):
        profile = self.profile()
        plan.validate(profile)
        for index, row in enumerate(plan.rows()):
            values = dict(line.split('=', 1) for line in plan.config(profile, row, index).splitlines()
                          if line and not line.startswith('#'))
            for key, value in {'repeats':'5', 'duration_ms':'60000', 'warmup_ms':'5000',
                               'flow2.deadline_ns':'100000', 'flow3.deadline_ns':'100000',
                               'sender.network_cpu':'3', 'receiver.network_cpu':'0',
                               'load.memory_workers':'0', 'load.io_workers':'0',
                               'alternate_mode_order':'1'}.items():
                self.assertEqual(values[key], value)
            count = sum((60000000000 + int(values[f'flow{i}.period_ns']) - 1) //
                        int(values[f'flow{i}.period_ns']) for i in (1, 2, 3)
                        if values[f'flow{i}.enabled'] == '1')
            self.assertLessEqual(count, 1048576)
            if row['single_flow']:
                self.assertEqual(sum(values[f'flow{i}.enabled']=='1' for i in (1, 2, 3)), 1)
            if not row['single_flow'] and not row['pass_pps']:
                self.assertEqual(values['flow1.enabled'], '0')

    def test_invalid_profile_rejected(self):
        for key, value in [('network_high_bps', True), ('network_middle_bps', 900000000),
                           ('receiver_network_cpu', 3), ('receiver_cpus', [0, 2, 1]),
                           ('receiver_priorities', [80, 80, 80]), ('pass_high_pps', 16000)]:
            profile = self.profile()
            profile[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                plan.validate(profile)

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory)/'evidence.txt'
            marker.write_text('original')
            result = subprocess.run([sys.executable, str(SCRIPT), '--output', directory],
                                    capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
