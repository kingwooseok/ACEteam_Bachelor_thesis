import json
import tempfile
from pathlib import Path
import unittest
from tests.test_design_plan import plan


class NovemberProfileTests(unittest.TestCase):
    def test_complete_plan_uses_adopted_settings_and_modes(self):
        directory=Path(__file__).resolve().parents[1]/'experiment/design'
        profile=json.loads((directory/'profile.pi5-20261104.json').read_text())
        plan.validate(profile)
        rows=plan.rows(profile=profile)
        self.assertEqual(sum(len(row['modes'])*plan.REPEATS for row in rows),75)
        for index,row in enumerate(rows):
            values=dict(line.split('=',1) for line in plan.config(profile,row,index).splitlines()
                        if line and not line.startswith('#'))
            self.assertEqual(values['load.cpu_workers'],'2')
            self.assertEqual(values['flow3.rx_priority'],'49')
            self.assertEqual(values['flow3.rx_policy'],'fifo')
            self.assertEqual(values['receiver.xsk_busy'],'1')
            self.assertEqual(values['measurement.phc_tracking'],'1')
            self.assertEqual(values['environment.rx_coalesce_usecs'],'0')
            self.assertEqual(values['load.network_bps'],'800000000' if row['network']=='high' else '0')
            for flow,period in enumerate((1000000,10000000,100000000),1):
                enabled=not row['single_flow'] or row['single_flow']==flow
                self.assertEqual(values[f'flow{flow}.enabled'],str(int(enabled)))
                if enabled:self.assertEqual(values[f'flow{flow}.period_ns'],str(period))
        for key,bad in [('flow_pps',[1000,0,10]),('load_workers',0),('xsk_busy',2),('rx_coalesce_usecs',-1)]:
            with self.subTest(key=key),self.assertRaises(ValueError):
                plan.validate({**profile,key:bad})
