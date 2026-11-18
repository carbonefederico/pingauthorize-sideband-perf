// envelope.mjs — sideband envelope construction, decision parsing, and the
// claim/rate helpers. Pure module, imported by the k6 script (inside the
// cluster), the unit tests, and the smoke test (Node). The envelope schema
// mirrors a gateway-side extAuthz adapter's buildSidebandEnvelope exactly —
// PAZ's sideband endpoint expects this shape, and field drift
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

// normalizeResponseCode(value) -> integer 100..599, or null.
// PAZ serializes the embedded response_code either as a number or as an
// exact three-digit string ("401" — the smoke test's garbage-token decision
// proves the string form occurs in practice). One normalizer for every
// comparison so both serializations score identically. Accepts an integer
// HTTP status code or an exact three-digit numeric string; rejects null,
// fractional values (499.5), malformed strings ("40x", "0403", ""), and
// out-of-range codes (99, 600, 999) — anything ambiguous or impossible is
// null, and callers treat null as malformed evidence.
export function normalizeResponseCode(value) {
  const n = typeof value === "number" ? value
    : (typeof value === "string" && /^[0-9]{3}$/.test(value)) ? Number(value)
    : null;
  if (n === null || !Number.isInteger(n) || n < 100 || n > 599) {
    return null;
  }
  return n;
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
// A well-formed embedded denial carries an object `response` whose
// `response_code` normalizes to a valid HTTP status (integer, or PAZ's
// string serialization of one). A `response` that is a string, an empty
// object, or null is ambiguous evidence — error.
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
  const code = normalizeResponseCode(r.response_code);
  // response_code must normalize to a valid HTTP status (2026-11-17 fifth
  // review, finding 1: both serializations score identically; 499.5, "40x",
  // and out-of-range values are malformed). A denial is an HTTP 4xx verdict
  // — anything else (2xx/3xx/5xx embedded "denials") is a server fault or
  // malformed evidence, never a decision.
  if (code === null) {
    return "error";
  }
  if (code < 400 || code >= 500) {
    return "error"; // non-4xx embedded verdicts: server fault or malformed
  }
  return "deny";
}

// isExpectedDenial(parsedBody, expectedDenialCode) -> boolean. True only
// when the body carries a well-formed denial whose NORMALIZED response_code
// equals the fixture's expected code (third review, finding 4: "any denial"
// would let a fixture pass for the wrong reason). A valid-but-unexpected
// denial — right shape, wrong rule, e.g. an authentication 401 where the
// ladder's 403 belongs — returns false and fails the outcome check. The
// normalization is the same one classify() applies, so string-coded "403"
// matches an expected 403 exactly as the numeric form does (fifth review,
// finding 1).
export function isExpectedDenial(parsedBody, expectedDenialCode) {
  if (typeof expectedDenialCode !== "number" || !Number.isInteger(expectedDenialCode)) {
    return false; // the fixture must name a concrete code
  }
  const r = parsedBody && typeof parsedBody === "object" && !Array.isArray(parsedBody)
    ? parsedBody.response
    : null;
  if (!r || typeof r !== "object" || Array.isArray(r)) {
    return false;
  }
  return normalizeResponseCode(r.response_code) === expectedDenialCode;
}

// isPermit(statusCode, parsedBody) -> boolean. Kept for the unit tests and
// any caller that only needs the permit/deny dichotomy; the load path uses
// classify() so errors stay out of the decision statistics.
export function isPermit(statusCode, parsedBody) {
  return classify(statusCode, parsedBody) === "permit";
}

// buildClaims({ userIndex, iteration, issuer, mcpAud, userCount, runId, agentIndex })
//   -> the subject-token claim set matching the policy ladder's permit
//   fixture, with `sub` rotating through synthetic VIP users and a fresh
//   jti per call so no token is ever replayed. jti includes the run id and
//   agent index (2026-11-17 review, finding 6): k6 processes run
//   independent VU and iteration counters, so (vu, iter, second) alone
//   collides across agents and yields byte-identical tokens.
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
