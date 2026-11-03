"""Validate continuous PHC coverage against every captured packet, including loss."""
import csv
from .join_results import load_calibration


def validate_clock(joined_path, calibration_path):
    calibration = load_calibration(calibration_path)
    report = dict(packets=0, received_packets=0, unreceived_packets=0,
                  missing_total=0, negative_d1=0, negative_total=0, invalid_decomposition=0)
    first = last = None
    with joined_path.open(encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream):
            report['packets'] += 1
            timestamps = [int(row[key]) for key in ('initial_xdp_ns', 'user_rx_mono_ns') if row[key]]
            if timestamps:
                first = min(timestamps + ([] if first is None else [first]))
                last = max(timestamps + ([] if last is None else [last]))
            received = bool(row['user_rx_mono_ns'])
            report['received_packets' if received else 'unreceived_packets'] += 1
            report['missing_total'] += int(received and not row['user_hw_ns'])
            for key, field in (('negative_d1', 'xdp_hw_ns'), ('negative_total', 'user_hw_ns')):
                report[key] += int(bool(row[field]) and int(row[field]) < 0)
            allowed = ('complete',) if received else ('complete', 'incomplete')
            report['invalid_decomposition'] += int(row['segment_status'] not in allowed)
    points = calibration.points
    gaps = [b.monotonic_ns-a.monotonic_ns for a,b in zip(points, points[1:])
            if first is not None and b.monotonic_ns >= first and a.monotonic_ns <= last]
    report['covered'] = bool(first is not None and points[0].monotonic_ns <= first <= last <= points[-1].monotonic_ns)
    report['max_gap_in_capture_ms'] = max(gaps)/1e6 if gaps else None
    report['accepted'] = bool(report['received_packets'] and report['covered'] and gaps and max(gaps) <= 500000000
                              and not any(report[k] for k in ('missing_total', 'negative_d1', 'negative_total', 'invalid_decomposition')))
    return report
