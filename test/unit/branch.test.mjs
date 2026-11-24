import { test } from "node:test";
import assert from "node:assert/strict";
import { selectBranch, branchAudience } from "../../helm/loadtest/files/envelope.mjs";

test("selectBranch(1) always returns 1", () => {
  for (let i = 0; i < 100; i++) {
    assert.equal(selectBranch(1), 1);
  }
});

test("selectBranch is deterministic under an injected rng", () => {
  let calls = 0;
  const rng = () => {
    calls += 1;
    return 0.42;
  };
  assert.equal(selectBranch(100, rng), 43); // floor(0.42*100)+1
  assert.equal(calls, 1);
  assert.equal(selectBranch(100, () => 0.999), 100);
  assert.equal(selectBranch(100, () => 0), 1);
});

test("selectBranch(100) over 10k draws hits every branch, min 1, max 100", () => {
  const seen = new Set();
  for (let i = 0; i < 10_000; i++) {
    const b = selectBranch(100);
    assert.ok(Number.isInteger(b) && b >= 1 && b <= 100, `out of range: ${b}`);
    seen.add(b);
  }
  assert.equal(seen.size, 100);
});

test("branchAudience(1) is the base audience untouched", () => {
  const base = "https://host/mcp/portfolio";
  assert.equal(branchAudience(base, 1), base);
});

test("branchAudience suffixes the last path segment", () => {
  assert.equal(
    branchAudience("https://host/mcp/portfolio", 37),
    "https://host/mcp/portfolio-37",
  );
});
