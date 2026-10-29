#!/usr/bin/env bash
# Bash entry point; Python standard library implements process supervision.
set -euo pipefail
exec python3 - "$0" "$@" <<'PYTHON'
from __future__ import annotations
import argparse
import configparser
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import weakref

DURATION = 60
REPEATS = 5


def dump(path, obj):
    with Path(path).open('x') as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write('\n')


def command(argv, timeout=60):
    return subprocess.run([str(x) for x in argv], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout).stdout


def placement(cpu, policy='other', priority=0):
    return ['taskset', '-c', str(cpu), 'chrt',
            {'other': '-o', 'fifo': '-f', 'rr': '-r'}[policy], str(priority)]


class Job:
    def __init__(self, argv, log):
        self.log = Path(log)
        self.file = self.log.open('x')
        self.argv = [str(x) for x in argv]
        self.p = subprocess.Popen(self.argv, stdout=self.file, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, start_new_session=True)

    def ready(self, needle, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.p.poll() is not None:
                raise RuntimeError(f'early exit: {self.argv}: {self.log.read_text()}')
            if needle in self.log.read_text():
                return
            time.sleep(.1)
        raise RuntimeError(f'readiness timeout: {self.argv}')

    def alive(self):
        if self.p.poll() is not None:
            raise RuntimeError(f'process exited: {self.argv}: {self.log.read_text()}')

    def stop(self, seconds=30, natural=False):
        if self.p.poll() is None and not natural:
            os.killpg(self.p.pid, signal.SIGTERM)
        try:
            rc = self.p.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            os.killpg(self.p.pid, signal.SIGKILL)
            self.p.wait()
            raise RuntimeError(f'forced kill; incomplete output: {self.argv}')
        finally:
            self.file.close()
        return rc

    def evidence(self, expected=None):
        self.alive()
        pid = self.p.pid
        actual = {'cpus': sorted(os.sched_getaffinity(pid)),
                  'policy': os.sched_getscheduler(pid),
                  'priority': os.sched_getparam(pid).sched_priority}
        if expected is not None and actual != expected:
            raise RuntimeError(f'placement mismatch: expected={expected}, actual={actual}')
        return {'argv': self.argv, 'pid': pid, 'actual_placement': actual,
                'status': Path(f'/proc/{pid}/status').read_text(),
                'scheduling': command(['chrt', '-p', pid]),
                'threads': command(['ps', '-L', '-p', pid, '-o',
                                    'pid,tid,psr,cls,rtprio,comm'])}


# Read raw pipe bytes: TextIO.readline() can block past a select timeout
# when the peer has sent a partial JSON line. Keep read-ahead for the next call.
CONTROL_BUFFERS = weakref.WeakKeyDictionary()


def receive_line(stream, timeout):
    pending = CONTROL_BUFFERS.pop(stream, b'')
    deadline = time.monotonic() + timeout
    while b'\n' not in pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise RuntimeError('control channel timeout')
        chunk = os.read(stream.fileno(), 65536)
        if not chunk:
            raise RuntimeError('control channel closed')
        pending += chunk
    line, tail = pending.split(b'\n', 1)
    if tail:
        CONTROL_BUFFERS[stream] = tail
    return json.loads(line)


def tx_worker():
    """No persistent remote daemon/files: lifetime is bounded by SSH and alarm."""
    jobs = []
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(
        RuntimeError('TX watchdog expired')))
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(
            RuntimeError('TX control process interrupted')))
    signal.alarm(300)
    try:
        cfg = receive_line(sys.stdin, 20)
        c, row = cfg['config'], cfg['row']
        signal.alarm(300 + 8 * c['stop_timeout_seconds'])
        os.sched_setaffinity(0, {c['tx_management_cpu']})
        repo = Path(c['tx_repo'])
        command([repo / 'udp_socket/bin/sender', '--help'])
        with tempfile.TemporaryDirectory(prefix='ace-tx-') as tmp:
            tmp = Path(tmp)
            monitor = Job(placement(c['tx_management_cpu']) +
                          ['mpstat', '-P', 'ALL', '1'], tmp / 'mpstat.log')
            jobs.append(monitor)
            net = None
            if row['network_bps']:
                net = Job(placement(c['tx_network_cpu']) + ['stdbuf', '-oL',
                    'iperf3', '-c', c['rx_address'], '-p', c['iperf_port'],
                    '-u', '-b', row['network_bps'], '-t', '600', '-i', '1'],
                    tmp / 'network.log')
                jobs.append(net)
                net.ready('connected', c['ready_timeout_seconds'])
            print(json.dumps({'ready': True, 'host': os.uname().nodename,
                'sender_sha256': hashlib.sha256((repo / 'udp_socket/bin/sender').read_bytes()).hexdigest(),
                'git_head': command(['git', '-C', repo, 'rev-parse', 'HEAD']).strip(),
                'git_diff': command(['git', '-C', repo, 'diff', 'HEAD', '--', 'udp_socket', 'include']),
                'jobs': [j.evidence() for j in jobs]}), flush=True)
            if receive_line(sys.stdin, 120) != {'send': True}:
                raise RuntimeError('invalid send command')
            senders = []
            start = time.monotonic()
            for flow, pps in enumerate(row['pps'], 1):
                if not pps:
                    continue
                prefix = 'pass' if flow == 1 else 'critical'
                j = Job(placement(flow - 1, c[prefix + '_policy'],
                                  c[prefix + '_priority']) + ['stdbuf', '-oL',
                    repo / 'udp_socket/bin/sender',
                    '--port', 8999 + flow, '--flow-id', flow,
                    '--count', pps * DURATION, '--period-ns', 1000000000 // pps,
                    '--payload-size', c['payload_size'], c['rx_address']], tmp / f'sender-{flow}.log')
                jobs.append(j)
                senders.append((flow, j, time.monotonic()))
            evidence = []
            for flow, j, _ in senders:
                # Wait until taskset/chrt have exec'd sender and it has installed
                # its handlers. A snapshot immediately after Popen can see the wrapper.
                j.ready('clock: CLOCK_REALTIME (TX), CLOCK_MONOTONIC (pacing)',
                        c['ready_timeout_seconds'])
                prefix = 'pass' if flow == 1 else 'critical'
                policy = {'other': os.SCHED_OTHER, 'fifo': os.SCHED_FIFO,
                          'rr': os.SCHED_RR}[c[prefix + '_policy']]
                evidence.append({'flow_id': flow, **j.evidence({
                    'cpus': [flow - 1], 'policy': policy,
                    'priority': c[prefix + '_priority']})})
            # Enforce the 60-second window even when the sender cannot keep up.
            while time.monotonic() - start < DURATION:
                if select.select([sys.stdin], [], [], .1)[0]:
                    raise RuntimeError('TX interrupted/disconnected during send')
                monitor.alive()
                if net:
                    net.alive()
            results = {}
            for flow, j, launched in senders:
                rc = j.stop(c['stop_timeout_seconds'])
                log = j.log.read_text()
                matches = re.findall(r'completed: sent=(\d+) errors=(\d+)', log)
                if len(matches) != 1:
                    raise RuntimeError('sender has no unambiguous completion record')
                sent, errors = map(int, matches[0])
                results[str(flow)] = {'sent': sent, 'errors': errors, 'returncode': rc,
                    'elapsed_seconds': time.monotonic() - launched,
                    'launch_offset_seconds': launched - start, 'log': log}
            print(json.dumps({'sent': results, 'placement': evidence}), flush=True)
            if receive_line(sys.stdin, 90 + 3 * c['stop_timeout_seconds']) != {'stop': True}:
                raise RuntimeError('invalid stop command')
            monitor.alive()
            if net:
                net.alive()
            for j in reversed(jobs):
                j.stop(c['stop_timeout_seconds'])
            print(json.dumps({'logs': {p.name: p.read_text() for p in tmp.iterdir()}}), flush=True)
    finally:
        for j in reversed(jobs):
            try:
                j.stop(3)
            except Exception:
                pass


def config(path):
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path):
        raise ValueError(f'cannot read {path}')
    c = dict(parser['experiment'])
    ints = ('top_pass_pps network_medium_bps network_high_bps iperf_port payload_size '
            'rx_management_cpu tx_management_cpu rx_network_cpu tx_network_cpu '
            'rx_pass_cpu rx_xsk_cpu pass_priority critical_priority drain_seconds '
            'stop_timeout_seconds ready_timeout_seconds').split()
    for key in ints:
        c[key] = int(c[key]) if c.get(key, '').strip() else 0
        if c[key] < 0:
            raise ValueError(f'{key} must be nonnegative')
    for key in ('load_cpus', 'nic_irqs'):
        c[key] = [int(x.strip()) for x in c[key].split(',') if x.strip()]
        if len(c[key]) != len(set(c[key])) or any(x < 0 for x in c[key]):
            raise ValueError(f'invalid {key}')
    if not c['load_cpus'] or 3 in c['load_cpus']:
        raise ValueError('baseline load_cpus must be nonempty and exclude CPU3')
    if c['rx_pass_cpu'] == 3:
        raise ValueError('baseline PASS receiver must not already be on CPU3')
    for prefix in ('pass', 'critical'):
        policy, priority = c[prefix + '_policy'], c[prefix + '_priority']
        if policy not in ('other', 'fifo', 'rr') or not (
                priority == 0 if policy == 'other' else 1 <= priority <= 99):
            raise ValueError(f'invalid {prefix} scheduling policy/priority')
    if c['top_pass_pps'] and not 9000 <= c['top_pass_pps'] <= 15476:
        raise ValueError('top_pass_pps must be 9000..15476 (record capacity)')
    if not 1 <= c['iperf_port'] <= 65535 or c['iperf_port'] in (9000, 9001, 9002):
        raise ValueError('invalid iperf_port')
    if not 0 <= c['payload_size'] <= 1440:
        raise ValueError('payload_size outside sender limits')
    for key in ('drain_seconds', 'stop_timeout_seconds', 'ready_timeout_seconds'):
        if not 1 <= c[key] <= 60:
            raise ValueError(f'{key} must be 1..60')
    ipaddress.IPv4Address(c['rx_address'])
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', c['tx_host']):
        raise ValueError('tx_host must be an SSH host alias or user@host')
    for key in ('rx_repo', 'tx_repo', 'kernel_tree', 'output_root', 'staging_root'):
        if not Path(c[key]).is_absolute():
            raise ValueError(f'{key} must be absolute')
    return c


def matrix(c):
    rows = []
    def add(label, pps, cpu=0, network=0, sharing='base'):
        row = {'pps': pps, 'cpu_percent': cpu, 'network_bps': network,
               'load_cpus': c['load_cpus'] + ([3] if sharing == 'load3' else []),
               'pass_cpu': 3 if sharing == 'pass3' else c['rx_pass_cpu']}
        for old in rows:
            if all(old[k] == v for k, v in row.items()):
                old['aliases'].append(label)
                return
        row.update(id=label, aliases=[label])
        rows.append(row)
    for i, name in enumerate(('PASS', 'CPUMAP', 'AF_XDP')):
        add('solo-' + name, [1000 if j == i else 0 for j in range(3)])
    for index, (cpu, net) in enumerate(((0, 0), (100, 0), (0, c['network_high_bps']),
                     (100, c['network_high_bps']), (50, 0),
                     (0, c['network_medium_bps']), (50, c['network_medium_bps'])), 1):
        # Keep unresolved network levels distinct in offline plans.
        label = f'A-{index}'
        add(label, [1000]*3, cpu, net)
    for pps in dict.fromkeys((0, 1000, 3000, 9000, c['top_pass_pps'])):
        for cpu in (0, 100):
            add(f'B-{pps}-{cpu}', [pps, 1000, 1000], cpu)
    for sharing in ('base', 'load3', 'pass3'):
        add('C-' + sharing, [1000]*3, 100, sharing=sharing)
    return rows


def check_inputs(c):
    if not c['top_pass_pps'] or not c['nic_irqs']:
        raise ValueError('fill preliminary top_pass_pps and nic_irqs before --run')
    if not 0 < c['network_medium_bps'] < c['network_high_bps']:
        raise ValueError('fill measured 0 < network_medium_bps < network_high_bps')


def irq_check(c):
    observed = {}
    for irq in c['nic_irqs']:
        root = Path(f'/proc/irq/{irq}')
        effective = (root / 'effective_affinity_list').read_text().strip()
        observed[str(irq)] = {'effective': effective,
            'requested': (root / 'smp_affinity_list').read_text().strip()}
        if effective != '2':
            raise RuntimeError(f'NIC IRQ {irq} effective CPU is {effective}, expected 2')
    return observed


def parameters(c, row, mode):
    return {k: json.dumps(v, sort_keys=True) if not isinstance(v, str) else v
            for k, v in {**c, **{k: v for k, v in row.items() if k not in ('id', 'aliases')},
                         'delivery_mode': mode, 'duration_seconds': 60,
                         'deadline_ns': 100000, 'stabilization_seconds': 5}.items()}


def analyze_run(c, row, mode, directory, tx):
    repo = Path(c['rx_repo'])
    manifest = json.loads((directory / 'manifest.json').read_text())
    completions = {}
    for flow, pps in enumerate(row['pps'], 1):
        if not pps:
            continue
        stem = f'flow-{flow}'
        payload = dict(manifest)
        payload['parameters'] = {**manifest['parameters'], 'flow_id': str(flow)}
        payload['run_id'] = manifest['run_id'] + f'-f{flow}'
        manifest_path = directory / f'{stem}.manifest.json'
        dump(manifest_path, payload)
        args = ['python3', repo / 'analysis/join_results.py', '--manifest', manifest_path,
                '--ingress', directory / 'ingress_records.csv',
                '--cpumap', directory / 'cpumap_records.csv',
                '--flow-id', flow, '--sent-count', tx['sent'][str(flow)]['sent'],
                '--target-count', pps * DURATION,
                '--phc-calibration', directory / 'phc-before.csv',
                '--phc-calibration', directory / 'phc-after.csv',
                '--output-csv', directory / f'{stem}.csv',
                '--summary-json', directory / f'{stem}.json']
        if mode == 'mixed' and flow == 3:
            args += ['--afxdp', directory / 'afxdp.csv']
        else:
            args += ['--udp-native', directory / f'udp-{flow}.bin']
        command(args, timeout=300)
        completions[str(flow)] = str(directory / f'{stem}.json.complete')
    return completions


def run_one(c, row, mode, repetition, root, source):
    run_id = f'{row["id"]}-{mode}-{repetition}'
    d = root / run_id
    d.mkdir()  # Never overwrite or infer completion from partial artifacts.
    repo = Path(c['rx_repo'])
    jobs, receivers, loads = [], [], []
    remote = None
    remote_log = None
    stage = None
    loader = None
    def spawn(argv, name):
        j = Job(argv, d / (name + '.log'))
        jobs.append(j)
        return j
    try:
        dump(d / 'irq-before.json', irq_check(c))
        (d / 'interrupts-before.txt').write_text(Path('/proc/interrupts').read_text())
        manifest_cmd = ['python3', repo / 'monitor/capture_manifest.py', d,
            '--interface', c['interface'], '--project-tree', repo,
            '--kernel-tree', c['kernel_tree'], '--kernel-base-ref', c['kernel_base_ref']]
        for k, v in parameters(c, row, mode).items():
            manifest_cmd += ['--set', f'{k}={v}']
        command(manifest_cmd, timeout=180)
        loader = spawn(placement(c['rx_management_cpu']) + ['stdbuf', '-oL',
            repo / 'BPF/xdp_loader', '--output-dir', d, '--records', sum(row['pps'])*60,
            '--mode', mode, c['interface']], 'loader')
        loader.ready('attached native xdp_dispatch', c['ready_timeout_seconds'])
        stage = tempfile.TemporaryDirectory(prefix='ace-matrix-', dir=c['staging_root'])
        for flow, pps in enumerate(row['pps'], 1):
            if not pps:
                continue
            cpu = {1: row['pass_cpu'], 2: 3, 3: c['rx_xsk_cpu']}[flow]
            prefix = 'pass' if flow == 1 else 'critical'
            argv = placement(cpu, c[prefix + '_policy'], c[prefix + '_priority']) + ['stdbuf', '-oL']
            if flow == 3 and mode == 'mixed':
                argv += [repo / 'BPF/afxdp_recv', '--output', d / 'afxdp.csv',
                         '--records', pps*60, c['interface'], '0']
                needle = 'AF_XDP ready:'
            else:
                argv += [repo / 'udp_socket/bin/receiver', '--bind', c['rx_address'],
                    '--port', 8999 + flow, '--flow-id', flow, '--count', pps*60,
                    '--payload-size', c['payload_size'], '--tmpfs',
                    Path(stage.name) / f'{flow}.bin', '--output', d / f'udp-{flow}.bin']
                needle = 'raw hardware timestamp:'
            j = spawn(argv, f'receiver-{flow}')
            receivers.append(j)
            j.ready(needle, c['ready_timeout_seconds'])
        if row['cpu_percent']:
            for cpu in row['load_cpus']:
                j = spawn(placement(cpu) + ['stress-ng', '--cpu', '1', '--cpu-load',
                    row['cpu_percent'], '--timeout', '600s', '--metrics-brief'], f'load-{cpu}')
                j.ready('dispatching hogs', c['ready_timeout_seconds'])
                loads.append(j)
        monitor = spawn(placement(c['rx_management_cpu']) +
                        ['mpstat', '-P', 'ALL', '1'], 'mpstat')
        net = None
        if row['network_bps']:
            net = spawn(placement(c['rx_network_cpu']) + ['stdbuf', '-oL', 'iperf3',
                        '-s', '-1', '-p', c['iperf_port'], '-i', '1'], 'iperf-server')
            net.ready('Server listening', c['ready_timeout_seconds'])
        remote_log = (d / 'ssh.log').open('x')
        remote_cmd = ['sudo', '-n', 'taskset', '-c', str(c['tx_management_cpu']),
                      'chrt', '-o', '0', 'python3', '-u', '-c', source, '--tx-worker']
        remote = subprocess.Popen(['ssh', '-T', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
            c['tx_host'], shlex.join(remote_cmd)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=remote_log, text=True, bufsize=1,
            start_new_session=True)
        remote.stdin.write(json.dumps({'config': c, 'row': row}) + '\n')
        remote.stdin.flush()
        tx_ready = receive_line(remote.stdout, c['ready_timeout_seconds'] + 30)
        if not tx_ready.get('ready'):
            raise RuntimeError('TX not ready')
        dump(d / 'tx-ready.json', tx_ready)
        manifest_path = d / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['transmitter'] = tx_ready
        for key in ('host', 'sender_sha256', 'git_head', 'git_diff'):
            manifest['parameters']['tx_' + key] = tx_ready[key]
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        dump(d / 'placement.json', [j.evidence() for j in jobs])
        time.sleep(5)
        for j in jobs:
            j.alive()
        def calibrate(name):
            output = command(placement(c['rx_management_cpu']) +
                [repo / 'udp_socket/bin/phc_calibrate', '--interface', c['interface'],
                 '--samples', 25, '--iterations', 10, '--output', d / f'phc-{name}.csv'])
            (d / f'calibrate-{name}.log').write_text(output)
        calibrate('before')
        remote.stdin.write('{"send": true}\n')
        remote.stdin.flush()
        tx = receive_line(remote.stdout, 60 + c['stop_timeout_seconds'] + 15)
        dump(d / 'tx-result.json', tx)
        time.sleep(c['drain_seconds'])
        loader.alive()
        for j in receivers:
            if any(str(arg).endswith('/BPF/afxdp_recv') for arg in j.argv):
                j.alive()  # AF_XDP never normally exits on reaching record capacity.
            if j.stop(c['stop_timeout_seconds']) != 0:
                raise RuntimeError(f'receiver failed: {j.log}')
        calibrate('after')
        remote.stdin.write('{"stop": true}\n')
        remote.stdin.flush()
        dump(d / 'tx-logs.json', receive_line(remote.stdout, c['stop_timeout_seconds'] + 15))
        if remote.wait(timeout=15) != 0:
            raise RuntimeError('SSH/TX worker failed')
        for j in loads + [monitor]:
            j.alive()
            j.stop(c['stop_timeout_seconds'])
        if net:
            net.stop(c['stop_timeout_seconds'])
        dump(d / 'irq-after.json', irq_check(c))
        (d / 'interrupts-after.txt').write_text(Path('/proc/interrupts').read_text())
        if loader.stop(c['stop_timeout_seconds']) != 0:
            raise RuntimeError('loader export/cleanup failed; retain pins for manual recovery')
        completions = analyze_run(c, row, mode, d, tx)
        dump(d / 'run.complete.json', {'completions': completions,
             'sender_errors': {f: v['errors'] for f, v in tx['sent'].items()}})
        return completions
    except BaseException as error:
        dump(d / 'FAILED.json', {'error': str(error)})
        raise
    finally:
        if remote:
            if remote.stdin:
                try:
                    remote.stdin.close()
                except BrokenPipeError:
                    pass  # EOF asks remote worker to clean its own children.
            try:
                remote.wait(timeout=10)
            except subprocess.TimeoutExpired:
                remote.terminate()
                try:
                    remote.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    remote.kill()
                    remote.wait()
        # Reverse start order guarantees AF_XDP receiver before loader.
        for j in reversed(jobs):
            try:
                j.stop(c['stop_timeout_seconds'])
            except Exception as error:
                print(f'cleanup: {error}', file=sys.stderr)
        if stage:
            stage.cleanup()
        if remote_log:
            remote_log.close()


def main():
    parser = argparse.ArgumentParser(prog='run_matrix.sh', description='D01–D11 matrix; run ON receiver Linux. '
        'Default --plan is offline and never uses SSH. --run starts local RX/XDP/load '
        'and controls TX over one key-authenticated SSH session with sudo -n. '
        'Requires root on RX, prebuilt tools on both hosts, verified SSH host key, '
        'noninteractive TX sudo and preconfigured NIC IRQ CPU2. No passwords, '
        'IRQ writes, Pi preliminary measurements, build, deployment or recovery. '
        'See config.example.conf for permissions and CPU settings.',
        epilog='Usage: ./experiment/run_matrix.sh --config /path/matrix.conf --plan; '
        'sudo -n ./experiment/run_matrix.sh --config /path/matrix.conf --run --session study01. '
        'Order: PASS/CPUMAP/AF_XDP solo, A (7 rows), B (PPS x CPU), C (3 sharing rows). '
        'Equivalent rows reuse the same runs via plan aliases. Each condition runs both modes '
        '5 times, alternating pair order. Ready -> stabilize 5s -> PHC before -> send 60s '
        '-> bounded drain/receiver stop -> PHC after -> export/join/aggregate. '
        'Critical flow targets: 60000 each, deadline 100000ns. TX CPUs: 0/1/2, iperf3 default 3. '
        'RX9001/CPUMAP CPU3, IRQ CPU2; other placements are in config. '
        'Outputs include raw logs, mpstat on both hosts, IRQ/affinity/policy evidence, '
        'per-flow target/actual/loss and calibrated latency, and five-run aggregates. '
        'Failures retain FAILED.json/raw files; no automatic recovery or partial-run reuse. '
        'Loss totals are in aggregate JSON; existing latency CSV format is preserved.')
    parser.add_argument('--config', required=True, type=Path)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--plan', action='store_true', help='print deduplicated matrix, no device access (default)')
    actions.add_argument('--run', action='store_true', help='execute all rows on RX + SSH TX; explicit opt-in')
    parser.add_argument('--session', default=time.strftime('%Y%m%d-%H%M%S'),
                        help='new result directory name; existing sessions are never overwritten')
    args = parser.parse_args(sys.argv[2:])
    c = config(args.config)
    # Missing measurements remain symbolic in plans; execution always requires numbers.
    plan_c = dict(c)
    if not c['network_medium_bps']:
        plan_c['network_medium_bps'] = 'PREMEASURED_MEDIUM'
    if not c['network_high_bps']:
        plan_c['network_high_bps'] = 'PREMEASURED_HIGH'
    rows = matrix(plan_c if not args.run else c)
    plan = {'duration_seconds': 60, 'stabilization_seconds': 5, 'repeats_per_mode': 5,
            'deadline_ns': 100000, 'conditions': rows,
            'runs': [{'condition': r['id'], 'repetition': rep, 'mode': mode}
                     for r in rows for rep in range(1, 6)
                     for mode in (('all-pass', 'mixed') if rep % 2 else ('mixed', 'all-pass'))],
            'top_pass_pps_pending': not bool(c['top_pass_pps']),
            'pending_conditions': ([] if c['top_pass_pps'] else
                ['B-PREMEASURED_TOP-0', 'B-PREMEASURED_TOP-100'])}
    if not args.run:
        print(json.dumps(plan, indent=2))
        return
    check_inputs(c)
    if os.geteuid() != 0:
        raise ValueError('--run requires root on the receiver; use sudo -n')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,39}', args.session):
        raise ValueError('unsafe session name')
    os.sched_setaffinity(0, {c['rx_management_cpu']})
    os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
    for executable in ('taskset', 'chrt', 'stdbuf', 'stress-ng', 'mpstat', 'iperf3', 'ssh', 'git'):
        if shutil.which(executable) is None:
            raise ValueError(f'missing RX executable: {executable}')
    for relative in ('BPF/xdp_loader', 'BPF/afxdp_recv', 'udp_socket/bin/receiver',
                     'udp_socket/bin/phc_calibrate'):
        if not os.access(Path(c['rx_repo']) / relative, os.X_OK):
            raise ValueError(f'build RX executable first: {relative}')
    irq_check(c)
    root = Path(c['output_root']) / args.session
    root.mkdir(parents=True, exist_ok=False)
    dump(root / 'plan.json', plan)
    dump(root / 'config.json', c)
    source = Path(sys.argv[1]).read_text().split("<<'PYTHON'\n", 1)[1].rsplit('\nPYTHON', 1)[0]
    groups = {}
    for item in plan['runs']:
        row = next(r for r in rows if r['id'] == item['condition'])
        print(f'{item["condition"]} {item["mode"]} repeat {item["repetition"]}/5', flush=True)
        completions = run_one(c, row, item['mode'], item['repetition'], root, source)
        for flow, marker in completions.items():
            groups.setdefault((row['id'], item['mode'], flow), []).append(marker)
    # Alias conditions share these exact five independent runs, never copies.
    for (condition, mode, flow), markers in groups.items():
        stem = root / f'{condition}-{mode}-flow-{flow}.aggregate'
        argv = ['python3', Path(c['rx_repo']) / 'analysis/aggregate_runs.py',
                '--deadline-ns', 100000, '--output-csv', str(stem) + '.csv',
                '--summary-json', str(stem) + '.json']
        for marker in markers:
            argv += ['--run', marker]
        command(argv, timeout=300)
    dump(root / 'matrix.complete.json', {'conditions': len(rows), 'runs': len(plan['runs'])})
    print(f'Complete: {root}')


if __name__ == '__main__':
    try:
        if '--tx-worker' in sys.argv:
            tx_worker()
        else:
            signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            main()
    except (Exception, KeyboardInterrupt) as error:
        print(f'error: {error}', file=sys.stderr)
        sys.exit(1)
PYTHON
