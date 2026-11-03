"""End-to-end native runner smoke and failure checks; Linux loopback only."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "experiment" / "ace-run"


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux native runner")
class NativeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ace-native-")
        self.directory = Path(self.temporary.name)
        self.processes = []
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.base = (ROOT / "experiment" / "socket-smoke.conf").read_text()
        self.base = self.base.replace("control_port=49090", f"control_port={self.port}")
        self.base = self.base.replace("output_root=results/experiment", f"output_root={self.directory}/result")
        self.config = self.directory / "run.conf"
        self.config.write_text(self.base)

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
        self.temporary.cleanup()

    def launch(self, role, config=None):
        process = subprocess.Popen([str(RUNNER), role, "--config", str(config or self.config)],
                                   cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        self.processes.append(process)
        return process

    def complete(self, process, success=True):
        output = process.communicate(timeout=20)[0]
        if success:
            self.assertEqual(process.returncode, 0, output)
        else:
            self.assertNotEqual(process.returncode, 0, output)
        return output

    def test_three_flow_two_repeat_socket_suite(self):
        receiver = self.launch("receiver")
        sender = self.launch("sender")
        self.complete(sender)
        self.complete(receiver)
        root = self.directory / "result" / "socket-smoke"
        for role in ("receiver", "sender"):
            runs = sorted((root / role / "runs").iterdir())
            self.assertEqual(len(runs), 2)
            for run in runs:
                data = json.loads((run / "run.json").read_text())
                self.assertEqual(data["state"], "completed")
                self.assertEqual(data["mode"], "socket-only")
                self.assertEqual(len(data["flows"]), 3)
                self.assertTrue((run / ".complete").exists())
                self.assertFalse((run / ".failed").exists())
                # socket-only도 상태 파일을 남기지만 governor/IRQ를 적용하지 않는다.
                for phase in ('before', 'after'):
                    self.assertIn('capture_complete=1', (run / f'system-{phase}.txt').read_text())
                self.assertFalse((run / 'environment-prepare.log').exists())
                self.assertEqual(data['environment']['governor'], 'performance')
                self.assertEqual(data['environment']['rx_irq_cpu'], 2)
                if role == 'receiver':
                    scheduler = json.loads((run / 'scheduler-ready.json').read_text())
                    self.assertEqual(scheduler['phase'], 'ready')
                    self.assertTrue(scheduler['complete'])
                manifest = json.loads((run / "manifest.json").read_text())
                self.assertEqual(manifest["run_id"], data["run_id"])
                for flow in data["flows"]:
                    stats = json.loads((run / f'{role}-flow{flow["flow_id"]}.json').read_text())
                    self.assertEqual(stats["sent" if role == "sender" else "received"], flow["count"])
        # A second launch must refuse the same run directories and preserve data.
        files = list(root.rglob("run.json"))
        before = {file: file.read_bytes() for file in files}
        receiver = self.launch("receiver")
        sender = self.launch("sender")
        self.complete(sender, False)
        self.complete(receiver, False)
        self.assertEqual(before, {file: file.read_bytes() for file in files})

    def test_role_specific_supervisor_affinity(self):
        # 실행 호스트가 허용한 CPU에서 두 역할의 독립 affinity를 실제 확인한다.
        cpus = sorted(os.sched_getaffinity(0))
        self.config.write_text(self.base.replace("duration_ms=100", "duration_ms=1000") +
                               f"receiver.supervisor_cpu={cpus[0]}\n" +
                               f"sender.supervisor_cpu={cpus[-1]}\n" +
                               "alternate_mode_order=1\n")
        receiver = self.launch("receiver")
        sender = self.launch("sender")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if list((self.directory / "result").rglob("start.ns")):
                break
            if receiver.poll() is not None or sender.poll() is not None:
                self.fail("runner exited before affinity check")
            time.sleep(0.01)
        else:
            self.fail("no start barrier")
        self.assertEqual(os.sched_getaffinity(receiver.pid), {cpus[0]})
        self.assertEqual(os.sched_getaffinity(sender.pid), {cpus[-1]})
        self.complete(sender)
        self.complete(receiver)

    def test_generated_design_configs_are_accepted_by_native_parser(self):
        from tests.test_design_plan import DesignPlanTests, plan
        profile = DesignPlanTests().profile()
        for index, row in enumerate(plan.rows()):
            self.config.write_text(plan.config(profile, row, index))
            output = self.complete(self.launch("plan"))
            conditions = [json.loads(line) for line in output.splitlines()[1:]]
            self.assertTrue(conditions)
            self.assertEqual(conditions[0]['environment']['sender_network_cpu'], 3)
            self.assertEqual(conditions[0]['environment']['receiver_supervisor_cpu'], -1)
            self.assertEqual(conditions[0]['environment']['receiver_available_cpus'], '0,1,2,3')
            for condition in conditions:
                active = condition['mode']=='mixed' and any(f['flow_id']==2 for f in condition['flows'])
                self.assertEqual(condition['environment']['receiver_cpumap_active'], active)
                self.assertEqual(condition['environment']['receiver_shared_cpus'],
                                 '0,1,2' if active else '0,1,2,3')

    def test_mismatched_configuration_is_rejected(self):
        other = self.directory / "other.conf"
        other.write_text(self.base.replace("duration_ms=100", "duration_ms=101"))
        receiver = self.launch("receiver")
        sender = self.launch("sender", other)
        self.complete(sender, False)
        self.complete(receiver, False)
        self.assertEqual(list((self.directory / "result").rglob(".complete")), [])

    def test_mode_paths_and_effective_receiver_placement(self):
        text = self.base.replace('modes=socket-only', 'modes=all-pass,pass-xsk,mixed,socket-only')
        text = text.replace('flow2.rx_cpu=inherit', 'flow2.rx_cpu=3')
        text += 'flow2.all_pass_rx_cpu=1\n'
        self.config.write_text(text)
        conditions = {item['mode']: item for item in map(
            json.loads, self.complete(self.launch('plan')).splitlines()[1:])}
        expected = {
            'all-pass': (['pass', 'pass', 'pass'], 1),
            'pass-xsk': (['pass', 'pass', 'xsk'], 3),
            'mixed': (['pass', 'cpumap', 'xsk'], 3),
            'socket-only': (['pass', 'pass', 'pass'], 3),
        }
        for mode, (paths, cpu) in expected.items():
            flows = conditions[mode]['flows']
            self.assertEqual([flow['requested_path'] for flow in flows], paths)
            self.assertEqual(flows[1]['rx_cpu'], cpu)
            self.assertEqual([flow['rx_cpu'] for flow in (flows[0], flows[2])], [-1, -1])
        # Existing configurations retain their common affinity in all-pass.
        self.config.write_text(text.replace('flow2.all_pass_rx_cpu=1\n', ''))
        conditions = [json.loads(line) for line in self.complete(self.launch('plan')).splitlines()[1:]]
        self.assertEqual(conditions[0]['flows'][1]['rx_cpu'], 3)

    def test_receiver_override_is_applied_to_the_running_socket_child(self):
        cpus = sorted(os.sched_getaffinity(0))
        self.config.write_text(
            self.base.replace('repo=AUTO', f'repo={ROOT}').replace(
                'flow2.rx_cpu=inherit', f'flow2.rx_cpu={cpus[0]}') +
            f'flow2.all_pass_rx_cpu={cpus[-1]}\nverify_placement=1\n')
        source = self.directory / 'receiver-placement.c'
        source.write_text(r'''
#define main native_runner_main
#include "experiment/ace-run.c"
#undef main
#include <assert.h>
int main(int argc, char **argv) {
    assert(argc==4);
    struct context x={.fd=-1,.receiver=1,.index=1};
    assert(read_run_config(argv[1],&x.c)==0);
    assert(chdir(x.c.repo)==0);
    strcpy(x.mode,argv[2]); strcpy(x.run_dir,argv[3]);
    int child=receiver_process(&x,1);
    cleanup_children(&x);
    assert(child>=0);
    return 0;
}
''')
        binary = self.directory / 'receiver-placement'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT),
                        '-I', str(ROOT / 'include'), str(source), str(ROOT / 'experiment/run_config.c'),
                        str(ROOT / 'experiment/ace_manifest.c'), '-o', str(binary)], check=True)
        # Call the real receiver-start path without loader, IRQ or governor setup.
        for mode, expected_cpu in (('all-pass', cpus[-1]), ('pass-xsk', cpus[0])):
            run = self.directory / mode
            run.mkdir()
            subprocess.run([str(binary), str(self.config), mode, str(run)],
                           check=True, timeout=10, capture_output=True, text=True)
            placement = json.loads((run / 'receiver-flow2.placement.json').read_text())
            self.assertTrue(placement['verified'])
            self.assertEqual(placement['cpus'], [expected_cpu])

    def test_shared_mask_paths_and_plan_validation(self):
        base = self.base.replace('modes=socket-only', 'modes=all-pass,pass-xsk,mixed')
        shared = base + 'receiver.shared_cpus=0-3\nreceiver.cpumap_cpu=3\n'
        self.config.write_text(shared)
        conditions = [json.loads(line) for line in self.complete(self.launch('plan')).splitlines()[1:]]
        for condition in conditions:
            general = '0,1,2' if condition['mode']=='mixed' else '0,1,2,3'
            self.assertEqual(condition['environment']['receiver_available_cpus'], '0,1,2,3')
            self.assertEqual(condition['environment']['receiver_shared_cpus'], general)
            self.assertEqual(condition['environment']['receiver_cpumap_active'], condition['mode']=='mixed')
            self.assertEqual(condition['environment']['receiver_cpumap_cpu'], 3)
            expected = [general, '3' if condition['mode'] == 'mixed' else general, general]
            self.assertEqual([flow['rx_cpus'] for flow in condition['flows']], expected)
            self.assertEqual([flow['rx_cpu'] for flow in condition['flows']],
                             [-1, 3 if condition['mode'] == 'mixed' else -1, -1])
        for value in ('', '0,,1', '2-0', '0,0', '0-2,2', '0-2', '3', '0,1,1024', 'inherit', '0,1,'):
            self.config.write_text(shared.replace('receiver.shared_cpus=0-3', 'receiver.shared_cpus='+value))
            self.complete(self.launch('plan'), False)
        for text in (
            shared.replace('receiver.cpumap_cpu=3', 'receiver.cpumap_cpu=2'),
            shared.replace('flow1.rx_cpu=inherit', 'flow1.rx_cpu=0'),
            shared + 'flow2.all_pass_rx_cpu=1\n',
            shared + 'receiver.supervisor_cpu=0\n',
            shared + 'receiver.network_cpu=1\n',
            shared.replace('load.cpu_percent=0', 'load.cpu_percent=100').replace(
                'load.cpu_cpus=', 'load.cpu_cpus=0,2,3'),
            shared + 'environment.rx_irq_cpu=3\n',
            base + 'receiver.cpumap_cpu=3\n',
        ):
            self.config.write_text(text)
            self.complete(self.launch('plan'), False)

        # A mixed classifier without its CPUMAP flow leaves every CPU shared.
        self.config.write_text(shared.replace('flow2.enabled=1', 'flow2.enabled=0'))
        conditions = [json.loads(line) for line in self.complete(self.launch('plan')).splitlines()[1:]]
        for condition in conditions:
            self.assertFalse(condition['environment']['receiver_cpumap_active'])
            self.assertEqual(condition['environment']['receiver_shared_cpus'], '0,1,2,3')
            self.assertEqual([flow['rx_cpus'] for flow in condition['flows']], ['0,1,2,3']*2)

        text = shared.replace('load.cpu_cpus=', 'load.cpu_cpus=0,1,2,3')
        text += 'load.cpu_layout=shared\nload.cpu_workers=3\n'
        self.config.write_text(text)
        conditions = [json.loads(line) for line in self.complete(self.launch('plan')).splitlines()[1:]]
        for condition in conditions:
            self.assertEqual(condition['load']['cpu_workers'], 3)
            self.assertEqual(condition['load']['cpu_layout'], 'shared')
            self.assertEqual(condition['load']['cpu_cpus'], condition['environment']['receiver_shared_cpus'])
        for invalid in (text.replace('load.cpu_workers=3', 'load.cpu_workers=0'),
                        text.replace('load.cpu_workers=3\n', ''),
                        text.replace('load.cpu_cpus=0,1,2,3', 'load.cpu_cpus=0,1,2')):
            self.config.write_text(invalid)
            self.complete(self.launch('plan'), False)

    def test_shared_receiver_mask_is_applied_to_live_socket_children(self):
        available = sorted(os.sched_getaffinity(0))
        shared = sorted([cpu for cpu in available if cpu != 3][:3] + [3])
        if 3 not in available or len(shared) < 2:
            self.skipTest('CPU3 and a general CPU are required for shared-affinity checks')
        cpulist = ','.join(map(str, shared))
        self.config.write_text(self.base.replace('duration_ms=100', 'duration_ms=1000') +
                               f'receiver.shared_cpus={cpulist}\nreceiver.cpumap_cpu=3\n'
                               f'environment.rx_irq_cpu={next(cpu for cpu in shared if cpu!=3)}\nverify_placement=1\n')
        receiver = self.launch('receiver')
        sender = self.launch('sender')
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            if list((self.directory/'result').rglob('start.ns')):
                self.assertEqual(os.sched_getaffinity(receiver.pid), set(shared))
                break
            if receiver.poll() is not None or sender.poll() is not None:
                self.fail('shared receiver exited before capture')
            time.sleep(.01)
        else:
            self.fail('no shared receiver start barrier')
        self.complete(sender)
        self.complete(receiver)
        runs = list((self.directory/'result/socket-smoke/receiver/runs').iterdir())
        self.assertEqual(len(runs), 2)
        for run in runs:
            descriptor = json.loads((run/'run.json').read_text())
            self.assertEqual([f['rx_cpus'] for f in descriptor['flows']], [cpulist]*3)
            for flow in (1, 2, 3):
                placement = json.loads((run/f'receiver-flow{flow}.placement.json').read_text())
                self.assertTrue(placement['verified'])
                self.assertEqual(placement['cpus'], shared)

    def test_cpumap_only_moves_the_important_receiver_out_of_shared_mask(self):
        available = sorted(os.sched_getaffinity(0))
        shared = sorted([cpu for cpu in available if cpu != 3][:3]+[3])
        if 3 not in available or len(shared) < 2:
            self.skipTest('CPU3 and a general CPU are required')
        self.config.write_text(self.base.replace('repo=AUTO', f'repo={ROOT}') +
                               f'receiver.shared_cpus={",".join(map(str, shared))}\n'
                               f'receiver.cpumap_cpu=3\nenvironment.rx_irq_cpu={next(cpu for cpu in shared if cpu!=3)}\n'
                               'verify_placement=1\n')
        source = self.directory/'shared-receiver.c'
        source.write_text(r'''
#define main native_runner_main
#include "experiment/ace-run.c"
#undef main
#include <assert.h>
int main(int argc, char **argv) {
    assert(argc==4);
    struct context x={.fd=-1,.receiver=1,.index=1};
    assert(read_run_config(argv[1],&x.c)==0);
    assert(chdir(x.c.repo)==0);
    strcpy(x.mode,argv[2]); strcpy(x.run_dir,argv[3]);
    assert(scheduler_mask(&x.c.receiver_shared_mask,SCHED_OTHER,0)==0);
    int child=receiver_process(&x,1);
    cleanup_children(&x);
    assert(child>=0);
    return 0;
}
''')
        binary = self.directory/'shared-receiver'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT),
                        '-I', str(ROOT/'include'), str(source), str(ROOT/'experiment/run_config.c'),
                        str(ROOT/'experiment/ace_manifest.c'), '-o', str(binary)], check=True)
        modes = [('all-pass', shared), ('pass-xsk', shared)]
        modes.append(('mixed', [3]))
        for mode, expected in modes:
            run = self.directory/mode
            run.mkdir()
            subprocess.run([str(binary), str(self.config), mode, str(run)],
                           check=True, timeout=10, capture_output=True, text=True)
            placement = json.loads((run/'receiver-flow2.placement.json').read_text())
            self.assertTrue(placement['verified'])
            self.assertEqual(placement['cpus'], expected)

    def test_unavailable_receiver_cpu_mask_fails_before_handshake_or_setup(self):
        # Linux silently removes offline CPUs from a mask containing an online
        # CPU; the runner must catch that reduction rather than silently pin.
        available = [cpu for cpu in sorted(os.sched_getaffinity(0)) if cpu != 3]
        if not available or 1023 in available:
            self.skipTest('requires an available general CPU and unavailable CPU1023')
        self.config.write_text(self.base +
                               f'receiver.shared_cpus={available[0]},3,1023\n'
                               f'receiver.cpumap_cpu=3\nenvironment.rx_irq_cpu={available[0]}\n')
        output = self.complete(self.launch('receiver'), False)
        self.assertIn('runner CPU placement', output)
        self.assertFalse((self.directory/'result').exists())

    def test_cpu_partition_check_precedes_receiver_environment_mutations(self):
        source = self.directory/'partition-order.c'
        source.write_text(r'''
#define main native_runner_main
#include "experiment/ace-run.c"
#undef main
#include <assert.h>
int main(int argc, char **argv) {
    assert(argc==4);
    struct context x={.fd=-1,.receiver=1};
    x.c.ready_ms=1000; x.c.stop_ms=1000; x.c.receiver_cpumap_cpu=3;
    strcpy(x.c.receiver_shared_cpus,"0,1,2,3");
    strcpy(x.c.interface,"eth0"); strcpy(x.c.governor,"performance");
    x.c.rx_irq_cpu=2;
    strcpy(x.mode,argv[1]); strcpy(x.run_dir,argv[2]);
    int result=prepare_environment(&x);
    cleanup_children(&x);
    return (result==0)==atoi(argv[3]) ? 0 : 1;
}
''')
        binary = self.directory/'partition-order'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT),
                        '-I', str(ROOT/'include'), str(source), str(ROOT/'experiment/run_config.c'),
                        str(ROOT/'experiment/ace_manifest.c'), '-o', str(binary)], check=True)
        fixture = self.directory/'partition-fixture'
        monitor = fixture/'monitor'
        monitor.mkdir(parents=True)
        check = monitor/'check_cpu_partition.sh'
        check.write_text('#!/bin/bash\nprintf "%s\\n" "$*" > checked\nexit 1\n')
        (monitor/'prepare_env.sh').write_text('#!/bin/bash\nprintf changed > mutated\n')
        rejected = self.directory/'rejected-partition'
        rejected.mkdir()
        subprocess.run([str(binary), 'mixed', str(rejected), '0'], cwd=fixture,
                       check=True, timeout=5, capture_output=True)
        self.assertEqual((fixture/'checked').read_text().strip(), 'eth0 0,1,2,3 3')
        self.assertFalse((fixture/'mutated').exists())
        # Operational socket smoke skips machine-level checks and mutation.
        smoke = self.directory/'smoke-partition'
        smoke.mkdir()
        subprocess.run([str(binary), 'socket-only', str(smoke), '1'], cwd=fixture,
                       check=True, timeout=5, capture_output=True)
        self.assertEqual(list(smoke.iterdir()), [])
        check.write_text('#!/bin/bash\nexit 0\n')
        accepted = self.directory/'accepted-partition'
        accepted.mkdir()
        subprocess.run([str(binary), 'mixed', str(accepted), '1'], cwd=fixture,
                       check=True, timeout=5, capture_output=True)
        self.assertTrue((fixture/'mutated').exists())

    def test_dynamic_partition_child_join_and_signal_cleanup(self):
        available = sorted(os.sched_getaffinity(0))
        if 3 not in available or len(available)<2:
            self.skipTest('CPU3 and a general CPU are required')
        shared = sorted([cpu for cpu in available if cpu!=3][:3]+[3])
        irq = next(cpu for cpu in shared if cpu!=3)
        cpulist = ','.join(map(str,shared))
        fixture = self.directory/'dynamic-fixture'
        (fixture/'monitor').mkdir(parents=True)
        (fixture/'udp_socket').symlink_to(ROOT/'udp_socket', target_is_directory=True)
        # Source-path fixtures keep machine settings untouched while exercising
        # the real C lifecycle, real receiver child, and interrupt cleanup.
        (fixture/'monitor/check_cpu_partition.sh').write_text('#!/bin/bash\nexit 0\n')
        (fixture/'monitor/prepare_env.sh').write_text('#!/bin/bash\nexit 0\n')
        helper = fixture/'monitor/cpu_partition.sh'
        helper.write_text('''#!/bin/bash
set -e
if [[ $1 == acquire ]]; then
  printf acquired > "$5/acquired"
  [[ ! -e fail-acquire ]]
else
  if [[ -s "$3/cgroup.procs" ]]; then
    pid=$(cat "$3/cgroup.procs")
    if kill -0 "$pid" 2>/dev/null; then exit 25; fi
  fi
  printf released > "$3/released"
  [[ ! -e fail-release ]]
fi
''')
        self.config.write_text(self.base.replace('repo=AUTO', f'repo={fixture}')+
                               f'receiver.shared_cpus={cpulist}\nreceiver.cpumap_cpu=3\n'
                               f'environment.rx_irq_cpu={irq}\nverify_placement=1\n')
        source = self.directory/'dynamic-cleanup.c'
        source.write_text(r'''
#define main native_runner_main
#include "experiment/ace-run.c"
#undef main
#include <assert.h>
int main(int argc,char **argv) {
    assert(argc==5);
    struct context x={.fd=-1,.receiver=1,.index=1,.partition_lock=-1};
    assert(read_run_config(argv[1],&x.c)==0);
    assert(chdir(x.c.repo)==0);
    strcpy(x.mode,"mixed"); strcpy(x.run_dir,argv[2]);
    assert(scheduler_mask(&x.c.receiver_shared_mask,SCHED_OTHER,0)==0);
    int prepared=prepare_environment(&x);
    assert((prepared==0)==atoi(argv[3]));
    assert(x.partition_attempted);
    if (prepared==0) {
        cpu_set_t general,actual;
        run_general_cpu_set(&x.c,x.mode,&general);
        assert(sched_getaffinity(0,sizeof(actual),&actual)==0 && CPU_EQUAL(&general,&actual));
        /* Only this private fixture substitutes a normal cgroup.procs file. */
        strcpy(x.partition_group,x.run_dir);
        int child=receiver_process(&x,1); assert(child>=0);
        char path[4096]; pathf(path,sizeof(path),"%s/cgroup.procs",x.run_dir);
        FILE *in=fopen(path,"r"); long moved=0;
        assert(in && fscanf(in,"%ld",&moved)==1); fclose(in);
        assert(moved==x.child[child].pid);
    }
    stopping=1;
    cleanup_children(&x);
    int restored=restore_cpu_partition(&x);
    assert((restored==0)==atoi(argv[4]));
    if (restored==0) {
        cpu_set_t actual; assert(!x.partition_attempted && !x.partition_active);
        assert(sched_getaffinity(0,sizeof(actual),&actual)==0);
        assert(CPU_EQUAL(&actual,&x.c.receiver_shared_mask));
        assert(restore_cpu_partition(&x)==0);
    } else assert(x.partition_attempted);
    return 0;
}
''')
        binary = self.directory/'dynamic-cleanup'
        subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-I',str(ROOT),
                        '-I',str(ROOT/'include'),str(source),str(ROOT/'experiment/run_config.c'),
                        str(ROOT/'experiment/ace_manifest.c'),'-o',str(binary)],check=True)
        for scenario, prepared, restored in (('success',1,1),('fail-acquire',0,1),('fail-release',1,0)):
            with self.subTest(scenario=scenario):
                run = self.directory/scenario
                run.mkdir()
                (run/'cgroup.procs').touch()
                if scenario!='success':
                    (fixture/scenario).touch()
                result = subprocess.run([str(binary),str(self.config),str(run),str(prepared),str(restored)],
                                        cwd=fixture,capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertTrue((run/'acquired').exists())
                self.assertTrue((run/'released').exists())
                if prepared:
                    placement=json.loads((run/'receiver-flow2.placement.json').read_text())
                    self.assertEqual(placement['cpus'],[3])
                    self.assertTrue(placement['verified'])
                if scenario!='success':
                    (fixture/scenario).unlink()

    def test_pass_xsk_preflight_requires_direct_receiver_only_when_enabled(self):
        source = self.directory / 'preflight.c'
        source.write_text(r'''
#define main native_runner_main
#include "experiment/ace-run.c"
#undef main
int main(int argc, char **argv) {
    if (argc!=2) return 2;
    struct context x={.fd=-1,.receiver=1};
    x.c.mode_count=1; strcpy(x.c.modes[0],"pass-xsk");
    x.c.flows[2].enabled=atoi(argv[1]);
    return preflight(&x) ? 1 : 0;
}
''')
        binary = self.directory / 'preflight'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT),
                        '-I', str(ROOT / 'include'), str(source), str(ROOT / 'experiment/run_config.c'),
                        str(ROOT / 'experiment/ace_manifest.c'), '-o', str(binary)], check=True)
        fixture = self.directory / 'preflight-repo'
        for relative in ('udp_socket/bin/sender', 'udp_socket/bin/receiver',
                         'udp_socket/bin/ace_clock', 'udp_socket/bin/phc_calibrate',
                         'BPF/xdp_loader', 'monitor/capture_state.sh',
                         'monitor/capture_scheduler.sh', 'monitor/prepare_env.sh',
                         'monitor/set_irq_affinity.sh'):
            path = fixture / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# fixture: preflight only checks access\n')
            path.chmod(0o755)
        result = subprocess.run([str(binary), '1'], cwd=fixture, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('missing executable: BPF/afxdp_recv', result.stderr)
        subprocess.run([str(binary), '0'], cwd=fixture, check=True, capture_output=True)
        direct_receiver = fixture / 'BPF/afxdp_recv'
        direct_receiver.write_text('# fixture\n')
        direct_receiver.chmod(0o755)
        subprocess.run([str(binary), '1'], cwd=fixture, check=True, capture_output=True)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ("iperf3", "sar")),
                         "real iperf3/sar are required")
    def test_real_network_load_and_observation_two_repeat_suite(self):
        # The real client reports failure if RX stops its server before TX ACK.
        # Reserve a dynamically selected port in both transports while choosing it.
        while True:
            with socket.socket() as tcp, socket.socket(type=socket.SOCK_DGRAM) as udp:
                tcp.bind(("127.0.0.1", 0))
                port = tcp.getsockname()[1]
                if port == self.port:
                    continue
                try:
                    udp.bind(("127.0.0.1", port))
                except OSError:
                    continue
                break
        config = self.base.replace("duration_ms=100", "duration_ms=1500")
        config = config.replace("warmup_ms=0", "warmup_ms=100")
        config = config.replace("monitor=0", "monitor=1")
        config = config.replace("load.network_bps=0", "load.network_bps=1000000")
        cpus = sorted(os.sched_getaffinity(0))
        self.config.write_text(config + f"load.network_port={port}\n" +
                               f"receiver.network_cpu={cpus[0]}\n" +
                               f"sender.network_cpu={cpus[-1]}\n")
        receiver = self.launch("receiver")
        sender = self.launch("sender")
        def descendants(pid):
            try:
                children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            except (FileNotFoundError, ProcessLookupError):
                return []
            result = []
            for child in children:
                child = int(child)
                try:
                    command = Path(f"/proc/{child}/cmdline").read_bytes().replace(b"\0", b" ")
                except (FileNotFoundError, ProcessLookupError):
                    # A short-lived child can exit after opening its proc file.
                    continue
                result.append((child, command))
                result.extend(descendants(child))
            return result
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            server = [pid for pid, command in descendants(receiver.pid)
                      if b"iperf3 -s" in command]
            client = [pid for pid, command in descendants(sender.pid)
                      if b"iperf3 -c" in command]
            if server and client:
                self.assertEqual(os.sched_getaffinity(server[0]), {cpus[0]})
                self.assertEqual(os.sched_getaffinity(client[0]), {cpus[-1]})
                break
            time.sleep(0.01)
        else:
            self.fail("no real iperf server/client for affinity check")
        self.complete(sender)
        self.complete(receiver)
        root = self.directory / "result" / "socket-smoke"
        for role in ("receiver", "sender"):
            runs = sorted((root / role / "runs").iterdir())
            self.assertEqual(len(runs), 2)
            for run in runs:
                data = json.loads((run / "run.json").read_text())
                self.assertEqual(data["state"], "completed")
                self.assertTrue((run / ".complete").exists())
                self.assertFalse((run / ".failed").exists())
                for flow in data["flows"]:
                    stats = json.loads((run / f'{role}-flow{flow["flow_id"]}.json').read_text())
                    self.assertEqual(stats["sent" if role == "sender" else "received"], flow["count"])
                if role == "receiver":
                    for name in ("cpu-samples.csv", "network.log", "iperf3.log"):
                        self.assertGreater((run / "monitor" / name).stat().st_size, 0)
                    self.assertFalse((run / 'monitor' / 'mpstat.log').exists())
                    self.assertIn('monotonic_ns,cpu,user', (run / 'monitor' / 'cpu-samples.csv').read_text())

    def test_peer_loss_flushes_own_receivers(self):
        self.config.write_text(self.base.replace("duration_ms=100", "duration_ms=5000"))
        receiver = self.launch("receiver")
        sender = self.launch("sender")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            states = list((self.directory / "result").rglob("start.ns"))
            if states:
                break
            if receiver.poll() is not None or sender.poll() is not None:
                self.fail("runner exited before measurement")
            time.sleep(0.01)
        else:
            self.fail("no start barrier")
        sender.send_signal(signal.SIGINT)
        self.complete(sender, False)
        self.complete(receiver, False)
        rx = next((self.directory / "result" / "socket-smoke" / "receiver" / "runs").iterdir())
        self.assertTrue((rx / ".failed").exists())
        self.assertFalse((rx / ".complete").exists())
        for stats in rx.glob("receiver-flow*.json"):
            self.assertTrue(json.loads(stats.read_text())["records_saved"])

    def test_opt_in_live_flow_placement_evidence(self):
        cpus = sorted(os.sched_getaffinity(0))
        text = self.base + 'verify_placement=1\n'
        # Use an available Linux CPU, without changing any machine-level setting.
        for flow in (1, 2, 3):
            for role in ('tx', 'rx'):
                text = text.replace(f'flow{flow}.{role}_cpu=inherit', f'flow{flow}.{role}_cpu={cpus[0]}')
        self.config.write_text(text)
        receiver = self.launch('receiver')
        sender = self.launch('sender')
        self.complete(sender)
        self.complete(receiver)
        evidence = list((self.directory / 'result').rglob('*.placement.json'))
        self.assertEqual(len(evidence), 12)
        for file in evidence:
            actual = json.loads(file.read_text())
            self.assertTrue(actual['verified'])
            self.assertEqual(actual['cpus'], [cpus[0]])
            self.assertEqual(actual['policy'], os.SCHED_OTHER)
            self.assertEqual(actual['priority'], 0)

    def test_full_design_configs_use_native_policy_and_capacity_checks(self):
        from tests.test_design_plan import DesignPlanTests, plan
        profile = DesignPlanTests().profile()
        profile.update(sender_policies=['other', 'fifo', 'rr'],
                       sender_priorities=[0, 80, 70])
        for index, row in enumerate(plan.rows(full_design=True)):
            self.config.write_text(plan.config(profile, row, index, full_design=True))
            output = self.complete(self.launch('plan'))
            modes = [json.loads(line) for line in output.splitlines()[1:]]
            self.assertEqual({m['mode'] for m in modes}, set(row['modes']))
            for mode in modes:
                self.assertLessEqual(sum(f['count'] for f in mode['flows']), 1048576)
                for flow in mode['flows']:
                    self.assertEqual(flow['count'], 60000)
                    index = flow['flow_id'] - 1
                    self.assertEqual(flow['tx_cpu'], index)
                    self.assertEqual(flow['tx_policy'], [os.SCHED_OTHER, os.SCHED_FIFO, os.SCHED_RR][index])
                    self.assertEqual(flow['tx_priority'], [0, 80, 70][index])
                    expected_rx_cpu = 3 if index == 1 and mode['mode'] == 'mixed' else -1
                    self.assertEqual(flow['rx_cpu'], expected_rx_cpu)
                    self.assertEqual(flow['rx_cpus'], '3' if expected_rx_cpu==3 else mode['environment']['receiver_shared_cpus'])
        # The general native runner still checks its recording capacity for
        # arbitrary manual configs independently of the fixed-rate study plan.
        text = plan.config(profile, plan.rows()[-1], 0, full_design=True)
        self.config.write_text(text.replace('flow1.period_ns=1000000', 'flow1.period_ns=64616'))
        output = self.complete(self.launch('plan'))
        for mode in map(json.loads, output.splitlines()[1:]):
            total = sum(flow['count'] for flow in mode['flows'])
            self.assertGreater(total, 1048500)
            self.assertLessEqual(total, 1048576)
        self.config.write_text(text.replace('flow1.period_ns=1000000', 'flow1.period_ns=64612'))
        self.complete(self.launch('plan'), False)

    def test_native_partial_control_frame_timeout_and_placement_mismatch(self):
        # Exercise production C helpers with socketpair and the current process;
        # no NIC, real-time privilege, SSH or machine configuration is involved.
        source = r'''
#define main native_runner_main
#include "experiment/ace-run.c"
#undef main
#include <assert.h>
int main(void) {
    struct run_config order={.mode_count=3,.alternate_mode_order=1};
    unsigned expected[3][3]={{0,1,2},{1,2,0},{2,0,1}};
    for (unsigned repeat=1;repeat<=6;repeat++)
        for (unsigned i=0;i<3;i++)
            assert(run_mode_index(&order,repeat,i)==expected[(repeat-1)%3][i]);
    order.mode_count=2;
    assert(run_mode_index(&order,1,0)==0 && run_mode_index(&order,2,0)==1);
    assert(run_mode_index(&order,2,1)==0 && run_mode_index(&order,3,0)==0);
    order.alternate_mode_order=0;
    assert(run_mode_index(&order,2,0)==0 && run_mode_index(&order,2,1)==1);
    int sockets[2]; char frame[256];
    assert(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets)==0);
    struct context x={.fd=sockets[0]};
    assert(write(sockets[1], "partial", 7)==7);
    int64_t start=ace_mono_ns();
    assert(transfer(&x,frame,sizeof(frame),0,50,0)==-1);
    assert(errno==ETIMEDOUT);
    assert(ace_mono_ns()-start < 1000000000LL);
    close(sockets[0]); close(sockets[1]);
    x.c.verify_placement=1;
    strcpy(x.run_dir,".");
    struct child c={.pid=getpid(),.expected_policy=SCHED_OTHER,.expected_priority=99};
    strcpy(c.name,"sender-flow1");
    assert(sched_getaffinity(0,sizeof(c.expected_cpus),&c.expected_cpus)==0);
    assert(verify_child_placement(&x,&c)==-1);
    assert(errno==EINVAL);
    return 0;
}
'''
        path = self.directory / 'native-check.c'
        executable = self.directory / 'native-check'
        path.write_text(source)
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT),
                        '-I', str(ROOT / 'include'), str(path), str(ROOT / 'experiment/run_config.c'),
                        str(ROOT / 'experiment/ace_manifest.c'), '-o', str(executable)], check=True)
        subprocess.run([str(executable)], cwd=self.directory, check=True, timeout=5,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertFalse(json.loads((self.directory / 'sender-flow1.placement.json').read_text())['verified'])

    def test_matrix_wrapper_sequences_native_socket_runs(self):
        import hashlib
        plan_dir = self.directory / 'matrix'
        plan_dir.mkdir()
        cases = []
        # Explicit operational fixture: short socket-only cases, not performance runs.
        for name in ('smoke-first', 'smoke-second'):
            text = self.base.replace('run_id=socket-smoke', f'run_id={name}') + 'verify_placement=1\n'
            (plan_dir / (name+'.conf')).write_text(text)
            cases.append(dict(case_id=name, config=name+'.conf', expected_runs=2,
                              expected_modes=['socket-only'], repeats=2,
                              references=['operational-test'], sha256=hashlib.sha256(text.encode()).hexdigest()))
        (plan_dir/'execution.json').write_text(json.dumps(dict(
            design='D01-D11', design_revision='fixed-1000pps-75-v4', cases=cases)))
        wrapper = str(ROOT/'experiment/run_matrix.sh')
        for role in ('receiver', 'sender'):
            process = subprocess.Popen([wrapper, role, '--plan-dir', str(plan_dir)], cwd=ROOT,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.processes.append(process)
        for process in reversed(self.processes):
            self.complete(process)
        for name in ('smoke-first', 'smoke-second'):
            for role in ('receiver', 'sender'):
                runs = list((self.directory/'result'/name/role/'runs').iterdir())
                self.assertEqual(len(runs), 2)
                self.assertTrue(all((run/'.complete').exists() for run in runs))
                self.assertFalse(any((run/'environment-prepare.log').exists() for run in runs))

    def test_plan_rejects_unknown_duplicate_and_capacity(self):
        for text in (self.base + "unknown=1\n", self.base + "repeats=1\n",
                     self.base.replace("run_id=socket-smoke", "run_id=_bad"),
                     self.base.replace("flow1.period_ns=2000000", "flow1.period_ns=1"),
                     self.base + "environment.governor=bad/value\n",
                     self.base + "environment.rx_irq_cpu=-1\n",
                     self.base + "receiver.supervisor_cpu=-2\n",
                     self.base + "sender.network_cpu=bad\n",
                     self.base + "flow2.all_pass_rx_cpu=bad\n",
                     self.base.replace('modes=socket-only', 'modes=pass-xsk,pass-xsk'),
                     self.base + "alternate_mode_order=2\n"):
            self.config.write_text(text)
            self.complete(self.launch("plan"), False)


if __name__ == "__main__":
    unittest.main()
