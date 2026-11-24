# Design: policy-tree scaling campaign — 100 MCP branches at 1000 decisions/second

## Intent and why

The campaign of record characterizes the sideband decision path with one
policy branch (`AgentGatewayPortfolioMCP`, 6 policy sets, 16 rules). That is
a defensible constant for path characterization, but it leaves the obvious
representativeness question open: a real MCP-gateway deployment authorizes
dozens to hundreds of MCP servers, each carrying a similar policy set, so the
Global Decision Point under load has not 1 branch but ~100 siblings, and
every request must find its branch in that tree. This campaign answers
whether that tree size changes decision latency, throughput behavior, or
CPU cost at a fixed reference load, by comparing the existing 1-branch tree
against a 100-branch tree built from identical per-branch structure.

The work adds a second measured configuration; it does not alter the
campaign of record, its artifacts, or its reports. Nothing in
`reports/` or `results/` for the existing campaign is edited.

## Goals

- Produce a versioned 100-branch policy snapshot derived deterministically
  from the existing snapshot, committed in `server-profiles/`.
- Measure the 1-branch baseline and the 100-branch tree in the same session,
  interleaved, at 1000 decisions/second, with k6 selecting branches randomly
  per request (uniform over the 100).
- Report the comparison: latency percentiles, achieved rate, error rate, PAZ
  CPU, and a per-branch latency-bucket analysis that exposes any
  `FirstApplicable` sibling-scan gradient (early branches vs late branches).
- Keep every existing fixture, smoke test, and report byte-identical in
  behavior for the 1-branch configuration.

## Non-goals

- No policy-content diversity modeling: all 100 branches share the identical
  6-set/16-rule structure. Accepted simplification; real MCP policy sets are
  structurally similar, and identical clones keep per-branch evaluation cost
  constant so the size effect is isolated.
- No attribute-space growth (store-backed resolvers, extra external
  services): the resolver census stays as in the baseline snapshot.
- No new load levels: 1000/s only (the campaign of record's mid-ladder
  level), three runs per configuration.
- No external-PDP mode, no autoscaling, no saturation/burst testing: the
  scope exclusions of the campaign of record carry over unchanged.
- No edits to the campaign of record's snapshots, profiles, or reports.

## Approach (decided in conversation)

**One deployment, package swap — not a parallel second stack.** The 100-branch
snapshot imports into PAP as a second committed branch (the snapshot-import
contract always creates a branch named after the file, so the existing
`AgentGatewayPortfolioMCP` branch remains untouched). `build-package.sh` gains
a branch-name parameter and publishes a per-branch S3 package; PAZ swaps trees
by pointer publish + 30s poll, no restart, same two PAZ pods. The only
variable across the comparison is the policy tree. A post-publish tree-identity
probe (`/mcp/portfolio-2` permits on the 100-branch tree, NOT_APPLICABLE on
the 1-branch tree) verifies the right tree is loaded before any run.

Rejected: parallel second PAZ stack (doubles cluster footprint, new release
and JWKS wiring, buys no control the interleaving doesn't); PAP
version-control branching of the existing branch (dozens of stateful API
calls, unreproducible, working-copy/commit dance for what a committed file
gives for free).

## Design decisions

- **Snapshot generation.** An offline generator script reads the existing
  `AgentGatewayPortfolioMCP.SNAPSHOT` and emits
  `AgentGatewayPortfolioMCP-x100.SNAPSHOT`: the original `Portfolio MCP` set
  plus 99 clones (`portfolio-2`..`portfolio-100` as `ResourcePath.leaf`
  conditions, per-branch audience constants, fresh UUIDs for every cloned
  object). Deterministic: regenerating yields byte-identical output.
  Committed in `server-profiles/pingauthorizepap/policies/`.
- **Global Decision Point id reused.** The x100 snapshot keeps the same GDP
  id (`a7c4e2d0-8f11-4b5a-9c3d-6e8f2a1b4c7d`) — the two snapshots never
  coexist in a loaded package, and the packaging root needs no
  parameterization. Only the PAP branch name is parameterized.
- **Traffic.** k6 selects the branch uniformly at random per request
  (not round-robin). The four existing scenarios (read / session / HITL /
  deny) keep their shares; each request's URL is `/mcp/<branch>` and the
  token's `aud` matches that branch's audience constant (R1 checks `aud`
  against the constant, so token and URL must agree per request). The
  deny fixture's wrong-audience token strips the current branch's suffix.
- **Run plan.** One session, 6 runs interleaved B,S,B,S,B,S (B = 1-branch
  baseline re-measured adjacent in time; S = 100-branch) at 1000/s,
  three runs each. The old campaign's numbers are not trusted across time;
  the baseline is re-measured in the same session.
- **Analysis.** Bucket per-request latency by branch index; compare
  first-decile vs last-decile branch buckets to expose sibling-scan
  position effects. If early and late branches differ materially, that is
  reported as a finding, not treated as a bug.
- **Report.** A comparison section in the report generator
  (new run_id, `profiles/scale-100.env`); the campaign of record's reports
  are untouched. Named limitations in the report: identical-branch structure
  (possible PAZ dedup/compilation sharing that a diverse tree couldn't);
  attribute-space growth not modeled; 1000/s only; no verdicts — the
  benchmark applies no performance acceptance criteria, per project rules.

## Constraints

- Kubernetes access: Federico runs all cluster logins manually; PAP import,
  package build, and runs happen in sessions where his auth is fresh. The
  generator, k6 changes, tests, and doc updates need no cluster.
- bash 3.2, scripts-not-Makefile, `results/`/`evidence/` untracked as before.
- The benchmark applies no performance acceptance criteria and computes no
  PASS/FAIL verdict.

## Left to the builder

Calls settled during the builder check (recorded so the plan starts from
them; all three change no design decision):

- Clone UUIDs are derived deterministically (UUIDv5 over a fixed namespace
  and the branch index), which is what makes regeneration byte-identical.
- Every collected decision record gains the selected branch index (and the
  token's audience) so per-branch bucketing is computable offline from the
  raw records.
- Each package swap is scripted as one command: publish → wait for the
  poll window → tree-identity probe → confirm, so the interleave B,S,B,S,B,S
  is operator-drivable without manual curl sequences.

Remaining builder discretion:

- Generator implementation details (ordering of cloned
  objects in the snapshot stream, unit tests for the generator).
- Exact k6 parameterization mechanics (how random branch selection threads
  through `buildClaims` and the envelope URL) — within the design
  constraint that token `aud` and envelope URL name the same branch on
  every request, including the wrong-audience deny fixture.
- Profile file contents for `scale-100.env` and the encoding of the
  interleaved B,S,B,S,B,S order in the run script.
- Wording and structure of the new comparison report section, within the
  project's no-verdict reporting rules.
