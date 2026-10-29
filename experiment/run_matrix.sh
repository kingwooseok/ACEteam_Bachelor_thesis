#!/usr/bin/env bash
# D01-D11 condition sequencing only. ace-run remains the sole device controller.
set -euo pipefail
exec python3 - "$0" "$@" <<'PYTHON'
import argparse
import hashlib
import json
from pathlib import Path
import re
import signal
import subprocess
import sys

ROOT = Path(sys.argv[1]).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_cases(directory):
    directory = directory.resolve(strict=True)
    execution = json.loads((directory / 'execution.json').read_text())
    if execution.get('design') != 'D01-D11' or not execution.get('cases'):
        raise ValueError('requires a configured --d01-d11 plan')
    seen = set()
    cases = execution['cases']
    for case in cases:
        name = case['case_id']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name) or name in seen:
            raise ValueError('invalid or duplicate case_id')
        seen.add(name)
        if case['config'] != name + '.conf' or case['expected_runs'] != 10:
            raise ValueError('invalid D01-D11 case entry')
        path = directory / case['config']
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'not a regular configuration: {path}')
        if hashlib.sha256(path.read_bytes()).hexdigest() != case['sha256']:
            raise ValueError(f'configuration changed; regenerate plan: {path}')
    return cases


def execute(role, directory, cases):
    runner = ROOT / 'experiment/ace-run'
    # Validate every file before the first device/environment action.
    for case in cases:
        subprocess.run([str(runner), 'plan', '--config', str(directory / case['config'])],
                       cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    for case in cases:
        print(f'{role}: {case["case_id"]} ({", ".join(case["references"])})', flush=True)
        child = subprocess.Popen([str(runner), role, '--config', str(directory / case['config'])], cwd=ROOT)
        try:
            status = child.wait()
        finally:
            if child.poll() is None:
                child.terminate()
                # ace-run owns child cleanup, drain/flush and bounded waits.
                child.wait()
        if status:
            raise RuntimeError(f'ace-run failed ({status}); matrix stopped at {case["case_id"]}')


def collect(directory, cases, receiver, sender, output):
    from analysis import analyze_experiment as analysis
    receiver = receiver.resolve(strict=True)
    sender = sender.resolve(strict=True)
    output = output.resolve()
    if output.is_relative_to(receiver) or output.is_relative_to(sender):
        raise ValueError('collection output must be outside source trees')
    output.mkdir(parents=False, exist_ok=False)
    entries = []
    errors = []
    for case in cases:
        name = case['case_id']
        destination = output / name
        try:
            analysis.collect_suite(receiver / name / 'receiver', sender / name / 'sender', destination)
            suite = json.loads((destination / 'suite.json').read_text())
            expected = {(mode, repeat) for mode in ('all-pass', 'mixed') for repeat in range(1, 6)}
            actual = [(item.get('mode'), item.get('repeat')) for item in suite['runs']]
            if len(actual) != 10 or set(actual) != expected:
                raise ValueError('expected five runs of each mode')
            for item in suite['runs']:
                descriptor = json.loads((destination / item['dir'] / 'run.json').read_text())
                if descriptor['config']['sha256'] != case['sha256']:
                    raise ValueError('collected run differs from planned configuration')
            entries.extend({**item, 'dir': name + '/' + item['dir']} for item in suite['runs'])
        except (OSError, ValueError, KeyError) as error:
            # Keep all raw collection evidence, but prevent incomplete case aggregation.
            failure = output / (name + '-incomplete')
            failure.mkdir()
            (failure / '.collection-failed').write_text(str(error) + '\n')
            entries.append(dict(run_id=name, state='failed', dir=failure.name))
            errors.append(dict(case_id=name, reason=str(error)))
    (output / 'suite.json').write_text(json.dumps(dict(runs=entries, collection_errors=errors), indent=2) + '\n')
    (output / 'execution.json').write_bytes((directory / 'execution.json').read_bytes())
    # Existing analyzer performs join, per-flow delivery, deadline and repeated-run statistics.
    return analysis.main(['--suite-dir', str(output)])


def main():
    parser = argparse.ArgumentParser(prog='run_matrix.sh', description=
        'D01-D11 opt-in wrapper around the native ace-run; no SSH or alternate controller.',
        epilog='Generate on a workstation. Copy the identical plan directory to both devices. '
        'Start receiver on RX, then sender on TX. Build ace-run/UDP/BPF beforehand; '
        'use the existing ace-run permissions (usually sudo). Its environment setup, '
        'IRQ discovery, state capture and failure cleanup remain active. No passwords. '
        'collect is offline: copy both output_root trees first; use native-compatible UDP ABI. '
        'Failures stop sequencing; existing run IDs/results are never overwritten. '
        'See experiment/README.md for commands and retained legacy behavior.')
    sub = parser.add_subparsers(dest='action', required=True)
    plan = sub.add_parser('plan', help='offline: generate explicit D01-D11 configs using the existing profile format')
    plan.add_argument('--profile', type=Path, required=True)
    plan.add_argument('--output', type=Path, required=True)
    for role in ('receiver', 'sender'):
        role_parser = sub.add_parser(role, help=f'run locally on {role}; executes native ace-run per case')
        role_parser.add_argument('--plan-dir', type=Path, required=True)
    report = sub.add_parser('collect', help='offline collection and existing per-flow report/aggregation')
    report.add_argument('--plan-dir', type=Path, required=True)
    report.add_argument('--receiver-root', type=Path, required=True)
    report.add_argument('--sender-root', type=Path, required=True)
    report.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[2:])
    if args.action == 'plan':
        return subprocess.call([sys.executable, str(ROOT / 'experiment/design/build_plan.py'),
            '--d01-d11', '--profile', str(args.profile), '--output', str(args.output)])
    directory = args.plan_dir.resolve(strict=True)
    cases = load_cases(directory)
    if args.action in ('receiver', 'sender'):
        execute(args.action, directory, cases)
        return 0
    return collect(directory, cases, args.receiver_root, args.sender_root, args.output)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as error:
        print(f'error: {error}', file=sys.stderr)
        sys.exit(1)
PYTHON
