#!/usr/bin/env python3
"""Whole-run Sideband report. Standard library only; no log parsing or verdicts.
Reads env.json, agent-*-metrics.json[.gz], agent-*-summary.json and the run-local
usage-during-run.txt. Includes every phase. Writes report.html and summary.json.
"""
import argparse
from array import array
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
import gzip
import json
import math
from pathlib import Path
import re
from statistics import median

RESULTS = Path(__file__).resolve().parent.parent / 'results'
REPORTS = Path(__file__).resolve().parent.parent / 'reports'
TRACKED = {'http_req_duration', 'sideband_request_elapsed', 'sideband_decision_success',
           'sideband_errors', 'sideband_denials', 'http_req_failed', 'dropped_iterations'}


def seconds(value):
    parts = re.findall(r'(\d+(?:\.\d+)?)(ms|s|m|h)', str(value))
    if not parts or ''.join(a + b for a, b in parts) != str(value):
        raise ValueError(f'Invalid duration: {value!r}')
    return sum(float(a) * {'ms': .001, 's': 1, 'm': 60, 'h': 3600}[b] for a, b in parts)


def timestamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return dt.timestamp()


def stats(values):
    if not values:
        return None
    ordered = sorted(values)
    def pct(p):
        return ordered[max(0, math.ceil(p * len(ordered)) - 1)]
    return {'requests': len(ordered), 'avg_ms': sum(ordered) / len(ordered),
            'median_ms': pct(.5), 'p90_ms': pct(.9), 'p95_ms': pct(.95),
            'p99_ms': pct(.99), 'max_ms': ordered[-1]}


def group():
    return {'latency': array('d'), 'elapsed': array('d'), 'success_samples': 0,
            'correct': 0, 'errors': 0, 'denials': 0, 'http_samples': 0, 'http_failed': 0}


def read_stream(path, agent, groups, buckets, notes):
    first = last = None
    http_total = dropped = malformed = 0
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if event.get('type') != 'Point' or event.get('metric') not in TRACKED:
                    continue
                name, data = event['metric'], event['data']
                value = float(data['value'])
                if not math.isfinite(value) or value < 0:
                    raise ValueError('Invalid metric value')
                if name in {'sideband_decision_success', 'http_req_failed'} and value not in (0, 1):
                    raise ValueError('Rate sample must be zero or one')
                moment = timestamp(data['time'])
                if name == 'dropped_iterations':
                    dropped += value
                    continue
                scenario = data.get('tags', {}).get('scenario', '(untagged)')
                g = groups.setdefault((agent, scenario), group())
                if name == 'http_req_duration':
                    g['latency'].append(value)
                    buckets[math.floor(moment)].append(value)
                    first = moment if first is None else min(first, moment)
                    last = moment if last is None else max(last, moment)
                    http_total += 1
                elif name == 'sideband_request_elapsed':
                    g['elapsed'].append(value)
                elif name == 'sideband_decision_success':
                    g['success_samples'] += 1
                    g['correct'] += int(value)
                elif name == 'http_req_failed':
                    g['http_samples'] += 1
                    g['http_failed'] += int(value)
                elif name == 'sideband_errors':
                    g['errors'] += value
                elif name == 'sideband_denials':
                    g['denials'] += value
            except (ValueError, KeyError, TypeError, AttributeError):
                malformed += 1
    if malformed:
        notes.append(f'{agent}: {malformed} malformed records excluded')
    if http_total == 0:
        notes.append(f'{agent}: no request latency samples collected')
    return {'requests': http_total, 'first_response_utc': first, 'last_response_utc': last,
            'observed_response_span_s': last - first if first is not None else None,
            'dropped_iterations_observed': dropped, 'malformed_records': malformed}


def native_summary(path):
    metrics = json.loads(path.read_text())['metrics']
    def count(name):
        item = metrics.get(name)
        if item is None:
            return None
        return item.get('values', item).get('count')
    return {'requests': count('http_reqs'), 'iterations': count('iterations'),
            'dropped_iterations': count('dropped_iterations'),
            'classified_errors': count('sideband_errors')}


def memory_mib(value):
    for suffix, factor in [('Ki', 1 / 1024), ('Mi', 1), ('Gi', 1024)]:
        if value.endswith(suffix):
            return float(value[:-2]) * factor
    return None


def cpu_samples(path, date, bounds, notes):
    """Read the existing per-run kubectl-top sampler; clip to observed traffic."""
    pods = defaultdict(list)
    if not path.exists() or bounds is None:
        notes.append('CPU/memory samples unavailable for the observed traffic interval')
        return pods
    day = datetime.fromisoformat(date).date()
    current = previous = None
    try:
        for line in path.read_text().splitlines():
            if line.startswith('==='):
                clock = datetime.strptime(line.strip('= '), '%H:%M:%S').time()
                current = datetime.combine(day, clock, timezone.utc).timestamp()
                if previous is not None and current < previous:
                    day += timedelta(days=1)
                    current += 86400
                previous = current
                continue
            fields = re.sub(r'^(agents:|paz:)\s*', '', line.strip()).split()
            if current is None or len(fields) < 3 or not bounds[0] <= current <= bounds[1]:
                continue
            cpu = float(fields[1][:-1]) if fields[1].endswith('m') else 1000 * float(fields[1])
            pods[fields[0]].append((current, cpu, memory_mib(fields[2])))
    except ValueError as exc:
        notes.append(f'CPU sampler format error: {exc}')
    if not pods:
        notes.append('No CPU samples overlap the observed traffic interval')
    return pods


def merge(items):
    result = group()
    for item in items:
        for key in result:
            if key in ('latency', 'elapsed'):
                result[key].extend(item[key])
            else:
                result[key] += item[key]
    return result


def describe(g, errors_available):
    total = len(g['latency'])
    # Sparse error counters have no point on success. Interpret absence as zero
    # only for schema-2 collections made with the updated collector/workload.
    errors = g['errors'] if errors_available else None
    wrong = g['success_samples'] - g['correct']
    return {'latency': stats(g['latency']), 'elapsed': stats(g['elapsed']),
            'requests': total, 'success_samples': g['success_samples'],
            'expected_outcomes_pct': 100 * g['correct'] / g['success_samples'] if g['success_samples'] else None,
            'classified_errors': errors,
            'incorrect_decisions': max(0, wrong - errors) if errors is not None and g['success_samples'] else None,
            'http_failed': g['http_failed'] if g['http_samples'] else None,
            'http_samples': g['http_samples']}


def analyze(run):
    env = json.loads((run / 'env.json').read_text())
    count, rate = int(env['AGENTS']), float(env['RATE'])
    duration, ramp = seconds(env['DURATION']), seconds(env['WARMUP'])
    if count < 1 or rate <= 0 or duration <= 0 or ramp < 0:
        raise ValueError('AGENTS, RATE and DURATION must be positive')
    full_duration = ramp + duration
    notes, groups = [], {}
    buckets = defaultdict(lambda: array('d'))
    expected = {f'agent-{i}' for i in range(count)}
    paths = {p.name.removesuffix('.gz').removesuffix('-metrics.json'): p
             for p in sorted(run.glob('agent-*-metrics.json.gz'))}
    paths.update({p.name.removesuffix('-metrics.json'): p for p in run.glob('agent-*-metrics.json')})
    for extra in sorted(set(paths) - expected):
        notes.append(f'Unexpected stream excluded: {extra}')
    agents = {}
    for agent in sorted(expected):
        code = env.get('agent_exit_codes', {}).get(agent)
        item = {'exit_code': code, 'native_summary': None}
        if code is None:
            notes.append(f'{agent}: execution exit code unavailable')
        elif code != 0:
            notes.append(f'{agent}: exited with code {code}')
        summary_path = run / f'{agent}-summary.json'
        if summary_path.exists():
            try:
                item['native_summary'] = native_summary(summary_path)
            except (ValueError, KeyError, TypeError):
                notes.append(f'{agent}: structured k6 summary malformed')
        else:
            notes.append(f'{agent}: structured k6 summary unavailable')
        if agent in paths:
            item.update(read_stream(paths[agent], agent, groups, buckets, notes))
            if item['native_summary'] and item['native_summary']['requests'] != item['requests']:
                notes.append(f'{agent}: stream request count differs from structured summary')
        else:
            notes.append(f'{agent}: raw metric stream missing')
        agents[agent] = item
    errors_available = env.get('artifact_schema_version', 0) >= 2
    for (agent, scenario), g in groups.items():
        if g['success_samples'] != len(g['latency']):
            notes.append(f'{agent}/{scenario}: {g["success_samples"]} outcome samples for {len(g["latency"])} requests')
        if g['http_samples'] != len(g['latency']):
            notes.append(f'{agent}/{scenario}: HTTP failure sample count does not match requests')
        if len(g['elapsed']) != len(g['latency']):
            notes.append(f'{agent}/{scenario}: full-call elapsed coverage incomplete')
    for agent in agents:
        agents[agent]['statistics'] = describe(merge(g for (a, _), g in groups.items() if a == agent), errors_available)
    scenarios = {name: describe(merge(g for (_, s), g in groups.items() if s == name), errors_available)
                 for name in sorted({s for _, s in groups})}
    overall = describe(merge(groups.values()), errors_available)
    bounds = (min(buckets), max(buckets) + 1) if buckets else None
    date = (env.get('timestamp_utc') or (datetime.fromtimestamp(bounds[0], timezone.utc).isoformat() if bounds else '1970-01-01'))[:10]
    pods = cpu_samples(run / 'usage-during-run.txt', date, bounds, notes)
    cpu = {pod: {'median_m': median(p[1] for p in points), 'samples': len(points),
                 'peak_memory_mib': max((p[2] for p in points if p[2] is not None), default=None)}
           for pod, points in pods.items()}
    # Native summaries alone supply native aggregate counts; never substitute logs.
    natives = [item['native_summary'] for item in agents.values()]
    dropped = sum((item['dropped_iterations'] or 0) for item in natives) if all(item is not None for item in natives) else None
    result = {'run_id': run.name, 'window': 'whole run, including ramp/warmup',
              'configured_duration_s': full_duration, 'configured_ramp_s': ramp,
              'plateau_target_per_s': rate, 'expected_requests_for_linear_ramp': rate * (duration + ramp / 2),
              'requests_observed': overall['requests'], 'average_requests_per_s': overall['requests'] / full_duration,
              'dropped_iterations': dropped, 'interrupted_iterations': None,
              'overall': overall, 'agents': agents, 'scenarios': scenarios, 'cpu': cpu,
              'cpu_source': 'usage-during-run.txt', 'limitations': notes}
    return result, buckets, pods


def fmt(value, unit=''):
    return 'unavailable' if value is None else f'{value:,.2f}{unit}'


def table(headers, rows):
    return '<table><tr>' + ''.join(f'<th>{escape(h)}</th>' for h in headers) + '</tr>' + ''.join(
        '<tr>' + ''.join(f'<td>{escape(str(v))}</td>' for v in row) + '</tr>' for row in rows) + '</table>'


def chart(title, series, unit):
    points = [p for _, values in series for p in values]
    if not points:
        return f'<h3>{escape(title)}</h3><p>Unavailable</p>'
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymax = max(p[1] for p in points) or 1
    colors = ['#2563eb', '#dc2626', '#059669', '#9333ea', '#ca8a04']
    lines = []
    for i, (label, values) in enumerate(series):
        coords = ' '.join(f'{50 + 830*(x-xmin)/max(1,xmax-xmin):.1f},{195-165*y/ymax:.1f}' for x,y in values)
        color = colors[i % len(colors)]
        lines.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="1.5"/>')
    labels = ' · '.join(escape(label) for label, _ in series)
    ticks = ''.join(f'<text x="4" y="{195-165*i/4:.0f}">{ymax*i/4:.1f}</text>' for i in range(5))
    return (f'<h3>{escape(title)} ({escape(unit)})</h3><svg viewBox="0 0 900 230" role="img" aria-label="{escape(title)}">'
            f'<path d="M50 20V195H890" fill="none" stroke="#aaa"/>{ticks}{"".join(lines)}'
            f'<text x="50" y="220">{xmin:.0f}s</text><text x="790" y="220">{xmax:.0f}s</text></svg><p>{labels}</p>')


def render(result, buckets, pods):
    overall = result['overall']
    populations = [overall] + list(result['scenarios'].values()) + [g['statistics'] for g in result['agents'].values()]
    show_errors = any(g['classified_errors'] is not None for g in populations)
    show_wrong = any(g['incorrect_decisions'] is not None for g in populations)
    headers = ['Population', 'Requests', 'Average ms', 'Median ms', 'p90 ms', 'p95 ms', 'p99 ms', 'Max ms', 'Expected outcomes']
    if show_errors:
        headers.append('Integration errors')
    if show_wrong:
        headers.append('Incorrect decisions')
    def row(name, g):
        values = g['latency'] or {}
        cells = [name, f"{g['requests']:,}"] + [fmt(values.get(k)) for k in ['avg_ms','median_ms','p90_ms','p95_ms','p99_ms','max_ms']]
        cells.append(fmt(g['expected_outcomes_pct'], '%'))
        if show_errors:
            cells.append(fmt(g['classified_errors']))
        if show_wrong:
            cells.append(fmt(g['incorrect_decisions']))
        return cells
    def pod_label(name):
        if '-loadtest-' in name:
            tail = name.split('-loadtest-', 1)[1].split('-')
            return f'Generator {tail[1]}' if len(tail) > 1 else 'Generator'
        return f'PAZ {name.rsplit("-", 1)[-1]}' if 'pingauthorize' in name else name
    top_headers = ['Requests', 'Duration including ramp', 'Whole-run average', 'Plateau target']
    top_values = [f"{result['requests_observed']:,}", fmt(result['configured_duration_s'], ' s'),
                  fmt(result['average_requests_per_s'], '/s'), fmt(result['plateau_target_per_s'], '/s')]
    if result['dropped_iterations'] is not None:
        top_headers.append('Dropped iterations')
        top_values.append(fmt(result['dropped_iterations']))
    html = ['<!doctype html><meta charset="utf-8"><title>Sideband performance</title>',
            '<style>body{font:15px system-ui;max-width:1150px;margin:32px auto;padding:0 20px;color:#172033}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{border:1px solid #ddd;padding:7px;text-align:right}td:first-child,th:first-child{text-align:left}svg{width:100%;max-height:270px}text{font-size:11px;fill:#555}h2{margin-top:32px}.notes{background:#f4f6f8;padding:12px 18px;margin-top:24px}</style>',
            f'<h1>PingAuthorize Sideband — {escape(result["run_id"])}</h1>',
            table(top_headers, [top_values]),
            '<p>Results include the ramp. The whole-run average is therefore lower than the plateau target.</p>',
            '<h2>Latency and outcomes</h2>', table(headers, [row('All agents', overall)])]
    if buckets:
        origin = min(buckets)
        html.append(chart('Throughput', [('Responses/s', [(t-origin,len(buckets.get(t, []))) for t in range(origin,max(buckets)+1)])], 'requests/s'))
        html.append(chart('Latency over time', [(label,[(t-origin,stats(v)[key]) for t,v in sorted(buckets.items())])
                    for label,key in [('Average','avg_ms'),('p90','p90_ms')]], 'ms'))
    else:
        origin = 0
    if overall['elapsed'] is not None:
        e = overall['elapsed']
        html.extend(['<h2>Full-call elapsed latency</h2>',table(['Average ms','p95 ms','p99 ms','Samples'],
                    [[fmt(e[k]) for k in ['avg_ms','p95_ms','p99_ms','requests']]])])
    html.extend(['<h2>Scenarios</h2>', table(headers,[row(name,g) for name,g in result['scenarios'].items()]),
                 '<h2>Generators</h2>', table(headers,[row(name,g['statistics']) for name,g in result['agents'].items()])])
    if result['cpu']:
        html.extend(['<h2>CPU and memory</h2>',table(['Pod','Median CPU (m)','Peak sampled memory (MiB)'],
                    [[pod_label(pod),fmt(g['median_m']),fmt(g['peak_memory_mib'])] for pod,g in result['cpu'].items()])])
        for title, is_generator in [('PAZ CPU',False),('Generator CPU',True)]:
            lines=[(pod_label(p),[(t-origin,c) for t,c,_ in pts]) for p,pts in sorted(pods.items())
                   if ('-loadtest-' in p) == is_generator]
            if lines:
                html.append(chart(title,lines,'millicores'))
    # Consolidate absent optional instrumentation. Preserve real discrepancies.
    absent, warnings = set(), []
    for note in result['limitations']:
        if 'structured k6 summary unavailable' in note:
            absent.add('structured k6 summaries (including dropped-iteration totals)')
        elif 'execution exit code unavailable' in note:
            absent.add('execution exit codes')
        elif 'full-call elapsed coverage incomplete' in note and overall['elapsed'] is None:
            absent.add('full-call elapsed time')
        else:
            warnings.append(note)
    if not show_errors:
        absent.add('separate integration-error counts')
    notes = ['Whole run, including ramp. HTTP latency excludes connection setup. Expected outcomes reflect the workload script’s checks. CPU is the median across samples during observed traffic; memory is the sampled peak.']
    if absent:
        notes.append('Not recorded in this run; omitted: ' + '; '.join(sorted(absent)) + '.')
    codes = [g['exit_code'] for g in result['agents'].values() if g['exit_code'] is not None]
    if codes:
        notes.append(f'Execution records: {len(codes)} of {len(result["agents"])} generators; nonzero exit codes: {sum(c != 0 for c in codes)}.')
    html.extend(['<div class="notes"><b>Notes</b><ul>' + ''.join('<li>'+escape(n)+'</li>' for n in notes)+'</ul></div>'])
    if warnings:
        html.append('<h2>Collection limitations</h2><ul>'+''.join('<li>'+escape(n)+'</li>' for n in warnings)+'</ul>')
    return '\n'.join(html)


def generate(run):
    result, buckets, pods = analyze(Path(run))
    html = render(result, buckets, pods)
    (Path(run) / 'report.html').write_text(html, encoding='utf-8')
    (Path(run) / 'summary.json').write_text(json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')
    # The published copy lives in reports/; keep it in step with the run directory.
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f'{result["run_id"]}.html').write_text(html, encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', help='Run ID under results/, or a run-directory path')
    args = parser.parse_args()
    run = Path(args.run)
    if not run.is_dir():
        run = RESULTS / args.run
    try:
        generate(run)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f'Cannot generate report: {exc}\n')
    print(f'Written: {run / "report.html"}, {run / "summary.json"}, reports/{run.name}.html')


if __name__ == '__main__':
    main()
