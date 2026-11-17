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
// WITH a `response` object is a DENY (the trap: agentgateway's extAuthz
// treats every 2xx as allow — this loader must not). Anything that is not a
// well-formed sideband response is an ERROR — transport failures, non-200
// statuses, and 200s whose body does not parse to an object — and must
// never be scored as an authorization decision in either direction
// (2026-11-17 review, finding 3: !isPermit() conflated all three).
export function classify(statusCode, parsedBody) {
  if (statusCode === 200) {
    if (parsedBody && typeof parsedBody === "object" && !Array.isArray(parsedBody)) {
      return parsedBody.response ? "deny" : "permit";
    }
    return "error"; // malformed body on a 200: not a decision
  }
  return "error"; // transport failure / integration error / non-200
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
export function buildClaims({ userIndex, iteration, issuer, mcpAud, userCount = 100, runId = "", agentIndex = 0 }) {
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
    jti: `${runId}-${agentIndex}-${userIndex}-${iteration}-${now}`,
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
