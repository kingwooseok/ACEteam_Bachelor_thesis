"""Observe readiness/failure/cleanup without launching a real machine stress load."""
import os
import csv
from pathlib import Path
import signal
import re
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix', 'Linux shell process tests')
class LoadControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ace-load-test-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.bin = self.folder / 'bin'
        self.bin.mkdir()
        self.ready = self.folder / 'ready'
        self.env = dict(os.environ, ACE_RUN_DIR=str(self.folder/'result'), ACE_RUN_ID='load-test', ACE_SKIP_MANIFEST='1', ACE_MONITOR_ENABLED='0', ACE_IPERF_ENABLED='0', ACE_READY_FILE=str(self.ready), PATH=str(self.bin)+os.pathsep+os.environ['PATH'])
        self.fake('python3', 'echo python-was-called >&2; exit 91')

    def fake(self, name, code):
        p = self.bin / name
        p.write_text('#!/bin/bash\n'+code+'\n')
        p.chmod(0o755)

    def start(self, args=None):
        p = subprocess.Popen(['bash',str(ROOT/'monitor/controller.sh'),*(args or ['0','1','10',''])], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def await_ready(self, p):
        deadline = time.monotonic()+4
        while time.monotonic()<deadline and p.poll() is None:
            if self.ready.exists(): return
            time.sleep(.01)
        self.fail('ready not published: '+str(p.communicate(timeout=2)))

    def test_lean_start_and_term_no_python(self):
        p=self.start(); self.await_ready(p)
        p.terminate(); out,err=p.communicate(timeout=5)
        self.assertEqual(p.returncode,0,(out,err))
        self.assertNotIn('python-was-called',err)
        self.assertTrue((self.folder/'result/config.txt').is_file())

    def test_bad_cpu_input_fails_before_output(self):
        p=self.start(['101','1','2','0'])
        p.communicate(timeout=3)
        self.assertNotEqual(p.returncode,0)
        self.assertFalse(self.ready.exists())

    def test_shared_workers_inherit_the_complete_general_cpu_set(self):
        available = sorted(os.sched_getaffinity(0))
        selected = available[:min(4, len(available))]
        cpus = ','.join(map(str, selected))
        evidence = self.folder/'worker-evidence'
        # This child stands in for stress-ng and inherits taskset's actual mask.
        self.fake('stress-ng', f'''printf '%s\\n' "$@" > "{evidence}.args"
grep Cpus_allowed_list /proc/$$/status > "{evidence}.cpus"
trap 'exit 0' TERM INT
while :; do sleep 1; done''')
        self.env.update(ACE_CPU_PLACEMENT='shared', ACE_CPU_WORKERS='3')
        p = self.start(['100', '1', '10', cpus])
        self.await_ready(p)
        args = Path(str(evidence)+'.args').read_text().splitlines()
        self.assertEqual(args[args.index('--cpu')+1], '3')
        self.assertEqual(args[args.index('--cpu-load')+1], '100')
        actual = Path(str(evidence)+'.cpus').read_text().split(':', 1)[1].strip()
        parsed = set()
        for part in actual.split(','):
            bounds = part.split('-')
            parsed.update(range(int(bounds[0]), int(bounds[-1])+1))
        self.assertEqual(parsed, set(selected))
        p.terminate(); out, err = p.communicate(timeout=6)
        self.assertEqual(p.returncode, 0, (out, err))
        result = self.folder/'result'
        self.assertTrue((result/'stress_cpu_shared.log').is_file())
        self.assertEqual(len(list(result.glob('stress_cpu*.log'))), 1)
        config = dict(line.split('=', 1) for line in (result/'config.txt').read_text().splitlines())
        self.assertEqual(config['cpu_workers'], '3')
        self.assertEqual(config['cpu_placement'], 'shared')
        self.assertEqual(config['load_cpus'], cpus)

    def test_invalid_shared_worker_count_fails_before_output(self):
        self.env.update(ACE_CPU_PLACEMENT='shared', ACE_CPU_WORKERS='0')
        p = self.start(['100', '1', '2', str(min(os.sched_getaffinity(0)))])
        out, err = p.communicate(timeout=3)
        self.assertNotEqual(p.returncode, 0, (out, err))
        self.assertFalse((self.folder/'result').exists())
        self.assertIn('shared CPU placement requires CPU workers', err)

    def test_failed_child_never_reports_ready(self):
        self.fake('iperf3','echo preparation-failed; exit 7')
        self.env['ACE_IPERF_ENABLED']='1'
        p=self.start(); p.communicate(timeout=5)
        self.assertNotEqual(p.returncode,0)
        self.assertFalse(self.ready.exists())
        self.assertIn('preparation-failed', (self.folder/'result/iperf3.log').read_text())

    def test_existing_ready_is_preserved(self):
        self.ready.write_text('existing')
        p=self.start(); p.communicate(timeout=5)
        self.assertNotEqual(p.returncode,0)
        self.assertEqual(self.ready.read_text(),'existing')

    def test_stop_kills_own_group_but_not_unrelated_process(self):
        pidfile=self.folder/'owned.pid'
        self.fake('iperf3',f'echo $$ > {pidfile}\ntrap "exit 0" TERM INT\nsleep 60 & wait $!')
        self.env['ACE_IPERF_ENABLED']='1'
        unrelated=subprocess.Popen(['sleep','60'])
        def finish_unrelated():
            if unrelated.poll() is None: unrelated.terminate()
            unrelated.wait(timeout=2)
        self.addCleanup(finish_unrelated)
        p=self.start(); self.await_ready(p)
        owned=int(pidfile.read_text())
        p.terminate(); p.communicate(timeout=6)
        self.assertEqual(p.returncode,0)
        self.assertIsNone(unrelated.poll())
        with self.assertRaises(ProcessLookupError):os.kill(owned,0)
        unrelated.terminate(); unrelated.wait(timeout=2)

    def test_network_uses_total_rate_and_preserves_packet_length(self):
        argsfile=self.folder/'args'
        self.fake('iperf3',f'printf "%s\\n" "$@" > {argsfile}')
        p=subprocess.run(['bash',str(ROOT/'monitor/network_load.sh'),'127.0.0.1','10000','5','udp','bidir','5201','1'],env=self.env,timeout=4)
        self.assertEqual(p.returncode,0)
        args=argsfile.read_text().splitlines()
        self.assertEqual(args[args.index('-b')+1],'2000')
        self.assertIn('--bidir',args)
        self.assertNotIn('-l',args)

    def live_tool(self, name):
        self.fake(name, 'echo tool-start\ntrap "echo tool-final; exit 0" TERM INT\nwhile :; do sleep 1; done')

    def preserved_stage(self, error, prefix):
        match=re.search(r'staged logs preserved at (/dev/shm/'+re.escape(prefix)+r'[^\s]*)', error)
        self.assertIsNotNone(match,error)
        stage=Path(match.group(1))
        self.assertEqual(stage.parent,Path('/dev/shm'))
        self.addCleanup(shutil.rmtree,stage)
        return stage

    def test_observation_logs_stay_in_tmpfs_until_term(self):
        for tool in ('mpstat','sar','iperf3'): self.live_tool(tool)
        self.env.update(ACE_MONITOR_ENABLED='1',ACE_IPERF_ENABLED='1')
        p=self.start(); self.await_ready(p)
        result=self.folder/'result'
        self.assertEqual(list(result.glob('*.log')),[])
        p.terminate(); out,err=p.communicate(timeout=6)
        self.assertEqual(p.returncode,0,(out,err))
        for name in ('mpstat.log','network.log','iperf3.log'):
            text=(result/name).read_text()
            self.assertIn('tool-start',text)
            self.assertIn('tool-final',text)

    def test_native_cpu_and_network_sidecar_flush_after_final_sample(self):
        self.live_tool('sar')
        self.env.update(ACE_MONITOR_ENABLED='1', ACE_INTERFACE='lo',
                        ACE_CPU_SAMPLER=str(ROOT/'udp_socket/bin/cpu_sample'))
        process = self.start()
        self.await_ready(process)
        result = self.folder/'result'
        self.assertFalse((result/'cpu-samples.csv').exists())
        self.assertFalse((result/'network-samples.csv').exists())
        process.terminate()
        out, err = process.communicate(timeout=6)
        self.assertEqual(process.returncode, 0, (out, err))
        with (result/'network-samples.csv').open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual({row['interface'] for row in rows}, {'lo'})
        self.assertGreater(int(rows[-1]['monotonic_ns']), int(rows[0]['monotonic_ns']))
        self.assertGreaterEqual(int(rows[-1]['rx_bytes']), int(rows[0]['rx_bytes']))
        self.assertTrue((result/'cpu-samples.csv').is_file())

    def test_log_collision_preserves_original_and_staged_capture(self):
        self.live_tool('iperf3'); self.env['ACE_IPERF_ENABLED']='1'
        p=self.start(); self.await_ready(p)
        destination=self.folder/'result/iperf3.log'
        destination.write_text('existing result\n')
        p.terminate(); out,err=p.communicate(timeout=6)
        self.assertNotEqual(p.returncode,0,(out,err))
        self.assertEqual(destination.read_text(),'existing result\n')
        stage=self.preserved_stage(err,'ace-monitor.')
        self.assertIn('tool-start',(stage/'iperf3.log').read_text())
        self.assertIn('tool-final',(stage/'iperf3.log').read_text())

    def test_native_timer_completes_and_early_tool_exit_fails(self):
        self.live_tool('iperf3'); self.env['ACE_IPERF_ENABLED']='1'
        p=self.start(['0','1','1','']); self.await_ready(p)
        out,err=p.communicate(timeout=6)
        self.assertEqual(p.returncode,0,(out,err))
        self.assertIn('DONE',out)
        self.assertIn('tool-final',(self.folder/'result/iperf3.log').read_text())

    def test_ready_tool_exit_is_detected_by_blocking_wait(self):
        self.fake('iperf3','echo tool-start; sleep 0.3; echo tool-failed; exit 7')
        self.env['ACE_IPERF_ENABLED']='1'
        p=self.start(); self.await_ready(p)
        out,err=p.communicate(timeout=6)
        self.assertNotEqual(p.returncode,0,(out,err))
        self.assertIn('exited before measurement finished',err)
        self.assertIn('tool-failed',(self.folder/'result/iperf3.log').read_text())

    def test_network_stdout_is_buffered_until_term(self):
        marker=self.folder/'network-started'
        self.fake('iperf3',f'touch "{marker}"\necho network-start\ntrap "echo network-final; exit 0" TERM INT\nwhile :; do sleep 1; done')
        output=self.folder/'network.log'
        with output.open('wb') as file:
            p=subprocess.Popen(['bash',str(ROOT/'monitor/network_load.sh'),'127.0.0.1','10000','1','udp','forward','5201','10'],env=self.env,stdout=file,stderr=subprocess.PIPE,text=True)
            self.addCleanup(lambda:p.poll() is None and p.kill())
            deadline=time.monotonic()+4
            while time.monotonic()<deadline and p.poll() is None and not marker.exists():time.sleep(.01)
            self.assertTrue(marker.exists())
            self.assertEqual(output.stat().st_size,0)
            p.terminate(); _,err=p.communicate(timeout=6)
            self.assertEqual(p.returncode,0,err)
        self.assertIn('network-start',output.read_text())
        self.assertIn('network-final',output.read_text())

    def test_network_flush_failure_preserves_staged_log(self):
        self.fake('iperf3','echo network-preserved')
        with open('/dev/full','wb') as output:
            p=subprocess.run(['bash',str(ROOT/'monitor/network_load.sh'),'127.0.0.1','10000','1','udp','forward','5201','1'],env=self.env,stdout=output,stderr=subprocess.PIPE,text=True,timeout=5)
        self.assertNotEqual(p.returncode,0)
        stage=self.preserved_stage(p.stderr,'ace-network.')
        self.assertIn('network-preserved',(stage/'network.log').read_text())


if __name__ == '__main__': unittest.main()
