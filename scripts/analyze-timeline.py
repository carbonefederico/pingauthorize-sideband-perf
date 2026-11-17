#!/usr/bin/env python3
"""analyze-timeline.py — 10-second bucket view of one run's k6 streams.

Answers: did slow agents degrade progressively or were they slow from the
start, and when did iteration shedding begin? Prints one matrix per metric:
rows are 10s buckets of measured-phase time, columns are agents.

Usage: python3 scripts/analyze-timeline.py results/<run_id> [metric...]
       metric: p50 (default) | p95 | drops
"""
import json
import sys
from array import array
from datetime import datetime, timezone
from pathlib import Path

BUCKET = 10.0


def parse_time(ts):
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
    """Per-agent: bucket index -> (durations, dropped)."""
    agents = {}  # idx -> {"t0": float, "dur": {b: array}, "drop": {b: float}}
    with open(path, "r", buffering=1 << 20) as fh:
        for line in fh:
            if "http_req_duration" not in line and "dropped_iterations" not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            metric = ev.get("metric", "")
            if metric not in ("http_req_duration", "dropped_iterations"):
                continue
            data = ev.get("data", {})
            if data.get("tags", {}).get("phase") == "warmup":
                continue
            value = data.get("value")
            if value is None:
                continue
            ts = data.get("time")
            if not ts:
                continue
            t = parse_time(ts)
            idx = int(path.name.split("-")[1])
            a = agents.setdefault(idx, {"t0": t, "dur": {}, "drop": {}})
            a["t0"] = min(a["t0"], t)
            b = int((t - a["t0"]) // BUCKET)
            if metric == "http_req_duration":
                a["dur"].setdefault(b, array("d")).append(value)
            else:
                a["drop"][b] = a["drop"].get(b, 0.0) + value
    return agents


def main():
    run_dir = Path(sys.argv[1])
    metrics = [m for m in sys.argv[2:]] or ["p50"]
    files = sorted(run_dir.glob("agent-*-metrics.json"))
    if not files:
        sys.exit(f"no agent-*-metrics.json under {run_dir}")
    merged = {}
    for path in files:
        for idx, a in scan(path).items():
            m = merged.setdefault(idx, {"t0": a["t0"], "dur": {}, "drop": {}})
            m["t0"] = min(m["t0"], a["t0"])
            for b, arr in a["dur"].items():
                m["dur"].setdefault(b, array("d")).extend(arr)
            for b, v in a["drop"].items():
                m["drop"][b] = m["drop"].get(b, 0.0) + v

    indices = sorted(merged)
    t0s = [merged[i]["t0"] for i in indices]
    t_ref = min(t0s)  # align agents on wall-clock, not per-agent start
    n_buckets = max(
        int((max(sum(len(v) for v in merged[i]["dur"].values()) and
                 (max((b for b in merged[i]["dur"]), default=0) * BUCKET)
                 for i in indices) or 0) // BUCKET) + 1 for i in indices
    )
    for metric in metrics:
        print(f"\n=== {metric} per 10s bucket (measured phase, wall-clock aligned) ===")
        header = f"{'t(s)':>6}" + "".join(f"{i:>8}" for i in indices)
        print(header)
        for b in range(n_buckets):
            row = f"{b * BUCKET:>6.0f}"
            any_data = False
            for i in indices:
                a = merged[i]
                rel_b = int(((t_ref + b * BUCKET) - a["t0"]) // BUCKET)
                if metric == "drops":
                    v = a["drop"].get(rel_b)
                    cell = f"{v:>8.0f}" if v else f"{'.':>8}"
                else:
                    arr = sorted(a["dur"].get(rel_b, ()))
                    if arr:
                        cell = f"{pct(arr, 0.5 if metric == 'p50' else 0.95):>8.0f}"
                    else:
                        cell = f"{'.':>8}"
                if cell.strip() != ".":
                    any_data = True
                row += cell
            if any_data:
                print(row)


if __name__ == "__main__":
    main()
