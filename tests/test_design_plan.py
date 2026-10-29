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


    def test_full_design_is_explicit_and_preserves_legacy_singles(self):
        profile = self.profile()
        for high, count in ((9000, 18), (12000, 20), (15476, 20)):
            profile['pass_high_pps'] = high
            plan.validate(profile, full_design=True)
            rows = plan.rows(high, full_design=True)
            self.assertEqual(len(rows), count)
            references = [label for row in rows for label in row['references']]
            self.assertEqual(len(references), len(set(references)))
            for cpu in (0, 100):
                self.assertIn(f'B-pass9000-cpu{cpu}', references)
                self.assertIn(f'B-pass{high}-cpu{cpu}', references)
            for index, row in enumerate(rows):
                text = plan.config(profile, row, index, full_design=True)
                values = dict(line.split('=', 1) for line in text.splitlines() if not line.startswith('#'))
                self.assertEqual(set(values['modes'].split(',')), {'all-pass', 'mixed'})
                self.assertEqual(values['verify_placement'], '1')
                self.assertEqual(values['repeats'], '5')
                capacity = 0
                for flow in (1, 2, 3):
                    if values[f'flow{flow}.enabled'] == '1':
                        period = int(values[f'flow{flow}.period_ns'])
                        packets = (60000000000 + period - 1) // period
                        capacity += packets
                        if flow in (2, 3):
                            self.assertEqual(packets, 60000)
                self.assertLessEqual(capacity, 1048576)
        legacy = plan.config(self.profile(), plan.rows()[0], 0)
        self.assertIn('modes=mixed\n', legacy)
        self.assertNotIn('verify_placement=', legacy)
        for high in (8999, 15477):
            profile['pass_high_pps'] = high
            with self.assertRaises(ValueError):
                plan.validate(profile, full_design=True)

    def test_full_design_wrapper_manifest_and_tamper_rejection(self):
        import types
        from unittest.mock import patch
        wrapper = ROOT / 'experiment/run_matrix.sh'
        source = wrapper.read_text().split("<<'PYTHON'\n", 1)[1].rsplit('\nPYTHON', 1)[0]
        module = types.ModuleType('matrix_wrapper')
        with patch.object(sys, 'argv', ['-', str(wrapper)]):
            exec(compile(source, str(wrapper), 'exec'), module.__dict__)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / 'profile.json'
            profile.write_text(json.dumps(self.profile()))
            output = root / 'plan'
            subprocess.run([str(wrapper), 'plan', '--profile', str(profile), '--output', str(output)],
                           check=True, capture_output=True)
            cases = module.load_cases(output)
            self.assertEqual(len(cases), 18)
            self.assertEqual(sum(c['expected_runs'] for c in cases), 180)
            # Wrapper validates the entire plan before any native receiver is launched.
            with patch.object(module.subprocess, 'run') as validate, \
                 patch.object(module.subprocess, 'Popen') as popen:
                popen.return_value.wait.return_value = 1
                popen.return_value.poll.return_value = 1
                with self.assertRaisesRegex(RuntimeError, 'matrix stopped'):
                    module.execute('receiver', output, cases)
                self.assertEqual(validate.call_count, 18)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(popen.call_args.args[0][1], 'receiver')
                self.assertTrue(popen.call_args.args[0][0].endswith('/experiment/ace-run'))
            (output / cases[0]['config']).write_text('changed')
            with self.assertRaisesRegex(ValueError, 'regenerate plan'):
                module.load_cases(output)


    def test_matrix_collection_reuses_existing_analysis_and_rejects_missing_repeat(self):
        import contextlib
        import io
        import types
        from unittest.mock import patch
        from analysis.tests.test_experiment import ExperimentTests
        from analysis.tests.test_join_results import CPUMAP_HEADER
        wrapper = ROOT / 'experiment/run_matrix.sh'
        source = wrapper.read_text().split("<<'PYTHON'\n", 1)[1].rsplit('\nPYTHON', 1)[0]
        module = types.ModuleType('matrix_collection')
        with patch.object(sys, 'argv', ['-', str(wrapper)]):
            exec(compile(source, str(wrapper), 'exec'), module.__dict__)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rx, tx, plan_dir = root/'rx', root/'tx', root/'plan'
            plan_dir.mkdir()
            case = dict(case_id='load0', sha256='fixture', expected_runs=10)
            (plan_dir/'execution.json').write_text(json.dumps(dict(cases=[case])))
            for mode in ('all-pass', 'mixed'):
                for repeat in range(1, 6):
                    name = f'load0-{mode}-r{repeat:04d}'
                    for base, role in ((rx, 'receiver'), (tx, 'sender')):
                        run = ExperimentTests().make_run(base/'load0'/role/'runs', name)
                        descriptor = json.loads((run/'run.json').read_text())
                        descriptor.update(mode=mode, repeat=repeat, config={'sha256':'fixture'})
                        if mode == 'all-pass':
                            descriptor['flows'][1]['requested_path'] = 'pass'
                            path = run/'xdp-ingress_records.csv'
                            path.write_text(path.read_text().replace('19,1,0', '19,0,0'))
                            (run/'xdp-cpumap_records.csv').write_text(CPUMAP_HEADER)
                        (run/'run.json').write_text(json.dumps(descriptor))
                        if role == 'receiver':
                            for path in run.glob('sender-flow*.json'):
                                path.unlink()
            output = root/'collected'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.collect(plan_dir, [case], rx, tx, output), 0)
            report = json.loads((output/'analysis-report/report.json').read_text())
            self.assertEqual(len(report['runs']), 10)
            self.assertEqual(len(report['conditions']), 2)
            for condition in report['conditions']:
                self.assertEqual(condition['status'], 'completed')
                self.assertEqual(condition['delivery']['2']['sent'], 15)
                self.assertEqual(condition['delivery']['2']['received_unique'], 5)
                self.assertEqual(condition['delivery']['2']['loss_count'], 10)
                self.assertEqual(condition['statistics']['flows']['2']['run_count'], 5)
            import shutil
            for base, role in ((rx, 'receiver'), (tx, 'sender')):
                shutil.rmtree(base/'load0'/role/'runs'/'load0-mixed-r0005')
            incomplete = root/'incomplete'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.collect(plan_dir, [case], rx, tx, incomplete), 1)
            report = json.loads((incomplete/'analysis-report/report.json').read_text())
            self.assertEqual(report['conditions'], [])
            self.assertEqual(report['runs'][0]['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
