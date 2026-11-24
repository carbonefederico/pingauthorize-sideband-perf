// probe-branch.mjs — one real decision against PAZ, one JSON line of output.
// The tree-identity probe used by swap-policy.sh: a valid vip-user-001 token
// minted for MCP_AUD, POSTed as a sideband envelope. Prints
//   {"verdict":"permit"|"deny","code":<int|null>}
// where code is the embedded response_code of a denial (null on permit).
// Exit 0 always — assertions live in the caller. Missing env also exits 0,
// with {"error": "..."} instead of a verdict, so a wiring mistake can never
// read as a probe success.
import { signToken } from "./smoke-helpers.mjs";
import { buildSidebandEnvelope, buildClaims, classify, normalizeResponseCode } from "../helm/loadtest/files/envelope.mjs";

const base = {
  sidebandUrl: process.env.PAZ_SIDEBAND_URL,
  secret: process.env.SIDEBAND_SHARED_SECRET,
  issuer: process.env.SUBJECT_ISSUER,
  mcpAud: process.env.MCP_AUD,
  signingKeyPem: process.env.SUBJECT_SIGNING_KEY_PEM,
};

const missing = Object.entries(base).filter(([, v]) => !v).map(([k]) => k);
if (missing.length > 0) {
  console.log(JSON.stringify({ error: `missing env: ${missing.join(", ")}` }));
  process.exit(0);
}

const claims = buildClaims({ userIndex: 0, iteration: 1, issuer: base.issuer, mcpAud: base.mcpAud });
const token = await signToken(base.signingKeyPem, claims);
const envelope = buildSidebandEnvelope({
  method: "POST",
  url: base.mcpAud,
  headers: {
    "content-type": "application/json",
    accept: "application/json, text/event-stream",
    authorization: `Bearer ${token}`,
    host: base.mcpAud.replace(/^https?:\/\//, "").split("/")[0],
  },
  body: JSON.stringify({
    jsonrpc: "2.0",
    method: "tools/call",
    params: {
      name: "get_portfolio_summary",
      arguments: { customerId: "CUST-00001", txn: "txn-probe" },
    },
    id: 1,
  }),
  sourceIp: "10.0.0.9",
  sourcePort: "41000",
  httpVersion: "1.1",
});

try {
  const response = await fetch(base.sidebandUrl, {
    method: "POST",
    headers: { "content-type": "application/json", "PDG-TOKEN": base.secret },
    body: JSON.stringify(envelope),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  const verdict = classify(response.status, payload);
  const code = verdict === "deny"
    ? normalizeResponseCode(payload?.response?.response_code)
    : null;
  console.log(JSON.stringify({ verdict, code }));
} catch (err) {
  console.log(JSON.stringify({ error: String(err) }));
}
