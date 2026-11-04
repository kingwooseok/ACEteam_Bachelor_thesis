"""Exercise saved pre-attach settings, hardware readback and restoration."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
BASH=os.environ.get('ACE_BASH_FOR_TEST') or shutil.which('bash')


@unittest.skipUnless(BASH, 'bash required')
class CoalescingTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory())
        self.root=Path(self.temp)
        self.run=self.root/'run'; self.run.mkdir()
        self.state=self.root/'state'; self.state.write_text('17')
        mock=self.root/'ethtool'
        mock.write_text('''#!/bin/sh
set -eu
if [ "$1" = -c ]; then
  printf 'rx-usecs: %s\n' "$(cat "$MOCK_COALESCE_STATE")"
elif [ "$1" = -C ]; then
  value=$4
  if [ "${MOCK_REJECT_ZERO:-0}" = 1 ] && [ "$value" = 0 ]; then exit 1; fi
  if [ "$value" != 0 ]; then value=$((value-1)); fi
  printf '%s' "$value" > "$MOCK_COALESCE_STATE"
else exit 2
fi
''',encoding='utf-8',newline='\n')
        mock.chmod(0o755)
        self.env={**os.environ,'PATH':str(self.root)+os.pathsep+os.environ['PATH'],
                  'MOCK_COALESCE_STATE':self.state.as_posix()}

    def call(self,action,check=True):
        return subprocess.run([BASH,str(ROOT/'monitor/rx_coalesce.sh'),action,'eth0',str(self.run),'0'],
                              env=self.env,check=check,capture_output=True,text=True,timeout=10)

    def test_pre_attach_setting_survives_driver_reset_and_quantization(self):
        self.call('save')
        self.state.write_text('49')  # XDP attachment reset after the saved value.
        self.call('apply')
        self.assertEqual(self.state.read_text(),'0')
        self.assertTrue(json.loads((self.run/'coalesce-active.json').read_text())['verified'])
        self.call('restore')
        self.assertEqual(self.state.read_text(),'17')
        self.call('restore')
        self.assertEqual(self.state.read_text(),'17')

    def test_failure_retains_original_setting_for_cleanup(self):
        self.call('save'); self.state.write_text('49')
        self.env['MOCK_REJECT_ZERO']='1'
        self.assertNotEqual(self.call('apply',False).returncode,0)
        self.assertFalse((self.run/'coalesce-active.json').exists())
        self.call('restore')
        self.assertEqual(self.state.read_text(),'17')

    def test_saved_original_is_not_overwritten(self):
        self.call('save'); self.state.write_text('49')
        self.assertNotEqual(self.call('save',False).returncode,0)
        self.assertEqual((self.run/'rx-coalesce-before.txt').read_text().strip(),'17')
