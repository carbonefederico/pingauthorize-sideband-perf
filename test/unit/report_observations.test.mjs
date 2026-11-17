import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// Tests for the observation-based report (fourth review): the generator
// computes NO performance verdict. These pin what it must record instead —
// collection completeness, execution status verbatim, measurement
// integrity — and that it never substitutes a favorable default.

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const GEN = join(ROOT, "scripts", "generate-report.py");
const RESULTS = join(ROOT, "results");

function makeStream(agent, { measured = 100, perReqMs = 5, scenario = null, phase = "measured", successRate = 1.0, errors = 0, withSuccess = true } = {}) {
  const t0 = Date.parse("2026-11-17T10:00:30Z");
  const lines = [];
  const mix = scenario
    ? [[scenario, measured]]
    : [["mix_read", Math.ceil(measured * 0.6)],
       ["mix_session", Math.ceil(measured * 0.15)],
       ["mix_hitl", Math.ceil(measured * 0.15)],
       ["mix_deny", measured - Math.ceil(measured * 0.6) - Math.ceil(measured * 0.15) - Math.ceil(measured * 0.15)]];
  let n = 0;
  for (const [scen, count] of mix) {
    for (let i = 0; i < count; i++, n++) {
      const ts = new Date(t0 + Math.floor(n / 10) * 10).toISOString().replace(".000Z", "Z");
      const tags = { scenario: scen, phase };
      lines.push(JSON.stringify({ type: "Point", metric: "http_req_duration", data: { time: ts, value: perReqMs, tags } }));
      lines.push(JSON.stringify({ type: "Point", metric: "sideband_request_elapsed", data: { time: ts, value: perReqMs + 2, tags } }));
      if (withSuccess) {
        lines.push(JSON.stringify({ type: "Point", metric: "sideband_decision_success", data: { time: ts, value: n / measured < successRate ? 1 : 0, tags } }));
      }
      if (errors > 0 && n < errors) {
        lines.push(JSON.stringify({ type: "Point", metric: "sideband_errors", data: { time: ts, value: 1, tags } }));
      }
    }
  }
  return lines.join("\n") + "\n";
}

function makeLog(agent, { requests = 100, successPct = 100.0, p95 = 5, dropped = 0, exitCode = 0 } = {}) {
  return [
    "          * mix_read: Up to 10.00 iterations/s for 5m0s over 2 stages",
    "    http_reqs......................: " + requests + "  10/s",
    `    sideband_decision_success.....: ${successPct}% ${Math.round(requests * successPct / 100)} out of ${requests}`,
    `    sideband_decision_latency.....: avg=${p95 - 1}ms min=1ms med=${p95 - 1}ms max=10ms p(90)=${p95 - 1}ms p(95)=${p95}ms`,
    "    dropped_iterations............: " + dropped,
    `k6 exited with code ${exitCode}; holding pod for result collection`,
    "",
  ].join("\n");
}

function makeRun(t, { agents = 1, streams = null, logs = null, env = {} } = {}) {
  const dir = join(RESULTS, `zz-obs-${t}-${Date.now()}`);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "env.json"), JSON.stringify({
    RATE: "100", AGENTS: String(agents), WARMUP: "30s", DURATION: "1s",
    profile: "test", job_terminal_state: "complete", ...env,
  }));
  for (let i = 0; i < agents; i++) {
    if (streams === null || streams[i] !== false) {
      writeFileSync(join(dir, `agent-${i}-metrics.json`), streams === null ? makeStream(`agent-${i}`) : streams[i]);
    }
    if (logs === null || logs[i] !== false) {
      writeFileSync(join(dir, `agent-${i}.log`), logs === null ? makeLog(`agent-${i}`) : logs[i]);
    }
  }
  return dir;
}

function generate(dir) {
  try {
    execFileSync("python3", [GEN, dir], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  } catch { /* summary may still be written */ }
  return JSON.parse(readFileSync(join(dir, "summary.json"), "utf8"));
}

function cleanup(dir) { rmSync(dir, { recursive: true, force: true }); }

test("no verdict field exists in the summary", () => {
  const dir = makeRun("noverdict", {});
  try {
    const s = generate(dir);
    assert.equal("verdict" in s, false);
    assert.equal("gate_failures" in s, false);
  } finally { cleanup(dir); }
});

test("complete run records full collection with no issues", () => {
  const dir = makeRun("complete", { agents: 2 });
  try {
    const s = generate(dir);
    assert.equal(s.integrity.collection.agents_expected, 2);
    assert.deepEqual(s.integrity.collection.issues, []);
    for (const [agent, rec] of Object.entries(s.integrity.execution.per_agent)) {
      assert.equal(rec.exit_code, 0);
      assert.equal(rec.status, "completed");
    }
  } finally { cleanup(dir); }
});

test("missing agent stream is recorded as a collection issue, not a verdict", () => {
  const dir = makeRun("missstream", {
    agents: 2,
    streams: [makeStream("agent-0"), false],
  });
  try {
    const s = generate(dir);
    assert.ok(s.integrity.collection.issues.includes("agent stream missing: agent-1"));
    assert.equal("verdict" in s, false);
  } finally { cleanup(dir); }
});

test("nonzero k6 exit is recorded verbatim with its code, not relabeled", () => {
  const dir = makeRun("exit7", {
    env: { agent_exit_codes: { "agent-0": 7 } },
    logs: [makeLog("agent-0", { exitCode: 7 })],
  });
  try {
    const s = generate(dir);
    const rec = s.integrity.execution.per_agent["agent-0"];
    assert.equal(rec.exit_code, 7);
    assert.equal(rec.status, "exited 7");
  } finally { cleanup(dir); }
});

test("missing success evidence is an explicit limitation, never a default", () => {
  const dir = makeRun("nosucc", {
    streams: [makeStream("agent-0", { withSuccess: false })],
  });
  try {
    const s = generate(dir);
    assert.ok(s.integrity.collection.issues.some(i => i.includes("success evidence incomplete")));
    // the aggregate success stays null — no favorable default
    assert.equal(s.success_pct, null);
  } finally { cleanup(dir); }
});

test("achieved rate is reported as an observation, not gated", () => {
  // 20 delivered of 100 expected: 20% — poor delivery, still a valid report
  const dir = makeRun("lowdel", {
    streams: [makeStream("agent-0", { measured: 20 })],
    logs: [makeLog("agent-0", { requests: 20 })],
  });
  try {
    const s = generate(dir);
    assert.equal(s.integrity.achieved_vs_target_pct, 20);
    assert.equal(s.integrity.measured_requests, 20);
    assert.equal("verdict" in s, false);
  } finally { cleanup(dir); }
});

test("dropped iterations are reported as observations", () => {
  const dir = makeRun("drops", {
    logs: [makeLog("agent-0", { dropped: 42 })],
  });
  try {
    const s = generate(dir);
    assert.equal(s.integrity.dropped_iterations, 42);
  } finally { cleanup(dir); }
});

test("non-measured phases are excluded and counted, not merged", () => {
  const dir = makeRun("badphase", {
    streams: [makeStream("agent-0", { phase: "Measured" })],
  });
  try {
    const s = generate(dir);
    assert.equal(s.integrity.measured_requests, 0);
    assert.equal(s.p95_pooled_measured_ms, null);
  } finally { cleanup(dir); }
});

test("elapsed percentiles are reported separately from duration", () => {
  const dir = makeRun("elapsed", {});
  try {
    const s = generate(dir);
    // synthetic elapsed = duration + 2ms
    assert.ok(s.pooled_elapsed.p95_ms > s.pooled_latency.p95_ms);
  } finally { cleanup(dir); }
});
