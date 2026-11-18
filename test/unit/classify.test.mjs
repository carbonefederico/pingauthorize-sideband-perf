import { test } from "node:test";
import assert from "node:assert/strict";
import { classify, isExpectedDenial, normalizeResponseCode, buildClaims } from "../../helm/loadtest/files/envelope.mjs";

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

// 2026-11-17 follow-up review, finding 3: embedded server errors and
// malformed embedded responses are errors, not denials.
test("200 with embedded response_code 500 is an error, not a denial", () => {
  assert.equal(classify(200, { response: { response_code: 500 } }), "error");
});

test("200 with a string response is an error", () => {
  assert.equal(classify(200, { response: "bad" }), "error");
});

test("200 with an empty-object response is an error (no response_code)", () => {
  assert.equal(classify(200, { response: {} }), "error");
});

test("200 with a null response is an error, not a permit", () => {
  assert.equal(classify(200, { response: null }), "error");
});

test("200 with an embedded error object is an error, not a permit", () => {
  assert.equal(classify(200, { error: "internal failure" }), "error");
});

test("200 with response_code 403 is a deny", () => {
  assert.equal(classify(200, { response: { response_code: 403 } }), "deny");
});

// 2026-11-17 fifth review, finding 1: PAZ serializes the embedded
// response_code as a string ("401" — proven live by the smoke test's
// garbage-token decision). Numeric and string forms must score identically,
// and malformed codes must stay errors.
test("string response_code \"403\" is a deny, identical to numeric 403", () => {
  assert.equal(classify(200, { response: { response_code: "403" } }), "deny");
  assert.equal(classify(200, { response: { response_code: "401" } }), "deny");
});

test("string response_code \"500\" remains an error", () => {
  assert.equal(classify(200, { response: { response_code: "500" } }), "error");
});

test("malformed response codes remain errors", () => {
  for (const bad of ["40x", "40.5", "0403", "", " 401", "401 ", "499.5", "40", null, undefined, [], {}]) {
    assert.equal(classify(200, { response: { response_code: bad } }), "error", `case ${JSON.stringify(bad)}`);
  }
});

test("out-of-range numeric codes remain errors", () => {
  for (const bad of [99, 100, 599, 600, -1, 0, 200, 302]) {
    assert.equal(classify(200, { response: { response_code: bad } }), "error", `case ${bad}`);
  }
});

test("normalizeResponseCode accepts integers and exact three-digit strings only", () => {
  assert.equal(normalizeResponseCode(401), 401);
  assert.equal(normalizeResponseCode("401"), 401);
  assert.equal(normalizeResponseCode("599"), 599);
  for (const bad of ["0403", "40", "40.5", "40x", "", null, undefined, 40.5, 99, 600]) {
    assert.equal(normalizeResponseCode(bad), null, `case ${JSON.stringify(bad)}`);
  }
});

// The expected-denial comparison must use the same normalization as
// classify(): a string-coded denial satisfies a numeric fixture.
test("isExpectedDenial matches string-coded denials against numeric fixtures", () => {
  assert.equal(isExpectedDenial({ response: { response_code: "403" } }, 403), true);
  assert.equal(isExpectedDenial({ response: { response_code: 403 } }, 403), true);
  assert.equal(isExpectedDenial({ response: { response_code: "401" } }, 403), false);
  assert.equal(isExpectedDenial({ response: { response_code: 401 } }, 403), false);
});

test("a valid but unexpected denial status fails the outcome check", () => {
  // Right shape (a deny by classify), wrong rule (401 where 403 belongs).
  assert.equal(classify(200, { response: { response_code: 401 } }), "deny");
  assert.equal(isExpectedDenial({ response: { response_code: 401 } }, 403), false);
});

test("isExpectedDenial rejects malformed bodies and malformed fixtures", () => {
  assert.equal(isExpectedDenial(null, 403), false);
  assert.equal(isExpectedDenial({}, 403), false);
  assert.equal(isExpectedDenial({ response: null }, 403), false);
  assert.equal(isExpectedDenial({ response: "bad" }, 403), false);
  assert.equal(isExpectedDenial({ response: { response_code: 403 } }, undefined), false);
  assert.equal(isExpectedDenial({ response: { response_code: 403 } }, "403"), false);
});

// 2026-11-17 follow-up review, finding 6: within one agent, VU 1 and VU 101
// share the wrapped userIndex — the jti must still differ (full vuId).
test("jti differs for VU 1 and VU 101 with identical iteration/second/agent", () => {
  const a = buildClaims({ userIndex: (1 + 42) % 100, iteration: 42, issuer: "iss", mcpAud: "aud", runId: "run-1", agentIndex: 0, vuId: 1, scenario: "mix_read" });
  const b = buildClaims({ userIndex: (101 + 42) % 100, iteration: 42, issuer: "iss", mcpAud: "aud", runId: "run-1", agentIndex: 0, vuId: 101, scenario: "mix_read" });
  assert.equal(a.sub, b.sub); // same VIP identity, by design of the wrap
  assert.notEqual(a.jti, b.jti); // but never the same token identity
});
