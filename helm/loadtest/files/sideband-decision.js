import http from 'k6/http';
import encoding from 'k6/encoding';
import { check } from 'k6';
import exec from 'k6/execution';
import { Rate, Trend, Counter } from 'k6/metrics';
// NOTE: no `import crypto from 'k6/crypto'` — that legacy module shadows the
// global WebCrypto `crypto` object that provides `crypto.subtle`.
// envelope.mjs rides in the same ConfigMap and supplies the envelope, the
// claim set, the decision classifier, and the rate distribution guard.

import { buildSidebandEnvelope, buildClaims, classify, distributeRate } from './envelope.mjs';

// Phase-scoped metrics: ramp traffic must not gate the measured thresholds
// (2026-11-17 review, finding 4). The phase tag is applied per request below
// AND carried on every custom metric, and the thresholds select
// {phase:measured} explicitly. `elapsed` additionally covers connection
// establishment (finding 7) — see decide().
const successRate = new Rate('sideband_decision_success');
const latency = new Trend('sideband_decision_latency', true);
const elapsed = new Trend('sideband_request_elapsed', true);
const denials = new Counter('sideband_denials');
const errors = new Counter('sideband_errors');

const required = [
  'PAZ_SIDEBAND_URL', 'SIDEBAND_SHARED_SECRET', 'MCP_AUD',
  'SUBJECT_ISSUER', 'SUBJECT_SIGNING_KEY',
];
for (const key of required) {
  if (!__ENV[key]) {
    throw new Error(`Missing required environment variable: ${key}`);
  }
}

const totalRate = Number(__ENV.TOTAL_RATE || __ENV.RATE || 50);
const agentCount = Number(__ENV.AGENT_COUNT || 1);
const agentIndex = Number(__ENV.AGENT_INDEX || 0);
if (agentIndex >= agentCount) {
  throw new Error(`AGENT_INDEX (${agentIndex}) must be less than AGENT_COUNT (${agentCount})`);
}
// Each agent drives its share of the total rate; drift over 5% fails loudly
// (envelope.mjs) instead of silently shedding or overloading.
const rate = distributeRate(totalRate, agentCount);

// Weighted traffic mix over the policy tree's four decision paths. The
// weights split each agent's per-second rate; with the campaign rates
// (100/250/500/1000 per 10 agents -> 10/25/50/100 per agent) every split is
// an integer, and distributeRate's drift guard runs per scenario too.
const MIX = {
  read: 0.6,     // read-permit path: the cheap 4-rule ladder
  session: 0.15, // session methods: initialize/tools-list/ping tool gate
  hitl: 0.15,    // order submission with HITL approval token: 10-rule ladder
  deny: 0.1,     // expected denials: full ladder walked to a deny verdict
};
// Split THIS AGENT's rate; whole-iteration rounding drifts are caught by the
// sum check below (fractional weights can round badly at small rates).
function splitRate(fraction) {
  return Math.round(rate * fraction);
}
const readRate = splitRate(MIX.read);
const sessionRate = splitRate(MIX.session);
const hitlRate = splitRate(MIX.hitl);
// Denials take the remainder: three independent roundings must not break the
// sum, and the deny mix tolerates the off-by-one a remainder can produce.
const denyRate = rate - readRate - sessionRate - hitlRate;
if (readRate + sessionRate + hitlRate + denyRate !== rate) {
  throw new Error(
    `Mix rates (${readRate}+${sessionRate}+${hitlRate}+${denyRate}) do not sum to the agent rate ${rate}`,
  );
}

const preAllocatedVUs = Number(__ENV.PRE_ALLOCATED_VUS || 25);
const maxVUs = Number(__ENV.MAX_VUS || 200);
const p95Ms = Number(__ENV.P95_MS || 500);
const minimumSuccessRate = Number(__ENV.SUCCESS_RATE || 0.99);
const userCount = Number(__ENV.SUBJECT_USER_COUNT || 100);
const subjectIssuer = __ENV.SUBJECT_ISSUER;
const mcpAud = __ENV.MCP_AUD;
const sidebandUrl = __ENV.PAZ_SIDEBAND_URL;
const sharedSecret = __ENV.SIDEBAND_SHARED_SECRET;
const requestTimeout = __ENV.REQUEST_TIMEOUT || '30s';

// Phase boundary: ramp traffic (first WARMUP seconds of each SCENARIO) is
// warmup, the plateau after it is the measured window. The boundary is
// computed from exec.scenario.startTime — the actual scenario start of the
// VU's current scenario — NOT from a Date.now() captured in init context:
// k6 re-runs init per VU (dynamically allocated VUs included), so an
// init-time clock skews per VU (2026-11-17 review, finding 4).
const WARMUP_MS = (() => {
  const m = String(__ENV.WARMUP || '30s').match(/^([\d.]+)(ms|s|m)?$/);
  if (!m) return 30000;
  return parseFloat(m[1]) * (m[2] === 'm' ? 60000 : m[2] === 'ms' ? 1 : 1000);
})();

// Phase is assigned at HTTP-call start (the moment decide() issues the
// request), consistently for the HTTP metric, the custom metrics, and the
// check — one rule for all three.
function currentPhase() {
  const scenarioStartMs = exec.scenario.startTime;
  return Date.now() - scenarioStartMs < WARMUP_MS ? 'warmup' : 'measured';
}

// The MCP audience doubles as the envelope URL's origin: the policies gate
// on this resource identifier, so the envelope must present it. k6's init
// context has no URL global — derive the host by string surgery.
const mcpHost = mcpAud.replace(/^https?:\/\//, '').split('/')[0];
const envelopeUrl = mcpAud;

// k6's WebCrypto needs the DER bytes of the PKCS#8 key: strip PEM armor and
// base64-decode once at init time.
const pkcs8Der = new Uint8Array(
  encoding.b64decode(
    __ENV.SUBJECT_SIGNING_KEY
      .replace(/-----[^-]+-----/g, '')
      .replace(/\s+/g, ''),
    'std',
  ),
);
let signingKeyPromise = null;
function getSigningKey() {
  if (!signingKeyPromise) {
    signingKeyPromise = crypto.subtle.importKey(
      'pkcs8',
      pkcs8Der,
      { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
      false,
      ['sign'],
    );
  }
  return signingKeyPromise;
}

function b64urlJson(object) {
  return encoding.b64encode(JSON.stringify(object), 'rawurl');
}

// Build and sign one RS256 subject token (kid perf-subject-key — the name of
// the JWK the PAZ validator fetches from the in-cluster JWKS server).
// extraClaims merges scenario-specific claims (HITL approval binding, denial
// negatives) over the base VIP fixture before signing.
async function buildSubjectToken(vu, iter, extraClaims = {}) {
  const claims = {
    ...buildClaims({
      userIndex: (vu + iter) % userCount,
      iteration: iter,
      issuer: subjectIssuer,
      mcpAud,
      userCount,
      // Run id + agent index make jti unique across the ten independent k6
      // processes (2026-11-17 review, finding 6): identical (vu, iter, now)
      // in two agents would otherwise produce byte-identical tokens.
      runId: __ENV.RUN_ID || '',
      agentIndex,
    }),
    ...extraClaims,
  };
  const signingInput = `${b64urlJson({ alg: 'RS256', typ: 'JWT', kid: 'perf-subject-key' })}.${b64urlJson(claims)}`;
  const key = await getSigningKey();
  const signature = await crypto.subtle.sign(
    'RSASSA-PKCS1-v1_5',
    key,
    new TextEncoder().encode(signingInput),
  );
  return `${signingInput}.${encoding.b64encode(signature, 'rawurl')}`;
}

// One JSON-RPC tools/call body. toolName selects the policy branch; for
// submit_trade_order the arguments carry the order shape the HITL rule
// mirrors into the token's tctx block (customerId/symbol/side/quantity/
// amount/txn) — the approval binding compares them 1:1.
function rpcBody(userIndex, iter, toolName = 'get_portfolio_summary') {
  if (toolName === 'submit_trade_order') {
    const customerId = `CUST-${String((userIndex % userCount) + 1).padStart(5, '0')}`;
    const order = {
      customerId,
      symbol: 'ACME',
      side: 'SELL',
      quantity: 3,
      orderType: 'MARKET',
      confirmedByUser: true,
      amount: 3000,
      txn: `txn-${iter}`,
    };
    return JSON.stringify({
      jsonrpc: '2.0',
      method: 'tools/call',
      params: { name: toolName, arguments: order },
      id: iter,
    });
  }
  return JSON.stringify({
    jsonrpc: '2.0',
    method: 'tools/call',
    params: {
      name: toolName,
      arguments: {
        customerId: `CUST-${String((userIndex % userCount) + 1).padStart(5, '0')}`,
        txn: `txn-${iter}`,
      },
    },
    id: iter,
  });
}

export const options = {
  insecureSkipTLSVerify: (__ENV.INSECURE_SKIP_TLS_VERIFY || 'false') === 'true',
  discardResponseBodies: false,
  scenarios: {
    // No separate warmup scenario: each measured scenario's ramp serves as
    // its own warmup (see the ramping comment below). The former
    // constant-rate warmup + step transition is what produced the 2026-11-16
    // stage-2000 transition stalls.
    // Measured: the weighted mix. Four arrival-rate scenarios share the
    // agent's rate; each carries its own tags so the report attributes
    // latency and outcomes per policy path.
    //
    // Ramped start, not constant: a constant-arrival measured window opens
    // at full rate in one step, so its first second's arrivals hit PAZ as
    // one wave of fresh handshakes. Every 2026-11-16 stage-2000 run stalled
    // exactly at this boundary (8s on a days-old JVM, 22s on a minutes-old
    // one, ~40 drops after the warmup rotation spread path warmth) while
    // the same rate in steady state never stalled once load was flowing;
    // 1500/s absorbed its smaller burst. The ramp below (WARMUP long,
    // reaching full rate exactly when the measured window opens) spreads
    // connection establishment across the ramp, so offered load never
    // exceeds the steady-state rate the plateau then holds.
    //
    // The phase tag is set per request inside decide() (ramp = warmup,
    // plateau = measured) from the run's wall clock, not statically on the
    // scenario: a static tag applies to the WHOLE scenario including its
    // plateau, which blinded the report's stream parser to every measured
    // datapoint of the ramped runs (2026-11-16 run F). The boundary is the
    // scenario start + WARMUP seconds; REQUEST_START_MS is set once at k6
    // init. Only the plateau is the measured window.
    mix_read: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      stages: [
        { target: readRate, duration: __ENV.WARMUP || '30s' },
        { target: readRate, duration: __ENV.DURATION || '5m' },
      ],
      preAllocatedVUs,
      maxVUs,
      gracefulStop: '30s',
      startTime: '0s',
      exec: 'readIteration',
      tags: { policy_path: 'read_permit' },
    },
    mix_session: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      stages: [
        { target: sessionRate, duration: __ENV.WARMUP || '30s' },
        { target: sessionRate, duration: __ENV.DURATION || '5m' },
      ],
      preAllocatedVUs,
      maxVUs,
      gracefulStop: '30s',
      startTime: '0s',
      exec: 'sessionIteration',
      tags: { policy_path: 'session_permit' },
    },
    mix_hitl: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      stages: [
        { target: hitlRate, duration: __ENV.WARMUP || '30s' },
        { target: hitlRate, duration: __ENV.DURATION || '5m' },
      ],
      preAllocatedVUs,
      maxVUs,
      gracefulStop: '30s',
      startTime: '0s',
      exec: 'hitlIteration',
      tags: { policy_path: 'order_hitl_permit' },
    },
    mix_deny: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      stages: [
        { target: denyRate, duration: __ENV.WARMUP || '30s' },
        { target: denyRate, duration: __ENV.DURATION || '5m' },
      ],
      preAllocatedVUs,
      maxVUs,
      gracefulStop: '30s',
      startTime: '0s',
      exec: 'denyIteration',
      tags: { policy_path: 'expected_deny' },
    },
  },
  thresholds: {
    // Latency and success gates apply to each MEASURED scenario's MEASURED
    // phase only: sub-metrics scoped by scenario AND phase (the custom
    // metrics carry the per-request phase tag since the 2026-11-17 review,
    // finding 4 — without it ramp traffic gated the thresholds). Warmup
    // traffic exists to prime the platform and must not gate.
    'sideband_decision_latency{scenario:mix_read,phase:measured}': [`p(95)<${p95Ms}`],
    'sideband_decision_latency{scenario:mix_session,phase:measured}': [`p(95)<${p95Ms}`],
    'sideband_decision_latency{scenario:mix_hitl,phase:measured}': [`p(95)<${p95Ms}`],
    'sideband_decision_latency{scenario:mix_deny,phase:measured}': [`p(95)<${p95Ms}`],
    'sideband_decision_success{scenario:mix_read,phase:measured}': [`rate>=${minimumSuccessRate}`],
    'sideband_decision_success{scenario:mix_session,phase:measured}': [`rate>=${minimumSuccessRate}`],
    'sideband_decision_success{scenario:mix_hitl,phase:measured}': [`rate>=${minimumSuccessRate}`],
    // The deny scenario's "success" is landing the EXPECTED denial; a permit
    // there is a policy failure, scored the same way. Errors (malformed
    // responses, transport failures) never count as successes in any
    // scenario — classify() keeps them out of the decision statistics, and
    // the error counter below fails the run outright.
    'sideband_decision_success{scenario:mix_deny,phase:measured}': [`rate>=${minimumSuccessRate}`],
    // Any integration error in the measured window fails the run: an error
    // is neither a permit nor an expected denial (2026-11-17 review,
    // finding 3).
    'sideband_errors{phase:measured}': ['count==0'],
    'http_req_failed': ['rate<0.01'],
    // Dropped iterations invalidate a stress stage: when required concurrency
    // (rate x latency) exceeds maxVUs, k6 silently sheds load and latency
    // looks fine because the excess requests were never sent. Any drop
    // fails the stage.
    dropped_iterations: ['count==0'],
  },
  tags: {
    system_under_test: 'pingauthorize-sideband',
    operation: 'sideband_decision',
  },
};

// The ramped scenarios serve as their own warmup (see the scenario comments),
// so warmupIteration below is dead code kept only for reference — no scenario
// references it anymore. The four-path rotation it implements lives on in
// spirit: the ramp itself exercises all four paths at increasing rate.
export async function warmupIteration() {
  switch (__ITER % 4) {
    case 0:
      await doRead(__VU, __ITER);
      break;
    case 1:
      await sessionIteration();
      break;
    case 2:
      await hitlIteration();
      break;
    default:
      await denyIteration();
      break;
  }
}

export async function readIteration() {
  await doRead(__VU, __ITER);
}

// Session methods hit the Allow Session Methods branch: same token shape,
// but the JSON-RPC method itself is the session method (MCP Method resolves
// from $.method of the body — initialize/tools/list/ping are TOP-LEVEL
// jsonrpc methods, not tools/call names).
export async function sessionIteration() {
  const token = await buildSubjectToken(__VU, __ITER);
  const body = JSON.stringify({
    jsonrpc: '2.0',
    method: 'tools/list',
    id: __ITER,
  });
  await decide({
    scenario: 'mix_session',
    token,
    body,
    expectPermit: true,
  });
}

// HITL order path: the token binds the approval to THIS order (approved_for
// + txn + the full tctx mirror of the order body), the body submits a 3k
// order inside the approval band. All ten Allow-Orders comparisons must pass.
export async function hitlIteration() {
  const userIndex = (__VU + __ITER) % userCount;
  const customerId = `CUST-${String(userIndex + 1).padStart(5, '0')}`;
  const txn = `txn-${__ITER}`;
  const order = {
    customerId,
    symbol: 'ACME',
    side: 'SELL',
    quantity: 3,
    orderType: 'MARKET',
    confirmedByUser: true,
    amount: 3000,
    txn,
  };
  const token = await buildSubjectToken(__VU, __ITER, {
    approved_for: 'submit_trade_order',
    txn,
    tctx: {
      tool: 'submit_trade_order',
      customerId,
      symbol: 'ACME',
      side: 'SELL',
      quantity: 3,
      amount: 3000,
    },
  });
  await decide({
    scenario: 'mix_hitl',
    token,
    body: JSON.stringify({
      jsonrpc: '2.0',
      method: 'tools/call',
      params: { name: 'submit_trade_order', arguments: order },
      id: __ITER,
    }),
    expectPermit: true,
  });
}

// Expected denials rotate through the ladder's negative fixtures: an
// undeclared acting agent (R2), a token without act.sub (R2 vacuous), an
// oversized order (Deny Oversized), and a wrong-audience token (R1). Each
// MUST come back denied — a permit is a policy failure.
export async function denyIteration() {
  const which = (__VU + __ITER) % 4;
  let token;
  let body;
  switch (which) {
    case 0: // undeclared acting agent -> R2 denies
      token = await buildSubjectToken(__VU, __ITER, { act: { sub: 'marketing_agent' } });
      body = rpcBody(__VU, __ITER, 'get_portfolio_summary');
      break;
    case 1: // no act.sub at all -> R2 denies (agent identity mismatch)
      token = await buildSubjectToken(__VU, __ITER, { act: undefined });
      body = rpcBody(__VU, __ITER, 'get_portfolio_summary');
      break;
    case 2: // oversized order (50k > 20k threshold) -> Deny Oversized
      token = await buildSubjectToken(__VU, __ITER, {
        approved_for: 'submit_trade_order',
        txn: `txn-${__ITER}`,
        tctx: {
          tool: 'submit_trade_order',
          customerId: `CUST-${String(((__VU + __ITER) % userCount) + 1).padStart(5, '0')}`,
          symbol: 'ACME',
          side: 'SELL',
          quantity: 100,
          amount: 50000,
        },
      });
      body = rpcBody(__VU, __ITER, 'submit_trade_order').replace('"amount":3000', '"amount":50000').replace('"quantity":3', '"quantity":100');
      break;
    default: // wrong audience (gateway root, not the MCP resource) -> R1 denies
      token = await buildSubjectToken(__VU, __ITER, {
        aud: mcpAud.replace(/\/mcp\/portfolio$/, ''),
      });
      body = rpcBody(__VU, __ITER, 'get_portfolio_summary');
      break;
  }
  await decide({ scenario: 'mix_deny', token, body, expectPermit: false });
}

export default async function () {
  await doRead(__VU, __ITER);
}

async function doRead(vu, iter) {
  const token = await buildSubjectToken(vu, iter);
  await decide({ scenario: 'mix_read', token, body: rpcBody(vu, iter), expectPermit: true });
}

async function decide({ scenario, token, body, expectPermit }) {
  const envelope = buildSidebandEnvelope({
    method: 'POST',
    url: envelopeUrl,
    headers: {
      'content-type': 'application/json',
      accept: 'application/json, text/event-stream',
      authorization: `Bearer ${token}`,
      host: mcpHost,
    },
    body,
    sourceIp: `10.0.0.${(__VU % 250) + 1}`,
    sourcePort: 40000 + (__ITER % 20000),
    httpVersion: '1.1',
  });

  // Dynamic phase tag: the ramped scenarios run ramp (warmup) and plateau
  // (measured) inside ONE scenario, so the phase must be decided per
  // request from the scenario clock — a static scenario tag labels the
  // plateau warmup too and blinds the report's measured-window parser.
  // One assignment rule for the HTTP metric, all custom metrics, and the
  // check: phase is read at HTTP-call start.
  const phase = currentPhase();
  const params = {
    headers: {
      'Content-Type': 'application/json',
      'PDG-TOKEN': sharedSecret,
    },
    tags: { name: 'POST /sideband/request', phase },
    timeout: requestTimeout,
  };

  const wallStart = Date.now();
  const response = http.post(sidebandUrl, JSON.stringify(envelope), params);
  // latency = k6's timings.duration (sending + waiting + receiving; the
  // steady-state connection is reused, so TCP/TLS setup is amortized to the
  // first iterations of each VU's connection). elapsed = the full call wall
  // time INCLUDING connection establishment — reported separately so the
  // connection-setup share stays visible (2026-11-17 review, finding 7).
  latency.add(response.timings.duration, { phase });
  elapsed.add(Date.now() - wallStart, { phase });

  let payload = null;
  try {
    payload = response.json();
  } catch (_) {
    // classify() scores the unparseable body as an error below.
  }

  // Three-way classification (2026-11-17 review, finding 3): permits and
  // denials are authorization decisions; malformed responses, transport
  // failures, and non-200s are ERRORS. Errors never count toward decision
  // success — an error in the deny scenario is not a successful expected
  // denial, it is a generator/SUT fault.
  const verdictKind = classify(response.status, payload);
  const expected = verdictKind === 'error'
    ? false
    : (expectPermit ? verdictKind === 'permit' : verdictKind === 'deny');
  check(response, {
    'PAZ returned the expected verdict': () => expected,
  });
  successRate.add(expected, { phase });
  if (verdictKind === 'deny') denials.add(1, { phase });
  if (verdictKind === 'error') errors.add(1, { phase });
}
