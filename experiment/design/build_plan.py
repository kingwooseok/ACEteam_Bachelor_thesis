#!/usr/bin/env python3
"""D01/D11 실행표와 양측 공통 설정 생성. 실행·접속·설정 변경은 하지 않는다."""
import argparse
import csv
import io
import ipaddress
import json
from pathlib import Path

PROFILE = Path(__file__).with_name('profile.example.json')


def rows(high=9000):
    # 같은 조건은 한 번만 보관하고 A/B/C 참조를 함께 남긴다.
    result = []
    def add(label, pps, cpu, network='none', cpus='0,1,2', shared=False):
        signature = (pps, cpu, network, cpus, shared)
        for row in result:
            if row['_signature'] == signature:
                row['references'].append(label)
                return
        result.append(dict(case_id=f'main-{len(result)+1:02d}', references=[label],
                           pass_pps=pps, cpu_percent=cpu, network=network,
                           load_cpus=cpus, pass_shares_cpu3=shared, single_flow=None,
                           _signature=signature))
    for cpu in (0, 100):
        for network in ('none', 'high'):
            add(f'A-cpu{cpu}-{network}', 1000, cpu, network)
    for pps in (0, 1000, 3000, high):
        for cpu in (0, 100):
            add(f'B-pass{pps}-cpu{cpu}', pps, cpu)
    add('C-base', 1000, 100)
    add('C-load-share', 1000, 100, cpus='0,1,2,3')
    add('C-receiver-share', 1000, 100, shared=True)
    add('middle-cpu', 1000, 50)
    add('middle-network', 1000, 0, 'middle')
    add('middle-both', 1000, 50, 'middle')
    singles = [dict(case_id=f'single-{i}', references=[f'D01-flow{i}'], pass_pps=1000,
                    cpu_percent=0, network='none', load_cpus='0,1,2',
                    pass_shares_cpu3=False, single_flow=i) for i in range(1, 4)]
    for row in result:
        del row['_signature']
    return singles + result


def validate(profile):
    expected = set(json.loads(PROFILE.read_text()))
    if set(profile) != expected:
        raise ValueError('profile keys must match profile.example.json')
    missing = [key for key, value in profile.items()
               if value is None or isinstance(value, list) and any(v is None for v in value)]
    if missing:
        raise ValueError('unresolved experimental choices: ' + ', '.join(missing))
    def integer(value, low, high, name):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'invalid {name}: expected integer {low}..{high}')
    ipaddress.IPv4Address(profile['receiver_address'])
    for name in ('receiver_cpus', 'receiver_priorities', 'sender_priorities',
                 'receiver_policies', 'sender_policies'):
        if not isinstance(profile[name], list) or len(profile[name]) != 3:
            raise ValueError(f'{name} needs three values')
    for value in profile['receiver_cpus']:
        integer(value, 0, 3, 'receiver CPU')
    if profile['receiver_cpus'][1] != 3 or any(profile['receiver_cpus'][i] == 3 for i in (0, 2)):
        raise ValueError('base CPUMAP receiver must use CPU3; other receivers use CPU0..2')
    for side in ('receiver', 'sender'):
        for policy, priority in zip(profile[f'{side}_policies'], profile[f'{side}_priorities']):
            if policy not in ('other', 'rr', 'fifo'):
                raise ValueError('invalid scheduler policy')
            integer(priority, 0 if policy == 'other' else 1,
                    0 if policy == 'other' else 99, 'scheduler priority')
        integer(profile[f'{side}_supervisor_cpu'], 0, 3, 'supervisor CPU')
    integer(profile['receiver_network_cpu'], 0, 2, 'receiver network CPU')
    integer(profile['receiver_supervisor_cpu'], 0, 2, 'receiver supervisor CPU')
    integer(profile['network_high_bps'], 1, 1000000000, 'network high bps')
    integer(profile['network_middle_bps'], 1, profile['network_high_bps'] - 1, 'network middle bps')
    integer(profile['pass_high_pps'], 3001, 15000, 'PASS high PPS (record capacity limit)')
    integer(profile['settle_ms'], 0, 60000, 'settle ms')


def config(profile, row, index):
    # 확정된 공통 조건만 고정하고 미확정 값은 profile에서 요구한다.
    values = dict(run_id=row['case_id'], receiver_address=profile['receiver_address'],
                  control_port=49090, repo='AUTO', output_root='results/experiment',
                  interface='eth0', modes='all-pass,mixed' if index % 2 == 0 else 'mixed,all-pass',
                  repeats=5, alternate_mode_order=1, duration_ms=60000, warmup_ms=5000,
                  settle_ms=profile['settle_ms'], ready_timeout_ms=60000,
                  stop_timeout_ms=60000, start_delay_ms=250, drain_ms=100,
                  supervisor_cpu='inherit', monitor=1)
    values.update({'receiver.supervisor_cpu': profile['receiver_supervisor_cpu'],
                   'sender.supervisor_cpu': profile['sender_supervisor_cpu'],
                   'receiver.network_cpu': profile['receiver_network_cpu'],
                   'sender.network_cpu': 3,
                   'environment.governor': 'performance', 'environment.rx_irq_cpu': 2,
                   'load.cpu_percent': row['cpu_percent'], 'load.cpu_cpus': row['load_cpus'],
                   'load.cpu_method': 'all', 'load.memory_workers': 0, 'load.memory_bytes': 0,
                   'load.io_workers': 0, 'load.io_path': '/tmp',
                   'load.network_bps': 0 if row['network'] == 'none' else profile[f"network_{row['network']}_bps"],
                   'load.network_protocol': 'udp', 'load.network_direction': 'forward',
                   'load.network_port': 5201, 'load.network_parallel': 1,
                   'load.on_sec': 0, 'load.off_sec': 0})
    # 단독 3행은 우선 mixed의 해당 경로만 측정한다. 본시험 비교 횟수와 분리한다.
    if row['single_flow']:
        values['modes'] = 'mixed'
    for i in range(1, 4):
        pps = row['pass_pps'] if i == 1 else 1000
        if row['single_flow']:
            pps = 1000 if i == row['single_flow'] else 0
        values.update({f'flow{i}.enabled': int(pps > 0),
                       f'flow{i}.period_ns': round(1000000000 / pps) if pps else 1000000,
                       f'flow{i}.deadline_ns': 0 if i == 1 else 100000,
                       f'flow{i}.tx_cpu': i - 1,
                       f'flow{i}.rx_cpu': 3 if i == 1 and row['pass_shares_cpu3'] else profile['receiver_cpus'][i - 1],
                       f'flow{i}.tx_policy': profile['sender_policies'][i - 1],
                       f'flow{i}.rx_policy': profile['receiver_policies'][i - 1],
                       f'flow{i}.tx_priority': profile['sender_priorities'][i - 1],
                       f'flow{i}.rx_priority': profile['receiver_priorities'][i - 1]})
    return '# Generated common config: copy identical bytes to both Pis.\n' + ''.join(
        f'{key}={value}\n' for key, value in values.items())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text()) if args.profile else None
    if profile is not None:
        try:
            validate(profile)
        except (ValueError, TypeError) as error:
            parser.error(str(error))
    plan = rows(profile['pass_high_pps'] if profile else 9000)
    # 기존 산출물/설정을 덮어쓰지 않고 새 폴더만 생성한다.
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / 'matrix.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(plan[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows({**row, 'references': ';'.join(row['references'])} for row in plan)
    summary = dict(status='configured-unvalidated' if profile else 'draft-unresolved',
                   main_conditions=len(plan)-3, main_runs=(len(plan)-3)*10,
                   single_runs=15, duration_ms=60000, warmup_ms=5000,
                   note='No deployment or experiment execution. Draft PASS high=9000 is provisional.')
    (args.output / 'plan.json').write_text(json.dumps(summary, indent=2) + '\n')
    if profile:
        (args.output / 'profile.json').write_text(json.dumps(profile, indent=2) + '\n')
        for index, row in enumerate(plan):
            (args.output / f"{row['case_id']}.conf").write_text(config(profile, row, index))
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
