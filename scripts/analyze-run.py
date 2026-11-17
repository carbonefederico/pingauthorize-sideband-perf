#!/usr/bin/env python3
"""analyze-run.py — per-request fingerprint of one run's k6 metric streams.

Reads results/<run_id>/agent-*-metrics.json (k6 NDJSON) and answers, per
agent, where the latency lived: the measured-phase http_req_duration is
decomposed into waiting (server think time), connecting (TCP) and
tls_handshaking. A wall-clock span per agent and the dropped_iterations sum
sit beside them, so a generator-side problem (handshake storms, shedding)
is separable from a server-side one (high waiting with clean handshakes).

Usage: python3 scripts/analyze-run.py results/<run_id> [agent-index ...]
"""
import json
import sys
from array import array
from pathlib import Path

DUR = "http_req_duration"
SUBS = ["http_req_waiting", "http_req_connecting", "http_req_tls_handshaking",
        "http_req_sending", "http_req_receiving"]
TRACKED = {DUR: None, **{m: m for m in SUBS},
           "dropped_iterations": None, "sideband_decision_success": None}


def parse_time(ts):
    # k6 writes RFC3339 with nanoseconds and a Z suffix; %f parses at most 6
    # fraction digits, so normalize the fraction before parsing.
    from datetime import datetime, timezone
    ts = ts.rstrip("Z")
    whole, _, frac = ts.partition(".")
    frac = (frac + "000000")[:6]
    dt = datetime.strptime(f"{whole}.{frac}", "%Y-%m-%dT%H:%M:%S.%f")
    return dt.replace(tzinfo=timezone.utc).timestamp()


def pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def scan(path):
    """Stream one agent's NDJSON into per-metric measured-phase arrays."""
    series = {m: array("d") for m in [*SUBS, DUR]}
    per_scenario = {}  # scenario -> array("d") of durations
    dropped = 0.0
    ok = bad = 0
    first_ts = last_ts = None
    with open(path, "r", buffering=1 << 20) as fh:
        for line in fh:
            if "http_req" not in line and "dropped_iterations" not in line \
                    and "sideband_decision_success" not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            metric = ev.get("metric", "")
            if metric not in TRACKED:
                continue
            data = ev.get("data", {})
            tags = data.get("tags", {})
            if tags.get("phase") == "warmup":
                continue
            value = data.get("value")
            if value is None:
                continue
            ts = data.get("time")
            if ts:
                t = parse_time(ts)
                first_ts = t if first_ts is None else min(first_ts, t)
                last_ts = t if last_ts is None else max(last_ts, t)
            if metric in series:
                series[metric].append(value)
            if metric == DUR:
                per_scenario.setdefault(tags.get("scenario", "?"), array("d")).append(value)
            elif metric == "dropped_iterations":
                dropped += value
            elif metric == "sideband_decision_success":
                ok += 1 if value >= 0.5 else 0
                bad += 0 if value >= 0.5 else 1
    return series, per_scenario, dropped, ok, bad, first_ts, last_ts


def main():
    run_dir = Path(sys.argv[1])
    only = {int(a) for a in sys.argv[2:]}
    files = sorted(run_dir.glob("agent-*-metrics.json"))
    if not files:
        sys.exit(f"no agent-*-metrics.json under {run_dir}")
    print(f"{'agent':<8}{'n':>8}{'span':>7}{'avg':>7}{'p50':>7}{'p95':>7}{'p99':>7}"
          f"{'max':>8}{'wait95':>8}{'conn95':>8}{'tls95':>7}{'drop':>7}{'acc':>7}  (ms, measured phase)")
    for path in files:
        idx = int(path.name.split("-")[1])
        if only and idx not in only:
            continue
        series, per_scenario, dropped, ok, bad, t0, t1 = scan(path)
        dur = sorted(series[DUR])
        wait = sorted(series["http_req_waiting"])
        conn = sorted(series["http_req_connecting"])
        tls = sorted(series["http_req_tls_handshaking"])
        span = f"{t1 - t0:.0f}s" if t0 else "-"
        acc = f"{ok / (ok + bad):.4f}" if ok + bad else "-"
        print(f"{idx:<8}{len(dur):>8}{span:>7}"
              f"{sum(dur)/len(dur):>7.1f}{pct(dur,0.5):>7.1f}{pct(dur,0.95):>7.1f}"
              f"{pct(dur,0.99):>7.1f}{dur[-1]:>8.0f}"
              f"{pct(wait,0.95):>8.1f}{pct(conn,0.95):>8.1f}{pct(tls,0.95):>7.1f}"
              f"{dropped:>7.0f}{acc:>7}")
    # Scenario split for the slowest agent (highest duration p95) so the
    # policy-path view sits next to the fingerprint.
    worst, worst_p95 = None, -1.0
    for path in files:
        idx = int(path.name.split("-")[1])
        if only and idx not in only:
            continue
        series, per_scenario, *_ = scan(path)
        d = sorted(series[DUR])
        p95 = pct(d, 0.95) if d else -1.0
        if p95 > worst_p95:
            worst, worst_p95, worst_scen = idx, p95, per_scenario
    if worst is not None and worst_p95 > 0:
        print(f"\nScenario split for agent {worst} (slowest, p95 {worst_p95:.0f}ms):")
        print(f"  {'scenario':<14}{'n':>8}{'p50':>8}{'p95':>8}{'max':>9}")
        for name, vals in sorted(worst_scen.items()):
            v = sorted(vals)
            print(f"  {name:<14}{len(v):>8}{pct(v,0.5):>8.1f}{pct(v,0.95):>8.1f}{v[-1]:>9.0f}")


if __name__ == "__main__":
    main()
