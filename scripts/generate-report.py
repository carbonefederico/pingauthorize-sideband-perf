#!/usr/bin/env python3
"""Generate a self-contained HTML performance report for a PAZ sideband test run.

Page 1 aggregates the whole run: throughput over time, response time (avg+p90)
over time, and PAZ pod CPU over time — all on one shared time axis so a vertical
line crosses the same moment in every chart. Page 2 holds per-agent details.

Inputs (under results/<run>/):
  agent-<i>-metrics.json  k6 streaming NDJSON (per-request latency, http_reqs)
  agent-<i>.log           k6 end-of-run summaries
  ../pod-usage-<UTC>.csv  PAZ pod CPU/memory samples from scripts/monitor.sh
Outputs:
  results/<run>/report.html  one file, no external resources
"""
import gzip
import io
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

RESULTS = Path(__file__).resolve().parent.parent / "results"
THRESHOLD_P95_MS = 500.0


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def parse_k6_summary(path):
    """End-of-run summary metrics from one k6 agent log."""
    text = path.read_text(errors="replace")
    out = {"agent": path.stem}
    patterns = {
        "reqs": r"http_reqs\.+: +(\d+) +([\d.]+)/s",
        "success": r"sideband_decision_success\.+: +([\d.]+)% +(\d+) out of (\d+)",
        # k6 prints durations >= 1s with an "s" suffix ("1.19s"), < 1s as ms,
        # and sub-ms values as microseconds ("973.43µs" — agent-5 of the
        # 2026-11-16 stage-1500 run was silently dropped without µs support).
        "latency": (r"sideband_decision_latency\.+: +avg=([\d.]+)(m?s|µs) +min=([\d.]+)(m?s|µs)"
                    r" +med=([\d.]+)(m?s|µs) +max=([\d.]+)(m?s|µs) +p\(90\)=([\d.]+)(m?s|µs) +p\(95\)=([\d.]+)(m?s|µs)"),
        # per-agent dropped iterations (load shedding by the VU cap)
        "dropped": r"dropped_iterations\.+: +(\d+)",
        # the configured scenario rate, from the scenario description line
        "target_rate": r"sideband_decision: +([\d.]+) +iterations/s",
        # the warmup duration, from the warmup scenario line ("30s")
        "warmup_cfg": r"warmup: +[\d.]+ +iterations/s for +([\d.]+[sm])",
        # the measured-scenario configured duration, from its progress line ("5m0s")
        "duration_cfg": r"sideband_decision ✓ +\[ +100% \] +\d+/\d+ VUs +([\d.]+[smh])",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            if key in ("warmup_cfg", "duration_cfg"):  # duration strings like "30s" / "5m0s"
                out[key] = list(m.groups())
            elif key == "latency":
                # interleave value/unit groups; normalize s and µs to ms
                vals = []
                g = m.groups()
                for i in range(0, len(g), 2):
                    n = float(g[i])
                    if g[i + 1] == "s":
                        n *= 1000.0
                    elif g[i + 1] == "µs":
                        n /= 1000.0
                    vals.append(n)
                out[key] = vals
            else:
                out[key] = [float(g) for g in m.groups()]
    return out


def parse_k6_metrics_json(path):
    """Parse k6's streaming NDJSON into per-second series.

    Returns dict with:
      dur_avg_s, dur_p90_s, dur_p95_s: [(t_sec, value_ms), ...]
      req_rate: [(t_sec, reqs_per_s), ...]  (derivative of cumulative count)
      total_requests: int
      t0: first absolute timestamp (str)
    """
    if path.stat().st_size > 512 * 1024 * 1024:
        print(f"warning: {path} unexpectedly large; skipping", file=sys.stderr)
        return None
    durations = []
    elapsed = []  # sideband_request_elapsed (custom Trend, full call incl. connect)
    subtimings = {}  # metric -> [(ts, ms)] for connecting/tls_handshaking/waiting/sending/receiving
    warmup_points = 0
    phaseless_points = 0
    scenario_lat = {}  # scenario -> [(ts, ms)] measured-window per-request latency
    scenario_success = {}  # scenario -> [ok_count, total] from the success Rate
    scenario_errors = {}  # scenario -> error count from sideband_errors
    sub_metric_names = {"http_req_connecting", "http_req_tls_handshaking",
                        "http_req_waiting", "http_req_sending", "http_req_receiving"}
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or '"Point"' not in line.replace(" ", ""):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "Point":
                continue
            metric = ev.get("metric", "")
            data = ev.get("data", {})
            tags = data.get("tags", {})
            # Measured-window extraction requires an EXPLICIT phase tag
            # (2026-11-17 follow-up review, additional issue): a Point with
            # no phase is ambiguous — counting it as measured would let
            # untagged traffic into the measured window. Points without a
            # phase are counted and reported as evidence of a format drift,
            # not silently merged.
            phase = tags.get("phase")
            if metric in ("http_req_duration", "sideband_request_elapsed",
                          "sideband_decision_success", "sideband_errors"):
                if phase is None:
                    phaseless_points += 1
                    continue
            if phase == "warmup":
                if metric in ("http_req_duration", "sideband_request_elapsed"):
                    warmup_points += 1
                continue
            scenario = tags.get("scenario", "")
            value = data.get("value", 0)
            t = data.get("time", "")
            try:
                ts = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except ValueError:
                continue
            if metric == "http_req_duration":
                durations.append((ts, float(value)))
                if scenario:
                    scenario_lat.setdefault(scenario, []).append((ts, float(value)))
            elif metric == "sideband_request_elapsed":
                elapsed.append((ts, float(value)))
            elif metric == "sideband_decision_success":
                st = scenario_success.setdefault(scenario, [0, 0])
                st[1] += 1
                if value:
                    st[0] += 1
            elif metric == "sideband_errors":
                scenario_errors[scenario] = scenario_errors.get(scenario, 0) + int(value)
            elif metric in sub_metric_names:
                subtimings.setdefault(metric, []).append((ts, float(value)))
    if not durations:
        return None
    durations.sort(key=lambda x: x[0])
    t0 = durations[0][0]

    # Bucket per second: avg + p90 over the requests in that second.
    buckets = {}
    for ts, ms in durations:
        sec = int((ts - t0).total_seconds())
        buckets.setdefault(sec, []).append(ms)
    dur_avg, dur_p90, dur_p95 = [], [], []
    for sec in sorted(buckets):
        vals = sorted(buckets[sec])
        n = len(vals)
        dur_avg.append((sec, sum(vals) / n))
        dur_p90.append((sec, vals[min(n - 1, int(math.ceil(0.90 * n)) - 1)]))
        dur_p95.append((sec, vals[min(n - 1, int(math.ceil(0.95 * n)) - 1)]))

    # Throughput: raw requests per second (count per bucket).
    req_rate = [(sec, len(buckets[sec])) for sec in sorted(buckets)]
    # Sub-timings (connecting / tls_handshaking / waiting / sending /
    # receiving): RAW per-second values kept for exact weighted pooling —
    # the old per-agent pre-meaned series made pooled_subtiming_means an
    # unweighted mean of agent means (follow-up review, additional issue).
    # Elapsed (full call incl. connect): same treatment as durations.
    elapsed.sort(key=lambda x: x[0])
    elapsed_buckets = {}
    for ts, ms in elapsed:
        elapsed_buckets.setdefault(int((ts - t0).total_seconds()), []).append(ms)
    sub_raw = {}
    for name, pts in subtimings.items():
        for ts, ms in pts:
            sub_raw.setdefault(name, {}).setdefault(
                int((ts - t0).total_seconds()), []).append(ms)
    # Per-scenario measured-window evidence, for the report's per-scenario
    # gate evaluation (follow-up review, finding 5).
    scen = {}
    for name, pts in scenario_lat.items():
        pts.sort()
        vals = sorted(ms for _, ms in pts)
        p95 = vals[min(len(vals) - 1, int(math.ceil(0.95 * len(vals))) - 1)] if vals else None
        scen[name] = {"latency_points": pts, "p95_ms": p95, "requests": len(vals)}
    for name, (ok, tot) in scenario_success.items():
        scen.setdefault(name, {})["success_ok"], scen[name]["success_total"] = ok, tot
    for name, errs in scenario_errors.items():
        scen.setdefault(name, {})["errors"] = errs
    return {
        "dur_avg_s": dur_avg,
        "dur_p90_s": dur_p90,
        "dur_p95_s": dur_p95,
        "req_rate": req_rate,
        # Raw per-second values, for exact pooling across agents on page 1.
        "buckets_ms": buckets,
        "elapsed_buckets_ms": elapsed_buckets,
        "subtimings_raw": sub_raw,
        "subtimings": sub_raw,
        "scenarios": scen,
        "phaseless_points": phaseless_points,
        "total_requests": len(durations),
        "total_elapsed": len(elapsed),
        "warmup_points": warmup_points,
        "t0": t0,
    }


def parse_k6_progress_lines(path):
    """Fallback: derive cumulative->per-second iteration counts from the k6
    progress lines ("running (0m03.0s), 01/25 VUs, 149 complete ...").
    Returns [(sec, cumulative_iters)] or []."""
    pts = []
    for m in re.finditer(r"running \((\d+)m([\d.]+)s\), \d+/\d+ VUs, (\d+) complete", path.read_text(errors="replace")):
        sec = int(m.group(1)) * 60 + float(m.group(2))
        pts.append((sec, int(m.group(3))))
    pts.sort()
    return pts


def pooled_percentile(series, q=0.95):
    """True pooled percentile (ms) over every measured-phase raw datapoint in
    all agents' per-second buckets — recomputable by a reviewer from the
    raw NDJSON. Returns None when no datapoints exist."""
    all_ms = [ms for s in series.values() for vals in s["buckets_ms"].values() for ms in vals]
    if not all_ms:
        return None
    all_ms.sort()
    return all_ms[min(len(all_ms) - 1, int(math.ceil(q * len(all_ms))) - 1)]


def pooled_latency_stats(series):
    """The README-table column set, all from ONE window: every measured-phase
    raw datapoint across all agents, pooled. avg / median / p90 / p95 / p99.
    The k6 end-of-run latency line is the warmup-inclusive aggregate and the
    {scenario:...} sub-line prints no p99 — this function is the single
    window-consistent source for every reported latency column."""
    all_ms = [ms for s in series.values() for vals in s["buckets_ms"].values() for ms in vals]
    if not all_ms:
        return None
    all_ms.sort()
    n = len(all_ms)

    def pct(q):
        return all_ms[min(n - 1, int(math.ceil(q * n)) - 1)]

    return {
        "avg_ms": sum(all_ms) / n,
        "median_ms": pct(0.50),
        "p90_ms": pct(0.90),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
        "max_ms": all_ms[-1],
        "requests": n,
    }


def pooled_subtiming_means(series):
    """Pooled per-second mean of each k6 sub-timing (connecting, tls_handshaking,
    waiting, sending, receiving) across all agents: {metric: [(sec, ms)]}.
    Pools RAW per-request values (each agent's series carries raw points per
    second since the follow-up review fix), so the per-second figure is a
    request-weighted mean — not an unweighted mean of agent means."""
    pooled = {}
    for s in series.values():
        for name, per_sec in s.get("subtimings", {}).items():
            d = pooled.setdefault(name, {})
            # subtimings now carries RAW values bucketed per second:
            # {sec: [ms, ...]} — pool every request's value, so the result
            # is a request-weighted mean rather than a mean of agent means.
            for sec, vals in per_sec.items():
                d.setdefault(sec, []).extend(vals)
    return {name: [(sec, sum(v) / len(v)) for sec, v in sorted(d.items())]
            for name, d in pooled.items()}


def parse_usage_sampler(path, date_utc="1970-01-01"):
    """Parse run-in-cluster.sh's usage-during-run.txt (the in-run kubectl top
    sampler) into the same shape parse_usage_csv returns, so summary CPU
    stats work for runs made without scripts/monitor.sh running. Format:
    '=== HH:MM:SS ===' headers (UTC wall clock of the run day), then
    'agents:'/'paz:' lines of 'podname  <cpu>m  <mem>Mi'. The caller passes
    the run's date (from env.json's timestamp) so timestamps land in the
    same absolute frame as the k6 streams — the report charts subtract the
    stream t0, and a 1970 frame would put the samples 56 years away from
    it. Returns {name: [(ts, cpu_m, mem_or_pct, kind), ...]}."""
    series = {}
    ts = None
    for line in open(path, errors="replace"):
        line = line.strip()
        if line.startswith("==="):
            hh, mm, ss = (line.strip("= ").split(":") + ["0", "0"])[:3]
            ts = f"{date_utc}T{int(hh):02d}:{int(mm):02d}:{int(ss):02d}Z"
            continue
        # The sampler prefixes each section's first pod line with
        # 'agents:'/'paz:' — strip the label, the pod data follows it on
        # the same line. Blank placeholder lines ('agents: <none>') have no
        # millisecond field and fall through the CPU check below.
        line = re.sub(r"^(agents:|paz:)\s*", "", line)
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or not parts[1].endswith("m"):
            continue
        try:
            cpu_m = int(parts[1].rstrip("m"))
        except ValueError:
            continue
        series.setdefault(parts[0], []).append((ts, cpu_m, 0, "pod"))
    return series


def parse_usage_csv(path):
    """Parse the monitor CSV into per-name series.

    Supports both layouts:
      pod-only (legacy):  timestamp,pod,cpu,memory
      current:            timestamp,kind,name,cpu,memory_or_pct   (kind pod|node)

    Returns {name: [(ts, cpu_m, mem_or_pct, kind), ...]}.
    """
    series = {}
    with open(path, errors="replace") as f:
        header = f.readline()
        has_kind = header.strip().split(",")[1:2] == ["kind"]
        for line in f:
            parts = line.strip().split(",")
            if has_kind:
                if len(parts) != 5:
                    continue
                ts, kind, name, cpu, last = parts
            else:
                if len(parts) != 4:
                    continue
                ts, name, cpu, last = parts
                kind = "pod"
            cpu_m = int(cpu.rstrip("m")) if cpu.endswith("m") else 0
            if last.endswith("Mi"):
                last_v = int(last[:-2])
            elif last.endswith("Gi"):
                last_v = int(float(last[:-2]) * 1024)
            elif last.endswith("%"):  # node CPU percentage
                last_v = float(last[:-1])
            else:
                last_v = 0
            series.setdefault(name, []).append((ts, cpu_m, last_v, kind))
    return series


# --------------------------------------------------------------------------
# series helpers
# --------------------------------------------------------------------------

def moving_average(points, window):
    """points: [(t, v)] sorted by t -> centered moving average of v over `window` samples."""
    if not points or window <= 1:
        return points
    out = []
    half = window // 2
    for i, (t, _) in enumerate(points):
        lo, hi = max(0, i - half), min(len(points), i + half + 1)
        out.append((t, sum(v for _, v in points[lo:hi]) / (hi - lo)))
    return out


def to_seconds(points, t0):
    """[(datetime|str, v)] -> [(float seconds since t0, v)]."""
    out = []
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    for t, v in points:
        if isinstance(t, str):
            t = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        out.append(((t - t0).total_seconds(), v))
    return out


# --------------------------------------------------------------------------
# chart rendering (inline SVG, no JS)
# --------------------------------------------------------------------------

PALETTE = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed"]


def line_chart(title, series_list, y_unit, y_max=None, height=200, width=860,
               y_label_every=3, x_grid_every=30, area_fill=False):
    """series_list: [(name, color, [(x, y), ...])]. Shared x axis in seconds."""
    all_pts = [(x, y) for _, _, pts in series_list for x, y in pts]
    if not all_pts:
        return f"<p class='muted'>No data for {escape(title)}.</p>"
    x_max = max(x for x, _ in all_pts) or 1
    y_max = y_max or max(y for _, y in all_pts) * 1.15 or 1
    pad_l, pad_r, pad_t, pad_b = 48, 14, 14, 28
    w = width - pad_l - pad_r
    h = height - pad_t - pad_b

    def X(x):
        return pad_l + x / x_max * w

    def Y(y):
        return pad_t + h - min(1.0, y / y_max) * h

    paths = []
    for si, (name, color, pts) in enumerate(series_list):
        if len(pts) < 2:
            continue
        d = "M" + " L".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in pts)
        if area_fill and len(series_list) == 1:
            d += f" L{X(pts[-1][0]):.1f},{Y(0):.1f} L{X(pts[0][0]):.1f},{Y(0):.1f} Z"
            gid = f"grad{si}"
            defs = (f'<linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
                    f'<stop offset="0%" stop-color="{color}" stop-opacity="0.25"/>'
                    f'<stop offset="100%" stop-color="{color}" stop-opacity="0.02"/></linearGradient>')
            paths.append(f'<defs>{defs}</defs><path d="{d}" fill="url(#{gid})" stroke="{color}" stroke-width="1.8"/>')
        else:
            paths.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.8" '
                         f'stroke-linecap="round" stroke-linejoin="round"/>')

    grid = []
    n_grid = 4
    for i in range(n_grid + 1):
        yv = y_max * i / n_grid
        grid.append(
            f'<line x1="{pad_l}" y1="{Y(yv):.1f}" x2="{width - pad_r}" y2="{Y(yv):.1f}" '
            f'stroke="currentColor" stroke-opacity="0.07"/>'
            f'<text x="{pad_l - 8}" y="{Y(yv) + 3:.1f}" text-anchor="end" class="tick">{yv:.0f}</text>'
        )
    xticks = []
    t = 0
    while t <= x_max:
        mm, ss = divmod(int(t), 60)
        xticks.append(
            f'<line x1="{X(t):.1f}" y1="{pad_t}" x2="{X(t):.1f}" y2="{pad_t + h}" '
            f'stroke="currentColor" stroke-opacity="0.05"/>'
            f'<text x="{X(t):.1f}" y="{height - 8}" text-anchor="middle" class="tick">{mm}:{ss:02d}</text>'
        )
        t += x_grid_every

    legend = "".join(
        f'<span class="lg"><i style="background:{color}"></i>{escape(name)}</span>'
        for name, color, _ in series_list
    )
    return f"""
<figure class="chart">
  <figcaption>{escape(title)} <span class="unit">{escape(y_unit)}</span></figcaption>
  <svg viewBox="0 0 {width} {height}" class="svg" role="img" aria-label="{escape(title)}">
    {''.join(grid)}{''.join(xticks)}{''.join(paths)}
  </svg>
  <div class="legend">{legend}</div>
</figure>"""


def stacked_area_chart(title, series_list, y_unit, height=200, width=860):
    """series_list stacked bottom-up; shared x axis in seconds. Readable CPU chart."""
    all_pts = [(x, y) for _, _, pts in series_list for x, y in pts]
    if not all_pts:
        return f"<p class='muted'>No data for {escape(title)}.</p>"
    x_max = max(x for x, _ in all_pts) or 1
    total_max = max(
        sum(y for _, _, pts in series_list for x2, y in pts if abs(x2 - x) < 1)
        for x, _ in all_pts
    )
    y_max = total_max * 1.15 or 1
    pad_l, pad_r, pad_t, pad_b = 48, 14, 14, 28
    w = width - pad_l - pad_r
    h = height - pad_t - pad_b

    def X(x):
        return pad_l + x / x_max * w

    def Y(y):
        return pad_t + h - min(1.0, y / y_max) * h

    # interpolate every series onto a common second grid, then stack
    grid = list(range(0, int(x_max) + 1))

    def interp(pts):
        d = dict((int(x), y) for x, y in pts)
        return [d.get(g, d[max(k for k in d if k <= g)] if any(k <= g for k in d)
                      else d[min(k for k in d)]) for g in grid]

    stacked = []
    cum = [0.0] * len(grid)
    areas = []
    for name, color, pts in series_list:
        vals = interp(pts)
        top = [c + v for c, v in zip(cum, vals)]
        d = f"M{X(grid[0]):.1f},{Y(cum[0]):.1f} " + " L".join(
            f"{X(g):.1f},{Y(t):.1f}" for g, t in zip(grid, top))
        d += " L" + " L".join(f"{X(g):.1f},{Y(c):.1f}" for g, c in
                              zip(reversed(grid), reversed(cum))) + " Z"
        areas.append(f'<path d="{d}" fill="{color}" fill-opacity="0.35" stroke="{color}" stroke-width="1"/>')
        cum = top

    gridl = []
    for i in range(5):
        yv = y_max * i / 4
        gridl.append(
            f'<line x1="{pad_l}" y1="{Y(yv):.1f}" x2="{width - pad_r}" y2="{Y(yv):.1f}" '
            f'stroke="currentColor" stroke-opacity="0.08"/>'
            f'<text x="{pad_l - 6}" y="{Y(yv) + 3:.1f}" text-anchor="end" class="tick">{yv:.0f}</text>'
        )
    xticks = []
    t = 0
    while t <= x_max:
        mm, ss = divmod(int(t), 60)
        xticks.append(
            f'<line x1="{X(t):.1f}" y1="{pad_t}" x2="{X(t):.1f}" y2="{pad_t + h}" '
            f'stroke="currentColor" stroke-opacity="0.06"/>'
            f'<text x="{X(t):.1f}" y="{height - 8}" text-anchor="middle" class="tick">{mm}:{ss:02d}</text>'
        )
        t += 30
    legend = "".join(
        f'<span class="lg"><i style="background:{color}"></i>{escape(name)}</span>'
        for name, color, _ in series_list
    )
    return f"""
<figure class="chart">
  <figcaption>{escape(title)} <span class="unit">({escape(y_unit)}, stacked)</span></figcaption>
  <svg viewBox="0 0 {width} {height}" class="svg" role="img" aria-label="{escape(title)}">
    {''.join(gridl)}{''.join(xticks)}{''.join(areas)}
    <line x1="{pad_l}" y1="{pad_t + h}" x2="{width - pad_r}" y2="{pad_t + h}" stroke="currentColor" stroke-opacity="0.3"/>
    <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + h}" stroke="currentColor" stroke-opacity="0.3"/>
  </svg>
  <div class="legend">{legend}</div>
</figure>"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        runs = sorted((p for p in RESULTS.iterdir() if p.is_dir()), reverse=True)
        if runs:
            sys.argv.append(runs[0].name)
            print(f"No RUN given; using newest: {runs[0].name}")
        else:
            print(__doc__)
            sys.exit(1)

    run_dir = RESULTS / sys.argv[1]
    # Per-run metadata captured at test launch (env.json, written by
    # run-in-cluster.sh): resolved envs, profile, thresholds. Thresholds
    # govern the verdict — the same values k6 gated on.
    env_meta = {}
    env_json = run_dir / "env.json"
    if env_json.is_file():
        env_meta = json.loads(env_json.read_text())
    if not run_dir.is_dir():
        print(f"Run not found: {run_dir}", file=sys.stderr)
        sys.exit(1)

    summaries = [parse_k6_summary(p) for p in sorted(run_dir.glob("agent-*.log"))
                 if not p.name.endswith("-metrics.json")]
    summaries = [s for s in summaries if "latency" in s]

    metric_files = sorted(run_dir.glob("agent-*-metrics.json"))
    fixture_warning = False
    stream_truncated = False
    stream_gaps = []
    series = {}
    for mf in metric_files:
        parsed = parse_k6_metrics_json(mf)
        if parsed:
            agent = mf.stem.replace("-metrics", "")
            series[agent] = parsed
            summ = next((s for s in summaries if s["agent"] == agent), None)
            if summ and "reqs" in summ:
                gap = 1 - parsed["total_requests"] / max(1, summ["reqs"][0])
                if gap > 0.5:
                    fixture_warning = True  # counts wildly off: likely synthetic data
                elif gap > 0.02:
                    # Streams are snapshotted while pods run, so the last few
                    # seconds of each agent's stream can be missing.
                    stream_truncated = True

    # Monitoring CSVs are per-RUN artifacts: monitor.sh writes
    # results/<run_id>/pod-usage-<run_id>.csv. Prefer the run's own file;
    # the global results/ glob is a legacy fallback and, when used, is
    # flagged in the summary (the review caught reports pulling whichever
    # CSV was newest regardless of run association).
    usage_files = sorted((run_dir).glob("pod-usage-*.csv")) or sorted(RESULTS.glob("pod-usage-*.csv"))
    usage_source = str(usage_files[-1]) if usage_files else None
    usage_from_run_dir = bool(sorted(run_dir.glob("pod-usage-*.csv")))
    usage = parse_usage_csv(usage_files[-1]) if usage_files else {}
    paz_series = {p: s for p, s in usage.items() if "pingauthorize" in p and "pap" not in p and s[0][3] == "pod"}
    node_series = {p: s for p, s in usage.items() if s[0][3] == "node"}
    # Loadtest agent pods from the same CSV (the in-run sampler in
    # run-in-cluster.sh writes usage-during-run.txt; monitor.sh writes the
    # pod-usage CSV — parse whichever exists).
    agent_series = {p: s for p, s in usage.items() if "loadtest" in p and s[0][3] == "pod"}
    if not agent_series and (run_dir / "usage-during-run.txt").is_file():
        # No monitor CSV: fall back to the in-run sampler for both the
        # agent pods and the PAZ pods (the sampler records both). The run's
        # UTC date comes from env.json so sampler timestamps share the k6
        # streams' absolute frame.
        run_date = (env_meta.get("timestamp_utc") or "1970-01-01T00:00:00Z")[:10]
        sampled = parse_usage_sampler(run_dir / "usage-during-run.txt", run_date)
        agent_series = {p: s for p, s in sampled.items() if "loadtest" in p}
        paz_series = {p: s for p, s in sampled.items()
                      if "pingauthorize" in p and "pap" not in p}
    # A report without PAZ pod CPU is not a complete report — this campaign's
    # numbers only mean something next to the platform load that produced
    # them (and a silently-absent panel reads as data, not absence). Warn
    # loudly at generation time; the report itself also carries a notice.
    if not paz_series:
        print("WARNING: no pod-usage CSV found in results/ — the PAZ-CPU "
              "and node-CPU panels will be MISSING from this report. "
              "Start scripts/monitor.sh BEFORE the run and keep it until it "
              "ends; regenerate afterwards.", file=sys.stderr)

    # Time base: k6 series t0 if present, else CSV start.
    t0 = min((s["t0"] for s in series.values()), default=None)
    if t0 is None and paz_series:
        first = min(s[0][0] for s in paz_series.values())
        t0 = datetime.fromisoformat(first.replace("Z", "+00:00"))

    # ---- page 1 series: the platform as a whole, pooled across all agents ----
    # Every agent measures the same platform; pooling every raw datapoint gives
    # the aggregate view (total req/s, platform-wide avg/p90 per second) rather
    # than per-agent slices. Per-agent series stay on the details page.
    throughput = []
    latency_avg = []
    latency_p90 = []
    if series:
        # Aggregate buckets key on ABSOLUTE time: each agent's stream has its
        # own t0 (its first datapoint), and combining per-agent relative
        # seconds would mix different wall-clock moments into one bucket
        # (2026-11-17 review, finding 8). t0 here is the EARLIEST first
        # datapoint across agents; every agent's buckets are shifted by its
        # own t0 offset against that origin.
        agg_t0 = min(s["t0"] for s in series.values())
        pooled = {}  # sec since common origin -> list of raw latencies
        for s in series.values():
            own0 = (datetime.fromisoformat(str(s["t0"]).replace("Z", "+00:00"))
                    if isinstance(s["t0"], str) else s["t0"])
            origin = (datetime.fromisoformat(str(agg_t0).replace("Z", "+00:00"))
                      if isinstance(agg_t0, str) else agg_t0)
            shift = int((own0 - origin).total_seconds())
            for sec, vals in s["buckets_ms"].items():
                pooled.setdefault(sec + shift, []).extend(vals)
        secs = sorted(pooled)
        agg_rate = [(sec, len(pooled[sec])) for sec in secs]
        agg_avg = [(sec, sum(pooled[sec]) / len(pooled[sec])) for sec in secs]
        agg_p90 = []
        for sec in secs:
            vals = sorted(pooled[sec])
            agg_p90.append((sec, vals[min(len(vals) - 1, int(math.ceil(0.9 * len(vals))) - 1)]))
        # Page-1 charts show RAW per-second values (no smoothing): the wave
        # texture is legible at this noise level, and chart peaks stay honest
        # (smoothing halves crest height). Gating numbers come from raw
        # datapoints regardless of chart rendering.
        throughput = [("total req/s", "#2563eb", agg_rate)]
        latency_avg = [("platform avg", "#2563eb", agg_avg)]
        latency_p90 = [("platform p90", "#d97706", agg_p90)]
    else:
        # No per-request streams: still chart throughput from the k6 progress
        # lines (cumulative iterations, differentiated per agent and summed).
        per_agent_cum = {}
        for log in sorted(run_dir.glob("agent-*.log")):
            cum = parse_k6_progress_lines(log)
            if len(cum) > 10:
                per_agent_cum[log.stem] = cum
        if per_agent_cum:
            maxsec = int(max(sec for pts in per_agent_cum.values() for sec, _ in pts))
            rate = []
            for sec in range(1, maxsec + 1):
                total = 0
                for pts in per_agent_cum.values():
                    d = dict(pts)
                    total += d.get(sec, d.get(sec - 1, 0)) - d.get(sec - 1, d.get(sec, 0))
                rate.append((sec, max(0, total)))
            throughput = [("total req/s", "#2563eb", rate)]
            latency_avg = latency_p90 = None
        else:
            throughput = latency_avg = latency_p90 = None

    cpu_charts = None
    cpu_series = []
    node_cpu_series = []
    if t0 is not None and paz_series:
        # Node names are pseudonymized (sorted order -> node-01..node-NN):
        # published reports must not carry the cluster's private DNS names
        # (a private IP like 10.33.x.x or a cloud-internal hostname pins the
        # environment). Engine pod names get the same treatment on the way
        # into evidence/ (paz-01..NN) — their deployment-hash suffix is a
        # fingerprint of the live cluster too.
        node_alias = {node: f"node-{i + 1:02d}"
                      for i, node in enumerate(sorted(node_series))}
        for i, (pod, pts) in enumerate(sorted(paz_series.items())):
            # paz-pingauthorize-<n> -> paz-<n>
            short = pod.split("-")[-1]
            secs = to_seconds([(r[0], r[1]) for r in pts], t0)
            cpu_series.append((f"paz-{short}", PALETTE[i % len(PALETTE)], secs))
        # Node-level CPU (percentage) for the nodes hosting the PAZ pods —
        # exposes co-tenant load that inflates per-pod CPU and latency.
        for node, pts in sorted(node_series.items()):
            secs = to_seconds([(r[0], r[2]) for r in pts], t0)  # col3 = cpu %
            if secs:
                node_cpu_series.append((node_alias[node], secs))

    # ---- aggregate numbers ----
    total_reqs = sum(int(s.get("reqs", [0])[0]) for s in summaries) or \
        sum(s["total_requests"] for s in series.values())
    # Missing success evidence is NOT a perfect score: with no agent summaries
    # (or no success metric in them) the gate cannot be evaluated and the run
    # is INVALID (2026-11-17 review, finding 1). Same for platform_p95 below:
    # with no streams and no logs the fallback must be None, not 0.
    success_vals = [s.get("success", [None])[0] for s in summaries if s.get("success")]
    success_pct = min(success_vals) if success_vals else None

    # Platform-wide p95: the true pooled percentile over every measured-phase
    # datapoint across all agents (exactly the same values a reviewer would
    # recompute from the raw NDJSON streams). Falls back to the worst agent's
    # end-of-run p95 only when no streams exist.
    platform_p95 = pooled_percentile(series) if series else (
        max((s["latency"][5] for s in summaries if "latency" in s), default=None))
    duration_s = max((s["dur_p90_s"][-1][0] for s in series.values()), default=None)
    # Dropped iterations and achieved-vs-target rate: the generator must not
    # shed load silently (Little's-law VU cap). The end-of-run aggregate
    # includes warmup; the measured-window target is target_rate x agents x
    # measured seconds, and the measured requests exclude the warmup scenario.
    total_dropped = sum(int(s.get("dropped", [0])[0]) for s in summaries)

    def parse_duration(text):
        # k6 duration forms: "30s", "5m", "5m0s" (progress lines)
        total = 0.0
        for num, unit in re.findall(r"([\d.]+)([smh])", text):
            total += float(num) * (60 if unit == "m" else 3600 if unit == "h" else 1)
        return total if total else None

    # Run configuration comes from the run's own captured metadata (env.json,
    # written by run-in-cluster.sh at launch) — never from parsing the log's
    # scenario description lines. The log-regex source of target_rate survived
    # the constant-arrival -> ramping-arrival rewrite (the summary line's
    # scenario name changed to mix_*), which silently disabled the
    # achieved-vs-target gate for every ramped run (2026-11-17 review, finding
    # 2): target_rate stayed None, achieved_pct stayed None, and the verdict
    # treated None as a pass. env.json carries RATE/AGENTS/WARMUP/DURATION
    # regardless of script shape, so the gate cannot be silently disabled by
    # future log-format drift.
    cfg_rate = float(env_meta.get("RATE")) if env_meta.get("RATE") else None
    cfg_agents = int(env_meta.get("AGENTS")) if env_meta.get("AGENTS") else None
    cfg_warmup_s = parse_duration(env_meta.get("WARMUP", "")) if env_meta.get("WARMUP") else None
    cfg_duration_s = parse_duration(env_meta.get("DURATION", "")) if env_meta.get("DURATION") else None

    # Measured-window requests: only datapoints inside the measured phase,
    # from the raw streams (the streams carry the per-request phase tag).
    # The end-of-run log aggregate is ramp-inclusive and may not be used as
    # the numerator — that fallback was the source of the published 1.05x
    # inflation (ramp ~15s of full-rate-equivalent traffic / 300s window).
    measured_reqs = sum(s["total_requests"] for s in series.values()) if series else None
    # The window is the CONFIGURED measured duration. An observed stream span
    # is not an acceptable substitute (follow-up review, finding 1): a run
    # that died at 40s would otherwise be evaluated against a 40s target and
    # its shortfall hidden. cfg_duration_s being None invalidates the run.
    window_s = cfg_duration_s
    achieved_pct = None
    if cfg_rate and measured_reqs and window_s:
        # env.json's RATE is the TOTAL target across all agents (the profile
        # semantic: "RATE=2000 = 2000 decisions/s aggregate"); the per-agent
        # split is RATE/AGENTS. The denominator is therefore RATE x window.
        target_measured = cfg_rate * window_s
        achieved_pct = 100.0 * measured_reqs / target_measured
        # No cap: overdelivery is a finding, not a rounding artifact to hide.
        # (Boundary iterations can push this a hair above 100; anything
        # materially above 100 means the run's window or config is wrong.)

    # The "avg throughput" tile: measured-window requests over the measured
    # window — the SAME numerator and window as the achieved-vs-target
    # metric. When the streams (and with them the measured numerator) are
    # unavailable, the tile is NOT APPROXIMATED from ramp-inclusive log
    # totals; it reports None and the report says so.
    throughput_total = (measured_reqs / window_s) if (measured_reqs is not None and window_s) else None
    # Thresholds ride with the run's captured metadata (env.json), falling
    # back to the last-resort defaults; never a hardcoded constant.
    p95_gate = float(env_meta.get("P95_MS", 500.0))
    success_gate = float(env_meta.get("SUCCESS_RATE", 0.99)) * 100.0

    # Verdict: every mandatory gate must be COMPUTABLE — a gate that cannot
    # be evaluated is insufficient evidence, not a pass (2026-11-17 review,
    # finding 1). Insufficient evidence -> INVALID, never PASS.
    gate_failures = []
    evidence_gaps = []
    if not summaries:
        evidence_gaps.append("no agent logs parsed")
    if not series:
        evidence_gaps.append("no per-request metric streams")
    elif measured_reqs is None or measured_reqs <= 0:
        evidence_gaps.append("zero measured requests")
    if achieved_pct is None:
        evidence_gaps.append("achieved-vs-target not computable (missing env.json, DURATION, or streams)")

    # Agent inventory: every expected agent (env.json AGENTS) must have BOTH
    # a parsed log and a metric stream (follow-up review, finding 2). Some
    # agents present is not the run configured.
    if cfg_agents:
        expected_ids = {f"agent-{i}" for i in range(cfg_agents)}
        log_ids = {s["agent"] for s in summaries}
        stream_ids = set(series)
        for missing in sorted(expected_ids - log_ids):
            evidence_gaps.append(f"agent log missing: {missing}")
        for missing in sorted(expected_ids - stream_ids):
            evidence_gaps.append(f"agent stream missing: {missing}")
        for extra in sorted((log_ids | stream_ids) - expected_ids):
            evidence_gaps.append(f"unexpected agent present: {extra}")

    # k6 execution evidence: the runner writes per-agent exit codes into
    # env.json (agent_exit_codes, follow-up review finding 2); a parsed log
    # that says k6 exited nonzero, or a missing exit code for a collected
    # agent, is a failure. Historical runs (pre-finding-2 runner) carry no
    # per-agent codes, but their COLLECTION is itself evidence: run-in-
    # cluster.sh exits nonzero before declaring "Results saved" unless the
    # Job reached succeeded == AGENTS (every pod exits 0), so a run dir that
    # exists with all agent streams counts as job-level success. The
    # report records which evidence level it used.
    agent_exit_codes = env_meta.get("agent_exit_codes") or {}
    exit_evidence = "per-agent" if agent_exit_codes else (
        "job-level (historical: runner verified succeeded==AGENTS before saving)"
        if summaries else "none")
    if agent_exit_codes:
        for s in summaries:
            code = agent_exit_codes.get(s["agent"])
            if code is None:
                evidence_gaps.append(f"k6 exit status unknown: {s['agent']}")
            elif int(code) != 0:
                gate_failures.append(f"k6 exited {code}: {s['agent']}")

    # Per-scenario measured gates from the streams (follow-up review,
    # finding 5): pooled statistics hide a failing low-volume scenario, so
    # the report evaluates each configured scenario's measured p95, success,
    # and error count independently.
    expected_scenarios = {"mix_read", "mix_session", "mix_hitl", "mix_deny"}
    scen_seen = set()
    for s in series.values():
        scen_seen.update(s.get("scenarios", {}).keys())
    if series and not scen_seen:
        evidence_gaps.append("no per-scenario evidence in streams")
    scen_gates = {}
    for name in sorted(expected_scenarios | scen_seen):
        agg = {"p95_ms": None, "success_pct": None, "errors": 0, "requests": 0}
        p95s, ok, tot = [], 0, 0
        errs = 0
        reqs = 0
        for s in series.values():
            sc = s.get("scenarios", {}).get(name)
            if not sc:
                continue
            reqs += sc.get("requests", 0)
            if sc.get("p95_ms") is not None:
                p95s.append((sc["requests"], sc["p95_ms"]))
            if sc.get("success_total"):
                ok += sc.get("success_ok", 0)
                tot += sc.get("success_total", 0)
            errs += sc.get("errors", 0)
        agg["requests"] = reqs
        agg["errors"] = errs
        # request-weighted pooled p95 per scenario (max of agent p95s is the
        # conservative choice for a gate; weighted pooling of raw values is
        # the pooled figure reported separately)
        if p95s:
            agg["p95_ms"] = max(p for _, p in p95s)
        if tot:
            agg["success_pct"] = 100.0 * ok / tot
        scen_gates[name] = agg
        if reqs == 0:
            gate_failures.append(f"scenario {name}: zero measured requests")
            continue
        if agg["p95_ms"] is not None and not (agg["p95_ms"] < p95_gate):
            gate_failures.append(f"scenario {name}: p95 {agg['p95_ms']} vs {p95_gate}")
        if agg["success_pct"] is not None and not (agg["success_pct"] >= success_gate):
            gate_failures.append(f"scenario {name}: success {agg['success_pct']:.2f}% vs {success_gate}")
        if errs != 0:
            gate_failures.append(f"scenario {name}: {errs} classified errors")

    # Aggregate gates (pooled statistics — labeled separately from the
    # per-scenario acceptance gates above).
    if platform_p95 is None or not (platform_p95 < p95_gate):
        gate_failures.append(f"pooled p95 gate: {platform_p95} vs {p95_gate}")
    if success_pct is None or not (success_pct >= success_gate):
        gate_failures.append(f"pooled success gate: {success_pct} vs {success_gate}")
    if total_dropped != 0:
        gate_failures.append(f"dropped iterations: {total_dropped}")
    # Achieved rate: computed AND enforced (follow-up review, finding 1) —
    # the first review fix only required computability; a 2%-achieved run
    # passed. Below 98% of the configured target is a FAIL.
    if achieved_pct is None:
        evidence_gaps.append("achieved-vs-target not computable (missing env.json or streams)")
    elif achieved_pct < 98.0:
        gate_failures.append(f"achieved rate: {achieved_pct:.2f}% vs required 98%")
    # k6's own exit status is ground truth for its thresholds; a run whose
    # captured exit code says a threshold failed is a FAIL even if local
    # parsing cannot see which.
    k6_exit = env_meta.get("k6_exit_code")
    if k6_exit is not None and int(k6_exit) != 0:
        gate_failures.append(f"k6 exited {k6_exit}")
    gate_failures.extend(evidence_gaps)
    if gate_failures:
        verdict = "INVALID" if evidence_gaps else "FAIL"
    else:
        verdict = "PASS"

    agent_rows = []
    for s in sorted(summaries, key=lambda x: x["agent"]):
        l = s["latency"]
        agent_rows.append(
            f"<tr><td>{escape(s['agent'])}</td>"
            f"<td class='num'>{l[0]:.1f}</td><td class='num'>{l[2]:.1f}</td>"
            f"<td class='num'>{l[5]:.1f}</td><td class='num'>{l[4]:.1f}</td>"
            f"<td class='num'>{l[3]:.0f}</td>"
            f"<td class='num'>{s.get('success', [0])[0]:.2f}%</td>"
            f"<td class='num'>{int(s.get('reqs', [0])[0])}</td>"
            f"<td class='num'>{int(s.get('dropped', [0])[0])}</td></tr>"
        )

    detail_sections = ""
    for agent in sorted(set(list(series.keys()) + [s["agent"] for s in summaries])):
        s = series.get(agent)
        summ = next((x for x in summaries if x["agent"] == agent), None)
        charts = ""
        if s:
            p90_max = max((v for _, v in s["dur_p90_s"]), default=0)
            charts += line_chart(f"{agent} — response time", [
                ("avg", "#2563eb", s["dur_avg_s"]),
                ("p90", "#d97706", s["dur_p90_s"]),
            ], "ms", y_max=p90_max * 1.3 or None)
            charts += line_chart(f"{agent} — throughput", [
                ("req/s", "#059669", s["req_rate"]),
            ], "req/s")
        if summ and "latency" in summ:
            l = summ["latency"]
            stat_rows = (
                f"<tr><td>avg</td><td class='num'>{l[0]:.2f} ms</td></tr>"
                f"<tr><td>median</td><td class='num'>{l[2]:.2f} ms</td></tr>"
                f"<tr><td>p90</td><td class='num'>{l[4]:.2f} ms</td></tr>"
                f"<tr><td>p95</td><td class='num'>{l[5]:.2f} ms</td></tr>"
                f"<tr><td>max</td><td class='num'>{l[3]:.2f} ms</td></tr>"
                f"<tr><td>requests</td><td class='num'>{int(summ.get('reqs', [0])[0])}</td></tr>"
                f"<tr><td>success</td><td class='num'>{summ.get('success', [0])[0]:.2f}%</td></tr>"
            )
        else:
            stat_rows = "<tr><td colspan='2' class='muted'>No end-of-run summary.</td></tr>"
        detail_sections += f"""
<section id="{escape(agent)}">
  <h3>{escape(agent)}</h3>
  {charts}
  <table><tr><th>metric</th><th class="num">value</th></tr>{stat_rows}</table>
</section>"""

    # page-1 charts (skip empty)
    chart_html = ""
    if throughput:
        chart_html += line_chart("Throughput over time", throughput, "req/s",
                                 x_grid_every=30, area_fill=True)
    if latency_avg:
        chart_html += line_chart("Response time — avg", latency_avg, "ms (per-second raw)")
        chart_html += line_chart("Response time — p90", latency_p90, "ms (per-second raw)")
    if not cpu_series:
        chart_html += ("<div style='padding:12px 16px;border:1px solid #f59e0b;"
                       "background:#fef3c7;border-radius:6px;margin:8px 0;"
                       "font-size:13px'><b>Engine CPU data missing</b> — no "
                       "pod-usage CSV was captured for this run. The verdict "
                       "holds on latency/success/rate alone; platform CPU "
                       "context is absent. Regenerate after a monitored "
                       "rerun.</div>")
    if cpu_series:
        chart_html += stacked_area_chart("Engine CPU", cpu_series, "millicores")
    if node_cpu_series:
        # Each node's total CPU as % of its allocatable — reveals co-tenant
        # load that inflates PAZ CPU and latency. Labels are already the
        # pseudonyms (node-01..NN) assigned in the cpu_series block above.
        node_lines = [
            (n, PALETTE[(i + 2) % len(PALETTE)], pts)
            for i, (n, pts) in enumerate(node_cpu_series)
        ]
        chart_html += line_chart("Hosting nodes — total CPU", node_lines,
                                 "% of node allocatable", y_max=130)
    if series:
        # Latency breakdown (M4): where the wall-clock time went — connection
        # setup, TLS, server wait, transfer. Pooled per-second means across
        # agents; explains WHAT changed when a stage's p95 moves.
        sub_means = pooled_subtiming_means(series)
        breakdown = []
        labels = {
            "http_req_connecting": ("connecting", "#d97706"),
            "http_req_tls_handshaking": ("tls_handshaking", "#7c3aed"),
            "http_req_waiting": ("waiting (server)", "#2563eb"),
            "http_req_sending": ("sending", "#059669"),
            "http_req_receiving": ("receiving", "#dc2626"),
        }
        for name, (label, color) in labels.items():
            if name in sub_means and sub_means[name]:
                breakdown.append((label, color, sub_means[name]))
        if breakdown:
            chart_html += line_chart("Latency breakdown (per-second raw)",
                                     breakdown, "ms per phase")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pf-perf run {escape(sys.argv[1])}</title>
<style>
  :root {{
    color-scheme: light dark;
    --fg: #0f172a; --fg-soft: #475569; --bg: #f1f5f9; --card: #ffffff;
    --line: #e2e8f0; --muted: #64748b; --accent: #2563eb;
    --green: #059669; --red: #dc2626; --amber-bg: #fffbeb; --amber-line: #f59e0b;
    --amber-fg: #92400e; --shadow: 0 1px 3px rgba(15,23,42,.06), 0 1px 2px rgba(15,23,42,.04);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --fg: #e2e8f0; --fg-soft: #94a3b8; --bg: #0b0f17; --card: #131a26;
      --line: #253044; --muted: #7c8aa0; --accent: #3b82f6;
      --green: #34d399; --red: #f87171; --amber-bg: #3d2f10; --amber-line: #b45309;
      --amber-fg: #fbbf24;
      --shadow: 0 1px 3px rgba(0,0,0,.4);
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font: 14px/1.55 -apple-system, "Segoe UI", Inter, sans-serif; margin: 0 auto;
    padding: 0 20px 40px; max-width: 960px; background: var(--bg); color: var(--fg);
  }}
  header.hero {{
    padding: 26px 0 14px; display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
  }}
  header.hero h1 {{ font-size: 21px; font-weight: 700; letter-spacing: -.01em; margin: 0; }}
  header.hero .rid {{ font-family: ui-monospace, monospace; font-size: 12px; color: var(--muted);
    background: var(--card); border: 1px solid var(--line); border-radius: 6px; padding: 2px 8px; }}
  nav.pages {{
    position: sticky; top: 0; z-index: 5; background: color-mix(in srgb, var(--bg) 88%, transparent);
    backdrop-filter: blur(6px); padding: 8px 0; margin-bottom: 6px;
    border-bottom: 1px solid var(--line);
  }}
  nav.pages a {{
    color: var(--fg-soft); text-decoration: none; font-size: 13px; font-weight: 500;
    padding: 5px 12px; border-radius: 999px; margin-right: 6px;
  }}
  nav.pages a:hover {{ background: var(--card); color: var(--fg); }}
  h2 {{ font-size: 16px; font-weight: 650; margin: 26px 0 10px; letter-spacing: -.01em; }}
  h3 {{ font-size: 14px; margin: 18px 0 6px; }}
  .tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 10px; margin: 12px 0 4px; }}
  .tile {{
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 12px 16px; box-shadow: var(--shadow);
  }}
  .tile .v {{ font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums;
              letter-spacing: -.01em; }}
  .tile .l {{ font-size: 10px; color: var(--muted); text-transform: uppercase;
              letter-spacing: .06em; margin-top: 1px; }}
  .pass {{ color: var(--green); }} .fail {{ color: var(--red); }} .invalid {{ color: var(--red); }}
  table {{ border-collapse: separate; border-spacing: 0; width: 100%; margin: 10px 0;
           background: var(--card); border: 1px solid var(--line); border-radius: 12px;
           overflow: hidden; box-shadow: var(--shadow); }}
  th, td {{ text-align: left; padding: 7px 14px; border-bottom: 1px solid var(--line);
            font-variant-numeric: tabular-nums; }}
  tr:last-child td {{ border-bottom: none; }}
  th {{ font-size: 10px; text-transform: uppercase; color: var(--muted);
        letter-spacing: .06em; background: color-mix(in srgb, var(--card) 92%, var(--bg)); }}
  .num {{ text-align: right; }}
  figure.chart {{
    margin: 12px 0; background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 12px 14px 8px; box-shadow: var(--shadow);
  }}
  figcaption {{ font-size: 13px; font-weight: 600; margin-bottom: 4px; }}
  .unit {{ font-weight: 400; color: var(--muted); font-size: 11px; margin-left: 6px; }}
  svg.svg {{ width: 100%; height: auto; display: block; }}
  .tick {{ font-size: 9px; fill: var(--muted); }}
  .legend {{ margin-top: 4px; font-size: 11px; color: var(--muted); }}
  .lg i {{ display: inline-block; width: 9px; height: 9px; border-radius: 2px;
           margin: 0 5px 0 12px; }}
  .lg:first-child i {{ margin-left: 0; }}
  .muted {{ color: var(--muted); }}
  .warn {{
    background: var(--amber-bg); border: 1px solid var(--amber-line); color: var(--amber-fg);
    padding: 9px 13px; border-radius: 10px; font-size: 12px; margin: 10px 0;
  }}
  footer {{ margin-top: 34px; font-size: 10px; color: var(--muted); }}
</style></head><body>
<header class="hero">
  <h1>Token-exchange performance</h1>
  <span class="rid">run {escape(sys.argv[1])}</span>
</header>
<nav class="pages">
  <a href="#aggregated">Aggregated</a>
  <a href="#agents">Per-agent details</a>
</nav>
<h2 id="aggregated">Aggregated</h2>
<div class="tiles">
  <div class="tile"><div class="v {verdict.lower()}">{verdict}</div><div class="l">thresholds</div></div>
  <div class="tile"><div class="v">{'—' if success_pct is None else f'{success_pct:.2f}%'}</div><div class="l">min success</div></div>
  <div class="tile"><div class="v">{'—' if platform_p95 is None else f'{platform_p95:.1f} ms'}</div><div class="l">platform p95</div></div>
  <div class="tile"><div class="v">{'—' if achieved_pct is None else f'{achieved_pct:.1f}%'}</div><div class="l">achieved vs target rate</div></div>
  <div class="tile"><div class="v {verdict.lower() if total_dropped else ''}">{total_dropped:,}</div><div class="l">dropped iterations</div></div>
  <div class="tile"><div class="v">{'—' if throughput_total is None else f'{throughput_total:.0f}/s'}</div><div class="l">avg throughput (measured window)</div></div>
  <div class="tile"><div class="v">{total_reqs:,}</div><div class="l">total requests</div></div>
</div>
{'<p class="warn">Note: time-series request counts do not match the end-of-run summaries — the series may be synthetic/preview data.</p>' if fixture_warning else ''}{'<p class="muted">Note: per-request streams are snapshotted while the agents run; the last few seconds of each stream are missing (end-of-run summaries are complete).</p>' if stream_truncated else ''}
{chart_html}
<h2 id="agents">Per-agent details</h2>
<p class="muted">{len(summaries)} agent(s) in this run.</p>
<table><tr><th>agent</th><th class="num">avg ms</th><th class="num">med ms</th>
<th class="num">p95 ms</th><th class="num">p90 ms</th><th class="num">max ms</th>
<th class="num">success</th><th class="num">requests</th><th class="num">dropped</th></tr>
{''.join(agent_rows)}
</table>
{detail_sections}
<footer>Generated {timestamp} by scripts/generate-report.py — self-contained, no external resources.</footer>
</body></html>"""
    out = run_dir / "report.html"
    out.write_text(html)
    print(f"Report written: {out}")

    # Commit-friendly per-run summary (raw streams stay out of git; this
    # JSON makes the evidence behind any report claim version-controlled).
    lat_stats = pooled_latency_stats(series) if series else None

    # Pooled elapsed percentiles over the measured window (sideband_request_
    # elapsed points, bucketed per agent). Empty for runs whose generator
    # predated the metric (the 2026-11-17 campaign) — reported as None.
    elapsed_vals = sorted(
        ms for s in series.values()
        for vals in s.get("elapsed_buckets_ms", {}).values()
        for ms in vals)
    e_n = len(elapsed_vals)
    if e_n:
        e_avg = sum(elapsed_vals) / e_n
        e_p90 = elapsed_vals[min(e_n - 1, int(math.ceil(0.90 * e_n)) - 1)]
        e_p95 = elapsed_vals[min(e_n - 1, int(math.ceil(0.95 * e_n)) - 1)]
    else:
        e_avg = e_p90 = e_p95 = None

    # CPU columns for the README table: steady-state statistics per PAZ pod
    # and per loadtest agent, from the monitor CSV or the in-run sampler
    # (whichever exists). Samples are first restricted to the run's measured
    # interval (sampler data covers warmup and gracefulStop too), then each
    # pod's chronological middle 60% is kept (ramp/teardown edges discarded)
    # and the MEDIAN of that window is reported — the actual median, not a
    # trimmed mean (2026-11-17 review, finding 5). Each PAZ replica is
    # reported separately; the pooled figure is the mean of per-replica
    # medians and carries the replica count.
    def steady_median_m(pts, t_start=None, t_end=None):
        if t_start is not None:
            def as_dt(t):
                if isinstance(t, str):
                    return datetime.fromisoformat(t.replace("Z", "+00:00"))
                return t
            pts = [p for p in pts if t_start <= as_dt(p[0]) <= t_end]
        if len(pts) < 5:
            return None, 0
        # chronological order preserved; drop the temporal edges
        lo, hi = int(len(pts) * 0.2), int(len(pts) * 0.8)
        window = [cpu_m for _, cpu_m, _, _ in pts[lo:hi]]
        window.sort()  # median needs sorted values; only after temporal selection
        mid = len(window) // 2
        median = (window[mid] if len(window) % 2
                  else (window[mid - 1] + window[mid]) / 2)
        return median, len(window)

    # measured-interval bounds in absolute time. t0 is the FIRST MEASURED
    # datapoint (the stream parser excludes phase=warmup before setting it),
    # so the window opens AT t0 — adding the configured warmup here measured
    # a window 30s late and dropped ten valid samples (follow-up review,
    # finding 4). The window closes at t0 + configured duration.
    cpu_t_start = cpu_t_end = None
    if t0 is not None and cfg_duration_s:
        cpu_t_start = t0
        cpu_t_end = t0 + timedelta(seconds=cfg_duration_s)
    paz_replicas = {}
    for pod, pts in sorted(paz_series.items()):
        med, n = steady_median_m(pts, cpu_t_start, cpu_t_end)
        if med is not None:
            paz_replicas[pod] = {"median_m": round(med), "samples": n}
    agent_meds = []
    agent_samples = 0
    for pod, pts in sorted(agent_series.items()):
        med, n = steady_median_m(pts, cpu_t_start, cpu_t_end)
        if med is not None:
            agent_meds.append(med)
            agent_samples += n
    paz_meds = [r["median_m"] for r in paz_replicas.values()]
    cpu_stats = {
        # Millicores; the 4-CPU PAZ limit and 500m agent request are the
        # reference points in the README table.
        "paz_pod_median_m": round(sum(paz_meds) / len(paz_meds)) if paz_meds else None,
        "paz_pods": len(paz_meds),
        "paz_replicas": paz_replicas,
        "agent_median_m": round(sum(agent_meds) / len(agent_meds)) if agent_meds else None,
        "agents": len(agent_meds),
        "interval": ("measured" if cpu_t_start is not None else "full-sampler"),
        # Provenance (follow-up review, finding 4): which monitoring source
        # fed the statistics and the window they were taken over.
        "source": usage_source,
        "source_is_run_file": usage_from_run_dir,
        "window_utc": ([cpu_t_start.isoformat(), cpu_t_end.isoformat()]
                       if cpu_t_start is not None else None),
    }

    summary = {
        "run_id": sys.argv[1],
        "profile": env_meta.get("profile"),
        "timestamp_utc": timestamp,
        "verdict": verdict,
        "gate_failures": gate_failures,
        # Per-scenario acceptance gates (follow-up review, finding 5) —
        # evaluated independently of the pooled statistics above.
        "scenario_gates": {
            name: {**agg, "p95_ms": round(agg["p95_ms"], 2) if agg["p95_ms"] is not None else None,
                   "success_pct": round(agg["success_pct"], 2) if agg["success_pct"] is not None else None}
            for name, agg in scen_gates.items()
        },
        "p95_pooled_measured_ms": round(platform_p95, 2) if platform_p95 is not None else None,
        "p95_gate_ms": p95_gate,
        "success_pct": round(success_pct, 2) if success_pct is not None else None,
        "achieved_vs_target_pct": round(achieved_pct, 2) if achieved_pct is not None else None,
        "throughput_measured_per_s": round(throughput_total, 1) if throughput_total is not None else None,
        "target_rate_per_agent": (cfg_rate / cfg_agents) if (cfg_rate and cfg_agents) else None,
        "dropped_iterations": total_dropped,
        "total_requests_measured": sum(s["total_requests"] for s in series.values()) if series else None,
        "measured_duration_s": duration_s,
        # The README-table column set: every latency column from the SAME
        # measured-window pooled distribution (k6's end-of-run line is
        # warmup-inclusive and prints no p99). None when no streams exist.
        "pooled_latency": {
            "avg_ms": round(lat_stats["avg_ms"], 2),
            "median_ms": round(lat_stats["median_ms"], 2),
            "p90_ms": round(lat_stats["p90_ms"], 2),
            "p95_ms": round(lat_stats["p95_ms"], 2),
            "p99_ms": round(lat_stats["p99_ms"], 2),
            "max_ms": round(lat_stats["max_ms"], 2),
            "requests": lat_stats["requests"],
        } if lat_stats else None,
        # Full-call elapsed (sideband_request_elapsed: sending + waiting +
        # receiving + connection establishment) — parsed from the streams
        # (follow-up review, finding 7). None for the campaign runs: the
        # metric started streaming only after the review fixes, and
        # reprocessing cannot recover a custom metric never captured.
        "pooled_elapsed": {
            "avg_ms": round(e_avg, 2),
            "p90_ms": round(e_p90, 2),
            "p95_ms": round(e_p95, 2),
            "requests": e_n,
        } if e_n else None,
        # Steady-state CPU medians (see cpu_stats above) — the README
        # table's CPU columns. None when no usage data was captured.
        "cpu": cpu_stats,
        "agents": [
            {
                "agent": s["agent"],
                "avg_ms": s["latency"][0],
                "p95_ms": s["latency"][5],
                "success_pct": s.get("success", [0])[0],
                "requests": int(s.get("reqs", [0])[0]),
                "dropped": int(s.get("dropped", [0])[0]),
            }
            for s in sorted(summaries, key=lambda x: x["agent"])
        ],
    }
    # Server-side ground truth: cn=Monitor Gateway Timing deltas. Monitor
    # counters are since-server-start, so the meaningful number is the delta
    # between this run's scrape and the previous run's — computed here from
    # the numeric attributes PAZ exposes (counters/timers named gateway*).
    monitor_deltas = {}
    for mf in sorted(run_dir.glob("monitor-paz-*.json")):
        try:
            entries = json.loads(mf.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        pod = mf.stem.replace("monitor-paz-", "")
        agg = {}
        for entry in entries:
            for key, vals in entry.get("attrs", {}).items():
                if key.startswith("ds-mon-"):
                    for v in vals:
                        # PAZ monitor attributes are strings ("15000") or
                        # single-key objects; accept both shapes.
                        if isinstance(v, dict):
                            v = next(iter(v.values()), None)
                        try:
                            agg[key] = agg.get(key, 0.0) + float(v)
                        except (TypeError, ValueError):
                            pass
        if agg:
            monitor_deltas[pod] = agg
    if monitor_deltas:
        summary["monitor_totals"] = {
            "note": ("since-server-start totals from PAZ cn=Monitor Gateway "
                     "providers at scrape time; derive per-run deltas by "
                     "subtracting the previous run's summary in the campaign"),
            "pods": monitor_deltas,
        }

    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Summary written: {run_dir / 'summary.json'}")

    # Local audit layer (git-ignored like results/): summary.json + a
    # sanitized env.json copied to evidence/<run_id>/ — a compact
    # cross-check of each report's numbers without the 100 MB streams.
    # env.json is sanitized on the way in: node names -> node-01..NN
    # (sorted, same aliases the report charts use), PAZ pod names ->
    # paz-01..NN (deployment hashes fingerprint the live cluster),
    # namespace generalized.
    ev_env = dict(env_meta)
    if ev_env:
        node_names = sorted(n["name"] for n in ev_env.get("nodes", []))
        alias = {n: f"node-{i + 1:02d}" for i, n in enumerate(node_names)}
        for n in ev_env.get("nodes", []):
            n["name"] = alias.get(n["name"], n["name"])
        # Engine pod names carry the deployment-hash suffix of the live
        # cluster (paz-pingauthorize-<n>) — a fingerprint.
        # Alias to paz-01..NN (sorted, stable per run); keep the node
        # placement so per-pod CPU panels stay interpretable.
        pod_names = sorted(p["name"] for p in ev_env.get("paz_pods", []))
        pod_alias = {p: f"paz-{i + 1:02d}" for i, p in enumerate(pod_names)}
        for p in ev_env.get("paz_pods", []):
            p["name"] = pod_alias.get(p["name"], p["name"])
            p["node"] = alias.get(p["node"], p["node"])
        ev_env["NAMESPACE"] = "pf-perf-lab"
    ev_dir = RESULTS.parent / "evidence" / sys.argv[1]
    ev_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    if ev_env:
        (ev_dir / "env.json").write_text(json.dumps(ev_env, indent=2))
    print(f"Evidence copied: {ev_dir}/")


if __name__ == "__main__":
    main()
