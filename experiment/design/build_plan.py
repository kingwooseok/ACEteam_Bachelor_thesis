#!/usr/bin/env python3
"""D01/D11 실행표와 양측 공통 설정 생성. 실행·접속·설정 변경은 하지 않는다."""
import argparse
import csv
import hashlib
import ipaddress
import json
from pathlib import Path

PROFILE = Path(__file__).with_name('profile.example.json')
DESIGN_REVISION = 'fixed-1000pps-75-v4'
REPEATS = 5


def write_json(path, value):
    path.write_bytes((json.dumps(value, indent=2) + '\n').encode('utf-8'))


def rows(full_design=False, profile=None):
    profile = profile or {}
    workers = profile.get('load_workers', 3)
    rates = profile.get('flow_pps', [1000, 1000, 1000])
    # The compatibility flag generates the same fixed-rate four-condition design.
    result = []
    def add(label, cpu, network):
        result.append(dict(case_id=f'main-{len(result)+1:02d}', references=[label],
                           pass_pps=rates[0], cpu_percent=cpu, network=network,
                           load_cpus='per-mode-general', load_workers=workers, single_flow=None,
                           modes=['all-pass', 'pass-xsk', 'mixed']))
    for cpu in (0, 100):
        for network in ('none', 'high'):
            add(f'A-cpu{cpu}-{network}', cpu, network)
    single_modes = ('all-pass', 'mixed', 'pass-xsk')
    singles = [dict(case_id=f'single-{i}', references=[f'D01-flow{i}'], pass_pps=rates[0],
                    cpu_percent=0, network='none', load_cpus='per-mode-general', load_workers=workers,
                    single_flow=i, modes=[single_modes[i-1]]) for i in range(1, 4)]
    return singles + result


def validate(profile, full_design=False):
    expected = set(json.loads(PROFILE.read_text()))
    optional = {'flow_pps', 'load_workers', 'xsk_busy', 'rx_coalesce_usecs', 'phc_tracking'}
    if not expected <= set(profile) or set(profile) - expected - optional:
        raise ValueError('profile keys must match profile.example.json plus supported runtime options')
    missing = [key for key, value in profile.items()
               if value is None or isinstance(value, list) and any(v is None for v in value)]
    if missing:
        raise ValueError('unresolved experimental choices: ' + ', '.join(missing))
    def integer(value, low, high, name):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'invalid {name}: expected integer {low}..{high}')
    ipaddress.IPv4Address(profile['receiver_address'])
    for name in ('receiver_priorities', 'sender_priorities',
                 'receiver_policies', 'sender_policies'):
        if not isinstance(profile[name], list) or len(profile[name]) != 3:
            raise ValueError(f'{name} needs three values')
    shared = profile['receiver_shared_cpus']
    if not isinstance(shared, list) or any(type(value) is not int for value in shared) or shared != [0, 1, 2, 3]:
        raise ValueError('receiver_shared_cpus must be [0,1,2,3] for the available Pi execution domain')
    integer(profile['receiver_cpumap_cpu'], 3, 3, 'CPUMAP CPU')
    for side in ('receiver', 'sender'):
        for policy, priority in zip(profile[f'{side}_policies'], profile[f'{side}_priorities']):
            if policy not in ('other', 'rr', 'fifo'):
                raise ValueError('invalid scheduler policy')
            integer(priority, 0 if policy == 'other' else 1,
                    0 if policy == 'other' else 99, 'scheduler priority')
    integer(profile['sender_supervisor_cpu'], 0, 3, 'sender supervisor CPU')
    for name in ('receiver_network_cpu', 'receiver_supervisor_cpu'):
        if profile[name] != 'inherit':
            raise ValueError(f'{name} must inherit the shared execution domain')
    integer(profile['network_high_bps'], 1, 1000000000, 'network high bps')
    integer(profile['settle_ms'], 0, 60000, 'settle ms')
    rates = profile.get('flow_pps', [1000]*3)
    if not isinstance(rates, list) or len(rates) != 3:
        raise ValueError('flow_pps needs three values')
    for rate in rates:
        integer(rate, 1, 1000000, 'flow PPS')
    integer(profile.get('load_workers', 3), 1, 64, 'load workers')
    for key in ('xsk_busy', 'phc_tracking'):
        integer(profile.get(key, 0), 0, 1, key)
    if 'rx_coalesce_usecs' in profile:
        integer(profile['rx_coalesce_usecs'], 0, 10000, 'RX coalesce usecs')


def config(profile, row, index, full_design=False):
    # 확정된 공통 조건만 고정하고 미확정 값은 profile에서 요구한다.
    modes = list(row['modes'])
    offset = index % len(modes)
    modes = modes[offset:] + modes[:offset]
    values = dict(run_id=row['case_id'], receiver_address=profile['receiver_address'],
                  control_port=49090, repo='AUTO', output_root='results/experiment',
                  interface='eth0', modes=','.join(modes),
                  repeats=REPEATS, alternate_mode_order=1, verify_placement=1,
                  duration_ms=60000, warmup_ms=5000,
                  settle_ms=profile['settle_ms'], ready_timeout_ms=60000,
                  stop_timeout_ms=60000, start_delay_ms=250, drain_ms=100,
                  supervisor_cpu='inherit', monitor=1)
    values.update({'receiver.supervisor_cpu': profile['receiver_supervisor_cpu'],
                   'receiver.shared_cpus': ','.join(map(str, profile['receiver_shared_cpus'])),
                   'receiver.cpumap_cpu': profile['receiver_cpumap_cpu'],
                   'sender.supervisor_cpu': profile['sender_supervisor_cpu'],
                   'receiver.network_cpu': profile['receiver_network_cpu'],
                   'sender.network_cpu': 3,
                   'environment.governor': 'performance', 'environment.rx_irq_cpu': 2,
                   'load.cpu_percent': row['cpu_percent'],
                   'load.cpu_cpus': ','.join(map(str, profile['receiver_shared_cpus'])),
                   'load.cpu_layout': 'shared', 'load.cpu_workers': row['load_workers'],
                   'load.cpu_method': 'all', 'load.memory_workers': 0, 'load.memory_bytes': 0,
                   'load.io_workers': 0, 'load.io_path': '/tmp',
                   'load.network_bps': 0 if row['network'] == 'none' else profile[f"network_{row['network']}_bps"],
                   'load.network_protocol': 'udp', 'load.network_direction': 'forward',
                   'load.network_port': 5201, 'load.network_parallel': 1,
                   'load.on_sec': 0, 'load.off_sec': 0})
    for key, native in (('xsk_busy', 'receiver.xsk_busy'), ('phc_tracking', 'measurement.phc_tracking'),
                        ('rx_coalesce_usecs', 'environment.rx_coalesce_usecs')):
        if key in profile:
            values[native] = profile[key]
    for i in range(1, 4):
        pps = profile.get('flow_pps', [1000]*3)[i-1]
        if row['single_flow'] and i != row['single_flow']:
            pps = 0
        values.update({f'flow{i}.enabled': int(pps > 0),
                       f'flow{i}.period_ns': round(1000000000 / pps) if pps else 1000000,
                       f'flow{i}.deadline_ns': 0 if i == 1 else 100000,
                       f'flow{i}.tx_cpu': i - 1,
                       f'flow{i}.rx_cpu': 'inherit',
                       f'flow{i}.tx_policy': profile['sender_policies'][i - 1],
                       f'flow{i}.rx_policy': profile['receiver_policies'][i - 1],
                       f'flow{i}.tx_priority': profile['sender_priorities'][i - 1],
                       f'flow{i}.rx_priority': profile['receiver_priorities'][i - 1]})
    # The native runner uses all CPUs until an enabled CPUMAP flow reserves CPU3.
    # Receivers and the configured load workers share the current general domain.
    return '# Generated common config: copy identical bytes to both Pis.\n' + ''.join(
        f'{key}={value}\n' for key, value in values.items())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--d01-d11', action='store_true',
                        help='compatibility flag: generate the fixed-1000-PPS 75-run D01-D11 design')
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text()) if args.profile else None
    if profile is not None:
        try:
            validate(profile, args.d01_d11)
        except (ValueError, TypeError) as error:
            parser.error(str(error))
    plan = rows(full_design=args.d01_d11, profile=profile)
    revision = 'mixed-rates-75-v5' if profile and 'flow_pps' in profile else DESIGN_REVISION
    workers = (profile or {}).get('load_workers', 3)
    # 기존 산출물/설정을 덮어쓰지 않고 새 폴더만 생성한다.
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / 'matrix.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(plan[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows({**row, 'references': ';'.join(row['references']),
                         'modes': ';'.join(row['modes'])} for row in plan)
    main_runs = sum(len(row['modes']) * REPEATS for row in plan if not row['single_flow'])
    single_runs = sum(len(row['modes']) * REPEATS for row in plan if row['single_flow'])
    summary = dict(status='configured-unvalidated' if profile else 'draft-unresolved',
                   design='D01-D11', design_revision=revision,
                   main_conditions=len(plan)-3, main_runs=main_runs,
                   single_runs=single_runs, total_runs=main_runs+single_runs,
                   all_pass_runs=25, pass_xsk_runs=25, mixed_runs=25,
                   duration_ms=60000, warmup_ms=5000,
                   cpu_domains={'shared': '0,1,2,3', 'cpumap_active_general': '0,1,2', 'cpumap': '3'},
                   load_workers=workers, load_cpu_layout='shared',
                   flow_pps=(profile or {}).get('flow_pps', 1000),
                   note='Configured flow rates stay fixed across four CPU/iperf3 load conditions. CPU3 is reserved only for an enabled CPUMAP flow; the configured workers share the active general domain.')
    write_json(args.output / 'plan.json', summary)
    if profile:
        write_json(args.output / 'profile.json', profile)
        cases = []
        for index, row in enumerate(plan):
            name = f"{row['case_id']}.conf"
            # Keep the saved configuration and its digest identical on Windows
            # and Linux: text-mode output would translate LF to CRLF on Windows.
            data = config(profile, row, index, args.d01_d11).encode('utf-8')
            (args.output / name).write_bytes(data)
            values = dict(line.split('=', 1) for line in data.decode('utf-8').splitlines()
                          if line and not line.startswith('#'))
            modes = values['modes'].split(',')
            cases.append(dict(case_id=row['case_id'], config=name,
                              sha256=hashlib.sha256(data).hexdigest(),
                              references=row['references'], expected_modes=modes,
                              repeats=REPEATS, expected_runs=len(modes)*REPEATS))
        write_json(args.output / 'execution.json',
                   dict(design='D01-D11', design_revision=revision, cases=cases))
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
