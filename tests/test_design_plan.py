"""실행표 중복 제거·미확정 값 차단·공통 실험 조건 회귀 검사."""
import importlib.util
import hashlib
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
    def setUp(self):
        # Linux runs real directory fsync; Windows still exercises the complete
        # generation/collection/analysis chain with that OS-specific call stubbed.
        if sys.platform == 'win32':
            import contextlib
            import os
            from unittest.mock import patch
            from analysis import join_results, aggregate_runs
            stack = self.enterContext(contextlib.ExitStack())
            stack.enter_context(patch.object(os, 'O_CLOEXEC', 0, create=True))
            stack.enter_context(patch.object(join_results, 'fsync_directories'))
            stack.enter_context(patch.object(aggregate_runs, 'fsync_directories'))

    def profile(self):
        # 회귀 검사용 값이다. 연구에서 확정한 정책/네트워크 세기가 아니다.
        return dict(receiver_address='192.168.10.10', receiver_shared_cpus=[0, 1, 2, 3],
                    receiver_cpumap_cpu=3,
                    receiver_policies=['other']*3, receiver_priorities=[0]*3,
                    sender_policies=['other']*3, sender_priorities=[0]*3,
                    receiver_supervisor_cpu='inherit', sender_supervisor_cpu=3,
                    receiver_network_cpu='inherit', network_high_bps=800000000,
                    pass_high_pps=9000, settle_ms=100)

    def test_document_selected_starting_profile(self):
        profile = json.loads(plan.PROFILE.read_text())
        self.assertEqual(profile['receiver_shared_cpus'], [0, 1, 2, 3])
        self.assertEqual(profile['receiver_cpumap_cpu'], 3)
        self.assertEqual(profile['receiver_policies'], ['other']*3)
        self.assertEqual(profile['receiver_priorities'], [0]*3)
        self.assertEqual(profile['sender_policies'], ['other', 'fifo', 'fifo'])
        self.assertEqual(profile['sender_priorities'], [0, 80, 80])
        # 네트워크 측정값을 임의로 채우지 않고 생성 차단을 유지한다.
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            plan.validate(profile)
        self.assertNotIn('network_middle_bps', profile)
        profile.update(network_high_bps=800000000, pass_high_pps=9000, settle_ms=100)
        plan.validate(profile)
        values = dict(line.split('=', 1) for line in plan.config(profile, plan.rows()[3], 3).splitlines()
                      if line and not line.startswith('#'))
        self.assertEqual(values['receiver.shared_cpus'], '0,1,2,3')
        self.assertEqual(values['receiver.cpumap_cpu'], '3')
        self.assertEqual(values['receiver.supervisor_cpu'], 'inherit')
        self.assertEqual(values['receiver.network_cpu'], 'inherit')
        for flow in (1, 2, 3):
            self.assertEqual(values[f'flow{flow}.rx_policy'], 'other')
            self.assertEqual(values[f'flow{flow}.rx_priority'], '0')
            self.assertEqual(values[f'flow{flow}.rx_cpu'], 'inherit')
            self.assertNotIn(f'flow{flow}.all_pass_rx_cpu', values)

    def test_deduplicated_main_and_single_conditions(self):
        rows = plan.rows()
        self.assertEqual(len(rows), 13)
        main = [r for r in rows if not r['single_flow']]
        self.assertEqual(len(main), 10)
        signatures = {(r['pass_pps'], r['cpu_percent'], r['network'],
                       r['load_cpus']) for r in main}
        self.assertEqual(len(signatures), 10)
        shared_base = next(r for r in main if 'A-cpu100-none' in r['references'])
        self.assertEqual(shared_base['references'], ['A-cpu100-none', 'B-pass1000-cpu100'])
        self.assertEqual(shared_base['load_cpus'], 'per-mode-general')
        self.assertTrue(all(row['modes'] == ['all-pass', 'pass-xsk', 'mixed'] for row in main))
        self.assertTrue(all(row['load_cpus'] == 'per-mode-general' for row in rows))
        self.assertTrue(all(row['load_workers'] == 3 for row in rows))
        self.assertEqual([row['modes'] for row in rows[:3]], [['all-pass'], ['mixed'], ['pass-xsk']])
        self.assertFalse(any(label.startswith('C-') for row in rows for label in row['references']))
        self.assertTrue(all('pass_shares_cpu3' not in row for row in rows))
        self.assertEqual(sum(len(row['modes']) * 5 for row in rows), 165)
        self.assertTrue(all(row['cpu_percent'] in (0, 100) and
                            row['network'] in ('none', 'high') for row in rows))

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
                               'sender.network_cpu':'3', 'receiver.network_cpu':'inherit',
                               'receiver.shared_cpus':'0,1,2,3', 'receiver.cpumap_cpu':'3',
                               'load.cpu_cpus':'0,1,2,3', 'load.cpu_layout':'shared',
                               'load.cpu_workers':'3',
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
                           ('receiver_network_cpu', 3), ('receiver_supervisor_cpu', 0),
                           ('receiver_shared_cpus', [0, 1, 3]), ('receiver_shared_cpus', [0, 1]),
                           ('receiver_shared_cpus', [0, 1, 2]),
                           ('receiver_shared_cpus', [False, 1, 2]), ('receiver_cpumap_cpu', 2),
                           ('receiver_cpus', [0, 3, 1]),
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


    def test_measured_high_replaces_provisional_9000_and_singles_stay_single_path(self):
        profile = self.profile()
        for high in (3001, 7000, 9000, 12000, 15476):
            profile['pass_high_pps'] = high
            plan.validate(profile, full_design=True)
            rows = plan.rows(high, full_design=True)
            self.assertEqual(len(rows), 13)
            references = [label for row in rows for label in row['references']]
            self.assertEqual(len(references), len(set(references)))
            for cpu in (0, 100):
                self.assertIn(f'B-pass{high}-cpu{cpu}', references)
                if high != 9000:
                    self.assertNotIn(f'B-pass9000-cpu{cpu}', references)
            for index, row in enumerate(rows):
                text = plan.config(profile, row, index, full_design=True)
                values = dict(line.split('=', 1) for line in text.splitlines() if not line.startswith('#'))
                self.assertEqual(set(values['modes'].split(',')), set(row['modes']))
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
        self.assertIn('modes=all-pass\n', legacy)
        self.assertIn('verify_placement=1', legacy)
        self.assertEqual(plan.rows(), plan.rows(full_design=True))
        for high in (3000, 15477):
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
            # Exercise PC-side generation on Windows as well as Linux, then
            # pass the unchanged files to the actual execution-plan loader.
            subprocess.run([sys.executable, str(SCRIPT), '--d01-d11',
                            '--profile', str(profile), '--output', str(output)],
                           check=True, capture_output=True)
            cases = module.load_cases(output)
            self.assertEqual(len(cases), 13)
            self.assertEqual(sum(c['expected_runs'] for c in cases), 165)
            self.assertEqual({c['expected_runs'] for c in cases}, {5, 15})
            summary = json.loads((output/'plan.json').read_text())
            self.assertEqual(summary['single_runs'], 15)
            self.assertEqual(summary['main_runs'], 150)
            self.assertEqual(summary['total_runs'], 165)
            self.assertEqual(summary['design_revision'], 'mode-partition-165-v3')
            self.assertEqual([summary[key] for key in ('all_pass_runs', 'pass_xsk_runs', 'mixed_runs')], [55]*3)
            self.assertEqual(summary['cpu_domains'], {'shared':'0,1,2,3', 'cpumap_active_general':'0,1,2', 'cpumap':'3'})
            self.assertEqual(summary['load_workers'], 3)
            for case in cases:
                data = (output / case['config']).read_bytes()
                self.assertNotIn(b'\r', data)
                self.assertEqual(hashlib.sha256(data).hexdigest(), case['sha256'])
            # Wrapper validates the entire plan before any native receiver is launched.
            with patch.object(module.subprocess, 'run') as validate, \
                 patch.object(module.subprocess, 'Popen') as popen:
                popen.return_value.wait.return_value = 1
                popen.return_value.poll.return_value = 1
                with self.assertRaisesRegex(RuntimeError, 'matrix stopped'):
                    module.execute('receiver', output, cases)
                self.assertEqual(validate.call_count, 13)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(popen.call_args.args[0][1], 'receiver')
                self.assertEqual(Path(popen.call_args.args[0][0]), ROOT / 'experiment/ace-run')
            (output / cases[0]['config']).write_text('changed')
            with self.assertRaisesRegex(ValueError, 'regenerate plan'):
                module.load_cases(output)

    def test_execution_metadata_must_match_saved_modes_and_repeats(self):
        import copy
        import types
        from unittest.mock import patch
        wrapper = ROOT / 'experiment/run_matrix.sh'
        source = wrapper.read_text().split("<<'PYTHON'\n", 1)[1].rsplit('\nPYTHON', 1)[0]
        module = types.ModuleType('matrix_metadata')
        with patch.object(sys, 'argv', ['-', str(wrapper)]):
            exec(compile(source, str(wrapper), 'exec'), module.__dict__)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root/'profile.json'
            profile.write_text(json.dumps(self.profile()))
            output = root/'plan'
            # The old flag remains optional; both routes publish the same design.
            subprocess.run([sys.executable, str(SCRIPT), '--profile', str(profile),
                            '--output', str(output)], check=True, capture_output=True)
            original = json.loads((output/'execution.json').read_text())
            self.assertEqual(len(module.load_cases(output)), 13)
            for change in (
                    {'expected_modes': ['mixed', 'mixed'], 'expected_runs': 10},
                    {'expected_modes': ['invalid']}, {'repeats': True},
                    {'expected_runs': 10},
                    {'expected_modes': ['mixed']},
                    {'repeats': 4, 'expected_runs': 4}):
                execution = copy.deepcopy(original)
                execution['cases'][0].update(change)
                (output/'execution.json').write_text(json.dumps(execution))
                with self.subTest(change=change), self.assertRaises(ValueError):
                    module.load_cases(output)
            execution = copy.deepcopy(original)
            del execution['design_revision']
            (output/'execution.json').write_text(json.dumps(execution))
            with self.assertRaisesRegex(ValueError, 'regenerate plan'):
                module.load_cases(output)
            execution = copy.deepcopy(original)
            execution['design_revision'] = 'minimal-135-v1'
            (output/'execution.json').write_text(json.dumps(execution))
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
            case = dict(case_id='load0', sha256='fixture', expected_runs=15,
                        expected_modes=['all-pass', 'pass-xsk', 'mixed'], repeats=5)
            (plan_dir/'execution.json').write_text(json.dumps(dict(cases=[case])))
            for mode in case['expected_modes']:
                for repeat in range(1, 6):
                    name = f'load0-{mode}-r{repeat:04d}'
                    for base, role in ((rx, 'receiver'), (tx, 'sender')):
                        run = ExperimentTests().make_run(base/'load0'/role/'runs', name)
                        descriptor = json.loads((run/'run.json').read_text())
                        descriptor.update(mode=mode, repeat=repeat, config={'sha256':'fixture'})
                        if mode in ('all-pass', 'pass-xsk'):
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
            self.assertEqual(len(report['runs']), 15)
            self.assertEqual(len(report['conditions']), 3)
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
