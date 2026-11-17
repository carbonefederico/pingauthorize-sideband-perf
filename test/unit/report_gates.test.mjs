import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// Regression coverage for the report generator's gates (2026-11-17
// follow-up review, "Required validation" list). Each case builds a
// synthetic run directory and runs the real generator against it.

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const GEN = join(ROOT, "scripts", "generate-report.py");
const RESULTS = join(ROOT, "results");

const NS = "http://www.w3.org/2000/svg";

// Emit the four campaign scenarios in the real 60/15/15/10 mix — the
// report's per-scenario gates require measured requests for every
// configured scenario, so single-scenario fixtures fail for the wrong
// reason. All requests land INSIDE the configured 1s measurement window
// (third review, fixture-repair note): timestamps span [t0, t0+0.9s] so the
// fixture represents the configured duration and offered rate.
function makeStream(agent, { measured = 100, perReqMs = 5, scenario = null, phase = "measured", successRate = 1.0, errors = 0 } = {}) {
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
      // spread within the configured 1s window: 10ms slots
      const ts = new Date(t0 + Math.floor(n / 10) * 10).toISOString().replace(".000Z", "Z");
      const tags = { scenario: scen, phase };
      lines.push(JSON.stringify({ type: "Point", metric: "http_req_duration", data: { time: ts, value: perReqMs, tags } }));
      lines.push(JSON.stringify({ type: "Point", metric: "sideband_request_elapsed", data: { time: ts, value: perReqMs + 2, tags } }));
      lines.push(JSON.stringify({ type: "Point", metric: "sideband_decision_success", data: { time: ts, value: n / measured < successRate ? 1 : 0, tags } }));
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

function makeRun(t, { agents = 1, cfg = {}, streams = null, logs = null, env = {} } = {}) {
  const dir = join(RESULTS, `zz-test-${t}-${Date.now()}`);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "env.json"), JSON.stringify({
    RATE: "100", AGENTS: String(agents), WARMUP: "30s", DURATION: "1s",
    P95_MS: "500", SUCCESS_RATE: "0.99", profile: "test",
    // Current-format execution evidence: the runner records the Job's
    // terminal state (third review, finding 2). Tests exercising missing
    // evidence override this.
    job_terminal_state: "complete",
    ...env,
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
  let out = "";
  try {
    out = execFileSync("python3", [GEN, dir], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  } catch (e) {
    out = (e.stdout || "") + (e.stderr || "");
  }
  return JSON.parse(readFileSyncSafe(join(dir, "summary.json")));
}

import { readFileSync } from "node:fs";
function readFileSyncSafe(p) { return readFileSync(p, "utf8"); }

function cleanup(dir) { rmSync(dir, { recursive: true, force: true }); }

// --- 1: achieved rate below 98% must FAIL --------------------------------
test("achieved rate below 98% fails the run", () => {
  const dir = makeRun("lowach", {
    // 1s window at 100/s target = 100 expected; stream carries 20 -> 20%
    streams: [makeStream("agent-0", { measured: 20 })],
    logs: [makeLog("agent-0", { requests: 20 })],
  });
  try {
    const s = generate(dir);
    assert.equal(s.verdict, "FAIL");
    assert.ok(s.gate_failures.some(g => g.includes("achieved rate")));
  } finally { cleanup(dir); }
});

test("achieved rate at the 98% boundary passes, just below fails", () => {
  // 98 exactly (98 requests of 100 expected) passes; 97.9 fails.
  for (const [n, expect] of [[98, "PASS"], [97, "FAIL"]]) {
    const dir = makeRun(`bnd${n}`, {
      streams: [makeStream("agent-0", { measured: n })],
      logs: [makeLog("agent-0", { requests: n })],
    });
    try {
      const s = generate(dir);
      assert.equal(s.verdict, expect, `${n} requests`);
    } finally { cleanup(dir); }
  }
});

// --- 2: missing agents and failed k6 execution ---------------------------
test("missing agents (1 of 2 configured) is INVALID", () => {
  // env.json declares two agents; only agent-0's log/stream exist.
  const dir = makeRun("missag", {
    agents: 2, cfg: {},
    streams: [makeStream("agent-0"), false],
    logs: [makeLog("agent-0"), false],
  });
  try {
    const s = generate(dir);
    assert.equal(s.verdict, "INVALID");
    assert.ok(s.gate_failures.some(g => g.includes("agent-1")));
  } finally { cleanup(dir); }
});

test("k6 exited nonzero is a FAIL when per-agent codes are recorded", () => {
  const dir = makeRun("exit99", {
    env: { agent_exit_codes: { "agent-0": 99 } },
    // log shows a threshold failing: p95 above the 500ms gate
    logs: [makeLog("agent-0", { p95: 600 })],
  });
  try {
    const s = generate(dir);
    assert.equal(s.verdict, "FAIL");
    assert.ok(s.gate_failures.some(g => g.includes("k6 exited 99")));
  } finally { cleanup(dir); }
});

// --- 3: per-scenario gates catch what pooled statistics hide -------------
test("a failing low-volume scenario fails the run despite healthy pooled stats", () => {
  // mix_read is fine; mix_hitl has 10 requests at p95 900ms (gate 500).
  const streams = [
    makeStream("agent-0", { measured: 100 }) +
    makeStream("agent-0", { measured: 10, perReqMs: 900, scenario: "mix_hitl" }),
  ];
  const dir = makeRun("scenfail", { streams });
  try {
    const s = generate(dir);
    assert.equal(s.verdict, "FAIL");
    assert.ok(s.gate_failures.some(g => g.includes("mix_hitl")));
  } finally { cleanup(dir); }
});

test("measured errors in a scenario fail the run", () => {
  const streams = [makeStream("agent-0", { measured: 50, errors: 3 })];
  const dir = makeRun("errs", { streams });
  try {
    const s = generate(dir);
    assert.equal(s.verdict, "FAIL");
    assert.ok(s.gate_failures.some(g => g.includes("errors")));
  } finally { cleanup(dir); }
});

// --- 5: CPU window starts at the measured boundary ------------------------
test("CPU selection window opens at t0 (no warmup added)", () => {
  // This is pinned structurally: the summary records the selected window
  // and it must start at the stream's first measured timestamp.
  const dir = makeRun("cpuwin", {});
  try {
    const s = generate(dir);
    // synthetic stream's t0 is 10:00:30 (measured phase starts there)
    assert.equal(s.cpu.window_utc[0].startsWith("2026-11-17T10:00:30"), true,
      `window start ${s.cpu.window_utc[0]}`);
  } finally { cleanup(dir); }
});

// --- third review, finding 1: success evidence completeness --------------
test("missing measured success evidence is INVALID despite 100% log success", () => {
  // stream carries durations for all scenarios but NO success points;
  // log claims 100%. The measured evidence is what gates.
  const streamNoSuccess = makeStream("agent-0")
    .split("\n")
    .filter(l => !l.includes("sideband_decision_success"))
    .join("\n");
  const dir = makeRun("nosucc", {
    streams: [streamNoSuccess],
    logs: [makeLog("agent-0", { successPct: 100.0 })],
  });
  try {
    const s = generate(dir);
    assert.equal(s.verdict, "INVALID");
    assert.ok(s.gate_failures.some(g => g.includes("success evidence incomplete")));
  } finally { cleanup(dir); }
});

test("success sample count mismatched to requests is INVALID", () => {
  // half the success points present
  const streamHalfSuccess = makeStream("agent-0")
    .split("\n")
    .filter((l, i) => !l.includes("sideband_decision_success") || i % 2 === 0)
    .join("\n");
  const dir = makeRun("halfsucc", { streams: [streamHalfSuccess] });
  try {
    const s = generate(dir);
    assert.equal(s.verdict, "INVALID");
  } finally { cleanup(dir); }
});

// --- third review, finding 3: phase strictness ---------------------------
test("non-measured phase values (invalid/empty/Misspelled) never count as measured", () => {
  const badPhases = ["invalid", "", "Measured", "MEASURED"];
  for (const ph of badPhases) {
    const stream = makeStream("agent-0", { phase: ph });
    const dir = makeRun(`ph-${ph || "empty"}`.replace(/[^a-z0-9-]/gi, ""), { streams: [stream] });
    try {
      const s = generate(dir);
      // zero measured requests from garbage-phase streams -> the run cannot
      // pass; INVALID via missing evidence (zero measured -> scenario gates)
      assert.notEqual(s.verdict, "PASS", `phase=${JSON.stringify(ph)} counted as measured`);
    } finally { cleanup(dir); }
  }
});

// --- third review, finding 2: execution evidence -------------------------
test("nonzero k6 exit in the log fails even without per-agent codes", () => {
  const dir = makeRun("logexit", {
    env: { job_terminal_state: undefined },
    logs: [makeLog("agent-0", { exitCode: 99 })],
  });
  try {
    const s = generate(dir);
    assert.equal(s.verdict, "FAIL");
    assert.ok(s.gate_failures.some(g => g.includes("k6 exited 99")));
  } finally { cleanup(dir); }
});

test("no exit evidence at all is INVALID (presence of files is not success)", () => {
  const dir = makeRun("noexit", {
    env: { job_terminal_state: undefined },
    logs: [makeLog("agent-0", { exitCode: 0 })], // log even says 0 — not independent evidence
  });
  try {
    const s = generate(dir);
    assert.equal(s.verdict, "INVALID");
    assert.ok(s.gate_failures.some(g => g.includes("execution evidence unknown")));
  } finally { cleanup(dir); }
});
