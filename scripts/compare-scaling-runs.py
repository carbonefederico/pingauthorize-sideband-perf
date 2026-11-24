#!/usr/bin/env python3
"""compare-scaling-runs.py — side-by-side table for the policy-tree scaling
campaign. Reads the runs' summary.json files (written by generate-report.py)
and prints one markdown table: per run, the pooled measured latency
percentiles, achieved rate, error and dropped-iteration counts, and PAZ CPU.
Standard library only; computes no verdicts — the campaign characterizes
performance, it applies no acceptance criteria.

Usage: python3 scripts/compare-scaling-runs.py results/<run_id> ...
"""
import argparse
import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent.parent / 'results'


def paz_cpu_mean(run):
    """Mean PAZ pod CPU (millicores) across the run's sampler samples."""
    samples = []
    for line in (run / 'usage-during-run.txt').read_text().splitlines():
        if line.startswith('paz:'):
            fields = line.strip().split()
            if len(fields) >= 3:
                cpu = fields[2]
                samples.append(float(cpu[:-1]) if cpu.endswith('m') else 1000 * float(cpu))
    return sum(samples) / len(samples) if samples else None


def row_for(run_path):
    summary = json.loads((run_path / 'summary.json').read_text())
    overall = summary['overall']
    latency = overall.get('latency') or {}
    cpu = [v['median_m'] for k, v in summary.get('cpu', {}).items() if 'pingauthorize' in k]
    return {
        'run': summary['run_id'],
        'branches': summary.get('branch_fan_out') or '1',
        'median_ms': latency.get('median_ms'),
        'p95_ms': latency.get('p95_ms'),
        'achieved_s': summary.get('average_requests_per_s'),
        'errors': overall.get('classified_errors'),
        'dropped': summary.get('dropped_iterations'),
        'paz_cpu_m': sum(cpu) / len(cpu) if cpu else paz_cpu_mean(run_path),
    }


def fmt(value, unit='', comma=False):
    if value is None:
        return 'unavailable'
    return f'{value:,.2f}{unit}' if comma else f'{value:.2f}{unit}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', help='Run IDs under results/, or run-directory paths')
    args = parser.parse_args()
    print('| Run | Branches | Median ms | p95 ms | Achieved /s | Errors | Dropped | PAZ CPU (m) |')
    print('|---|---|---:|---:|---:|---:|---:|---:|')
    for name in args.runs:
        run = Path(name)
        if not run.is_dir():
            run = RESULTS / name
        try:
            r = row_for(run)
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            print(f'| {name} | — | cannot read run: {exc} | | | | | |')
            continue
        print(f"| {r['run']} | {r['branches']} | {fmt(r['median_ms'])} | {fmt(r['p95_ms'])} | "
              f"{fmt(r['achieved_s'])} | {fmt(r['errors'])} | {fmt(r['dropped'])} | {fmt(r['paz_cpu_m'])} |")


if __name__ == '__main__':
    main()
