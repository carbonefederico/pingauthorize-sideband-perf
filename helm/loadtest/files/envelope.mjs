// envelope.mjs — sideband envelope construction, decision parsing, and the
// claim/rate helpers. Pure module, imported by the k6 script (inside the
// cluster), the unit tests, and the smoke test (Node). The envelope schema
// mirrors the sibling project's paz-extauthz-adapter buildSidebandEnvelope
// exactly — PAZ's sideband endpoint expects this shape, and field drift
// yields silently vacuous decisions (NOT_APPLICABLE), not errors.

// buildSidebandEnvelope({ method, url, headers, body, sourceIp, sourcePort, httpVersion })
//   -> the PAZ sideband request envelope.
//   headers: plain object; rendered as an array of single-key objects
//            preserving the caller's key casing.
//   body:    string (PAZ expects the raw body as a string).
//   httpVersion: bare protocol version ("1.1"), never "HTTP/1.1".
//   sourcePort: string (text, per the sideband schema).
export function buildSidebandEnvelope({
  method = "POST",
  url,
  headers = {},
  body = null,
  sourceIp = null,
  sourcePort = null,
  httpVersion = "1.1",
}) {
  return {
    source_ip: sourceIp ?? null,
    source_port: sourcePort != null ? String(sourcePort) : null,
    method,
    url,
    http_version: String(httpVersion).replace(/^HTTP\//, ""),
    headers: Object.entries(headers).map(([name, value]) => ({ [name]: String(value) })),
    body,
  };
}

// classify(statusCode, parsedBody) -> "permit" | "deny" | "error".
// PAZ signals permit as HTTP 200 with NO embedded `response` object; a 200
// WITH a well-formed `response` object is a DENY (the trap: agentgateway's
// extAuthz treats every 2xx as allow — this loader must not). Everything
// else is an ERROR: transport failures, non-200s, malformed bodies, and
// embedded server faults (2026-11-17 follow-up review, finding 3 — an
// embedded response_code 500 or an {"error": ...} body is a SUT fault, not
// an authorization denial, and never scores as a decision in either
// direction).
//
// A well-formed embedded denial carries an object `response` with a numeric
// `response_code` (the sideband adapter's decision echo). A `response` that
// is a string, an empty object, or null is ambiguous evidence — error.
export function classify(statusCode, parsedBody) {
  if (statusCode !== 200) {
    return "error"; // transport failure / integration error / non-200
  }
  if (!parsedBody || typeof parsedBody !== "object" || Array.isArray(parsedBody)) {
    return "error"; // unparseable / non-object body on a 200
  }
  if ("error" in parsedBody) {
    return "error"; // embedded integration error (e.g. {"error": "..."})
  }
  if (!("response" in parsedBody) || parsedBody.response === null) {
    // No `response` key at all: a permit. A present-but-null `response` is
    // ambiguous (the field exists but carries nothing) — malformed evidence.
    return "response" in parsedBody ? "error" : "permit";
  }
  const r = parsedBody.response;
  if (typeof r !== "object" || Array.isArray(r) || !("response_code" in r)) {
    return "error"; // response without a response_code: not a well-formed denial
  }
  const code = r.response_code;
  if (typeof code !== "number" || !Number.isFinite(code)) {
    return "error"; // non-numeric response_code
  }
  // Embedded HTTP-error codes (5xx) are server faults, not denials.
  if (code >= 500) {
    return "error";
  }
  return "deny";
}

// isPermit(statusCode, parsedBody) -> boolean. Kept for the unit tests and
// any caller that only needs the permit/deny dichotomy; the load path uses
// classify() so errors stay out of the decision statistics.
export function isPermit(statusCode, parsedBody) {
  return classify(statusCode, parsedBody) === "permit";
}

// buildClaims({ userIndex, iteration, issuer, mcpAud, userCount, runId, agentIndex })
//   -> the subject-token claim set: the sibling project's permit fixture,
//   with `sub` rotating through synthetic VIP users and a fresh jti per
//   call so no token is ever replayed. jti includes the run id and agent
//   index (2026-11-17 review, finding 6): k6 processes run independent VU
//   and iteration counters, so (vu, iter, second) alone collides across
//   agents and yields byte-identical tokens.
export function buildClaims({ userIndex, iteration, issuer, mcpAud, userCount = 100, runId = "", agentIndex = 0, vuId = 0, scenario = "" }) {
  const now = Math.floor(Date.now() / 1000);
  const user = String((userIndex % userCount) + 1).padStart(3, "0");
  return {
    iss: issuer,
    sub: `vip-user-${user}`,
    sub_type: "vip_user",
    client_id: "customer_support_agent",
    aud: mcpAud,
    scope: "portfolio:read portfolio:write",
    act: { sub: "customer_support_agent" },
    iat: now,
    exp: now + 300,
    // Request uniqueness is separate from user-population selection:
    // userIndex wraps modulo userCount (the policy's VIP fixture), while
    // jti carries the FULL VU id (not the wrapped user index) plus run,
    // agent, scenario, and iteration — VU 1 and VU 101 share a userIndex
    // but must never share a token (2026-11-17 follow-up review, finding 6).
    jti: `${runId}-${agentIndex}-${vuId}-${scenario}-${iteration}-${now}`,
  };
}

// distributeRate(totalRate, agentCount) -> per-agent rate.
// Whole-iteration rounding can distort the aggregate (25/10 -> 3/agent = 30
// total, +20%); fail loudly instead of silently shedding or overloading.
export function distributeRate(totalRate, agentCount) {
  const rate = Math.max(1, Math.round(totalRate / agentCount));
  const drift = Math.abs(rate * agentCount - totalRate) / totalRate;
  if (drift > 0.05) {
    throw new Error(
      `Rate cannot be distributed: ${totalRate}/s over ${agentCount} agents rounds to ` +
      `${rate * agentCount}/s (${(drift * 100).toFixed(1)}% drift > 5%). ` +
      `Choose RATE divisible by AGENTS, or fewer agents.`,
    );
  }
  return rate;
}
