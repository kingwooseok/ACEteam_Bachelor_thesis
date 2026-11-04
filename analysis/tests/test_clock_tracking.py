import json
import tempfile
import unittest
from pathlib import Path
from . import test_experiment as fixtures
from .test_join_results import CALIBRATION_HEADER
from analysis import analyze_experiment as experiment


class ClockTrackingTests(unittest.TestCase):
    setUp = fixtures.ExperimentTests.setUp
    make_run = fixtures.ExperimentTests.make_run
    def continuous(self, directory):
        run = self.make_run(Path(directory), 'continuous')
        descriptor = json.loads((run/'run.json').read_text())
        descriptor['environment'] = {'phc_tracking': 1}
        (run/'run.json').write_text(json.dumps(descriptor), encoding='utf-8')
        (run/'phc-during.csv').write_text(CALIBRATION_HEADER +
            '0,0,490,600,510,20,100,1\n1,0,4990,5100,5010,20,100,1\n', encoding='utf-8')
        return run

    def test_continuous_capture_and_packet_loss_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.continuous(directory)
            (run/'phc-before.csv').unlink(); (run/'phc-after.csv').unlink()
            result, loaded, _ = experiment.inspect_run(run)
            self.assertEqual(result['status'], 'completed', result.get('reason'))
            self.assertTrue(result['clock_validation']['accepted'])
            self.assertEqual(result['clock_validation']['unreceived_packets'], 1)
            self.assertIsNotNone(loaded)
            self.assertEqual(experiment.inspect_run(run)[0]['status'], 'completed')

    def test_continuous_capture_missing_is_not_replaced_by_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.continuous(directory); (run/'phc-during.csv').unlink()
            result, loaded, _ = experiment.inspect_run(run)
            self.assertEqual(result['status'], 'analysis-failed')
            self.assertIsNone(loaded)

    def test_out_of_bracket_capture_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.continuous(directory)
            (run/'phc-during.csv').write_text(CALIBRATION_HEADER+
                '0,0,490,600,510,20,100,1\n1,0,990,1100,1010,20,100,1\n',encoding='utf-8')
            result, loaded, _ = experiment.inspect_run(run)
            self.assertEqual(result['status'], 'analysis-failed')
            self.assertFalse(result['clock_validation']['covered'])
            self.assertIsNone(loaded)

    def test_long_gap_is_rejected_despite_endpoint_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.continuous(directory)
            (run/'phc-during.csv').write_text(CALIBRATION_HEADER+
                '0,0,490,600,510,20,100,1\n1,0,600000490,600000600,600000510,20,100,1\n',encoding='utf-8')
            result, loaded, _ = experiment.inspect_run(run)
            self.assertEqual(result['status'], 'analysis-failed')
            self.assertTrue(result['clock_validation']['covered'])
            self.assertEqual(result['clock_validation']['max_gap_in_capture_ms'], 600)
            self.assertIsNone(loaded)

    def test_negative_packet_latency_is_retained_and_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.continuous(directory)
            (run/'phc-during.csv').write_text(CALIBRATION_HEADER+
                '0,0,490,300,510,20,-200,1\n1,0,4990,4800,5010,20,-200,1\n',encoding='utf-8')
            result, loaded, _ = experiment.inspect_run(run)
            self.assertEqual(result['status'], 'analysis-failed')
            self.assertGreater(result['clock_validation']['negative_d1'], 0)
            self.assertIsNone(loaded)
            self.assertIn('-200', (run/'analysis/joined.csv').read_text())


