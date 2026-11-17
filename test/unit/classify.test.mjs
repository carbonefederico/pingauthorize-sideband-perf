import { test } from "node:test";
import assert from "node:assert/strict";
import { classify, buildClaims } from "../../helm/loadtest/files/envelope.mjs";

// 2026-11-17 review, finding 3: errors must be separated from authorization
// decisions. A malformed body, a transport failure, or a non-200 is an
// ERROR — never a permit, never a (successful) expected denial.

test("200 with an unparseable body (null payload) is an error, not a permit", () => {
  assert.equal(classify(200, null), "error");
});

test("200 with a string body is an error, not a permit", () => {
  assert.equal(classify(200, "OK"), "error");
});

test("200 with an array body is an error, not a permit", () => {
  assert.equal(classify(200, []), "error");
});

test("200 without an embedded response object is a permit", () => {
  assert.equal(classify(200, {}), "permit");
});

test("200 with an embedded response object is a deny", () => {
  assert.equal(classify(200, { response: { response_code: 403 } }), "deny");
});

test("transport failure (status 0) is an error, not a denial", () => {
  assert.equal(classify(0, null), "error");
});

test("HTTP 500 is an error, not a successful expected denial", () => {
  assert.equal(classify(500, null), "error");
});

test("HTTP 502 with a body is an error", () => {
  assert.equal(classify(502, { message: "bad gateway" }), "error");
});

// 2026-11-17 review, finding 6: jti must be unique across agents — two k6
// processes with the same VU number and iteration inside the same second
// previously produced byte-identical tokens.
test("jti differs between agents with identical vu/iter/second", () => {
  const a = buildClaims({ userIndex: 7, iteration: 42, issuer: "iss", mcpAud: "aud", runId: "run-1", agentIndex: 0 });
  const b = buildClaims({ userIndex: 7, iteration: 42, issuer: "iss", mcpAud: "aud", runId: "run-1", agentIndex: 3 });
  assert.notEqual(a.jti, b.jti);
});

test("jti differs between runs with identical agent/vu/iter/second", () => {
  const a = buildClaims({ userIndex: 7, iteration: 42, issuer: "iss", mcpAud: "aud", runId: "run-1", agentIndex: 2 });
  const b = buildClaims({ userIndex: 7, iteration: 42, issuer: "iss", mcpAud: "aud", runId: "run-2", agentIndex: 2 });
  assert.notEqual(a.jti, b.jti);
});

test("jti without runId/agentIndex still collides within one second — the k6 path must pass both", () => {
  // The defaults exist for the unit fixtures only. The load path always
  // passes runId + agentIndex (sideband-decision.js), and the two tests
  // above pin that uniqueness. This test documents WHY the defaults are
  // unsafe in production: two calls with the same (user, iteration) in the
  // same second produce identical jti — the exact cross-agent collision
  // finding 6 fixed. If this assertion ever fails, someone changed the jti
  // composition; re-verify agent uniqueness before shipping.
  const a = buildClaims({ userIndex: 1, iteration: 1, issuer: "iss", mcpAud: "aud" });
  const b = buildClaims({ userIndex: 1, iteration: 1, issuer: "iss", mcpAud: "aud" });
  assert.equal(a.jti, b.jti); // documents the collision the production args prevent
});
