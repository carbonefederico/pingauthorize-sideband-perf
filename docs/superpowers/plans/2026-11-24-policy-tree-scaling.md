# Policy-Tree Scaling Campaign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether a 100-branch MCP policy tree changes sideband decision latency vs the 1-branch baseline, at 1000 decisions/second, with k6 selecting branches randomly per request.

**Architecture:** A deterministic Python generator clones the existing `Portfolio MCP` policy subtree 99× into a new committed snapshot file; PAP imports both snapshots as sibling branches; `build-package.sh` gains a branch parameter and a new `swap-policy.sh` publishes a tree and waits for PAZ to confirm it; the k6 script gains a `BRANCH_COUNT` env (default 1 = today's behavior) that picks a random branch per request and tags every metric with it; a decile analyzer and a run-comparison script turn the streams into the scaling answer.

**Tech Stack:** Python 3 (stdlib only), bash 3.2-compatible, node:test for JS units, existing helm/k6/JWKS stack unchanged.

**Spec:** `docs/superpowers/specs/2026-11-24-policy-tree-scaling-design.md`

## Global Constraints

- bash 3.2 only in `scripts/` — no `declare -A` (macOS system bash).
- No performance acceptance criteria, no PASS/FAIL verdict anywhere in analysis or reports.
- The campaign of record's artifacts stay untouched: `server-profiles/pingauthorizepap/policies/AgentGatewayPortfolioMCP.SNAPSHOT` byte-identical; `profiles/stage-*.env` unchanged; existing `reports/` files unmodified.
- GDP id `a7c4e2d0-8f11-4b5a-9c3d-6e8f2a1b4c7d` is reused in the x100 snapshot (never coexists with the baseline package; packaging root needs no parameterization).
- Snapshot import contract: importing always creates a PAP branch named after the file (minus `.SNAPSHOT`); a taken name 409s.
- Kubernetes auth: Federico runs logins; on any auth-flavored error STOP and hand back. Session confirmed fresh 2026-11-24.
- Fixed load level: 1000/s (RATE=1000, AGENTS=10, DURATION=5m, WARMUP=30s — the stage-1000 shape).
- Test commands: `npm test` (node --test `test/unit/**/*.test.mjs`), `python3 -m unittest test/unit.report_replacements_test` style module paths for Python units.

## Review Focus

- **Snapshot digest/footer:** the generated snapshot carries the original `DataStreamFooter` digest, which cannot be recomputed offline; the live PAP import (Task 2) is the only validator. If import rejects it, the contingency is stripping the footer element and re-importing — never editing the baseline snapshot.
- **Branch/audience agreement on every request:** token `aud` and envelope URL leaf must name the same branch, including the wrong-audience deny fixture (which strips `/mcp/<branch>` to the gateway root). Pinned by `branch.test.mjs` in Task 3.
- **BRANCH_COUNT unset or 1 → legacy behavior:** no random selection, no extra tags, identical envelope. Pinned by `branch.test.mjs`.
- **Swap leaves exactly one tree live:** `swap-policy.sh` must verify the *negative* case too (probe leaf absent on the baseline tree, present on x100) — a probe that only checks the positive case would pass after a failed swap-back. Pinned by the probe assertions in Task 4.
- **Random branch uniformity:** selection must be uniform over 1..BRANCH_COUNT (no off-by-one, no modulo bias concern at 100; a 10 000-sample chi-square-lite bounds check). Pinned by `branch.test.mjs`.

---

### Task 1: Snapshot generator (offline, deterministic)

**Files:**
- Create: `scripts/generate-x100-snapshot.py`
- Test: `test/unit/test_x100_snapshot.py`

**Interfaces:**
- Consumes: `server-profiles/pingauthorizepap/policies/AgentGatewayPortfolioMCP.SNAPSHOT` (single-line JSON array, 161 elements).
- Produces: `server-profiles/pingauthorizepap/policies/AgentGatewayPortfolioMCP-x100.SNAPSHOT`, byte-identical on regeneration. Later tasks import and package this file.

Snapshot facts this task relies on (verified against the file):
- `Portfolio MCP` PolicySet id `59473014-ee49-41c7-8f65-4bfb8388e8e6`, condition `HttpRequest.ResourcePath.leaf == "portfolio"` (attr id `354ae71a-f26d-4449-aa8b-f203c1e92d08`), child of `AgentGateway` set (id `35d4ec64-e0ce-498d-b4b8-155436bb19ba`, empty condition).
- Clone scope = the 38 elements reachable from `Portfolio MCP` (1 PolicySet + 6 Policies + 16 Rules + 15 Statements), plus 1 new per-clone audience constant attribute — the original (`183b9113-9fba-4127-8cb5-7c1312e47429`, constant `https://agentgateway-stack-ag.ping-devops.com/mcp/portfolio`) is top-level and referenced by id from the R1 rules, so each clone gets its own attribute and its R1 references are repointed.
- The audience URL string also appears in the `Allow Read Tools` description text — replace it in cloned description strings.
- Element order in the array is preserved; clones are appended before the `Portfolio MCP`-adjacent tail elements (final order: originals, then per-branch elements 2..100, then the two `PackageSeparator`/`EndOfPackage` elements and footer, which stay last and untouched).

- [ ] **Step 1: Write the failing test** — `test/unit/test_x100_snapshot.py` (unittest, stdlib only, runs offline against the real baseline snapshot):
  - `test_baseline_untouched`: the baseline snapshot file's bytes are not modified by generation (hash before/after).
  - `test_deterministic`: generating twice yields byte-identical output.
  - `test_structure`: x100 is a JSON list; element count = 161 + 99×39; GDP id and `PackageHeader` unchanged; exactly one `DataStreamFooter`, last.
  - `test_tree`: AgentGateway set has 100 children; child 1 resolves to the original `Portfolio MCP`; children 2..100 have condition `leaf == "portfolio-<i>"`; each branch i's R1 rules reference that branch's audience attribute, whose constant value is `https://agentgateway-stack-ag.ping-devops.com/mcp/portfolio` for i=1 and `.../mcp/portfolio-<i>` otherwise.
  - `test_no_id_collisions`: all `id` values of cloned elements are fresh (no id appears in both baseline and clone sets, and clone ids are unique); cloned elements' `parentId`/child/statement references resolve within the x100 element set.
- [ ] **Step 2: Run it — expected FAIL** (module/file missing): `python3 -m unittest test.unit.test_x100_snapshot -v` (from repo root, or `python3 -m unittest discover -s test/unit -p 'test_x100_snapshot.py' -t .`).
- [ ] **Step 3: Implement** `generate-x100-snapshot.py`:
  - CLI: `python3 scripts/generate-x100-snapshot.py` (no parameters — conventions over knobs; paths derived from the repo layout).
  - UUIDs: `uuid.uuid5(NAMESPACE, f"{branch-index}:{role}:{original-id}")` with a fixed module-level namespace UUID — deterministic across runs and machines.
  - Load baseline, compute the 38-element reachable set (children/statements walk), emit originals + per-branch clones (branch 1 = original subtree, no clone), rewrite: set condition leaf constant, per-branch audience attribute (new id, constant value per branch, `parentId` = the existing `Constants` folder id `f22540f5-62fa-4acc-b511-783d797cc7b8`), R1 reference repointing, description text replacement, and the AgentGateway set's `children` array (100 id refs in order).
  - Write with `json.dump(..., separators=(",", ":"), ensure_ascii=False)` — matching the baseline's compact serialization — as one line.
- [ ] **Step 4: Run tests — expected PASS.**
- [ ] **Step 5: Generate the artifact** (no commit step — Federico commits when he asks).

### Task 2: Live PAP import validation (cluster — first kubectl of session)

**Files:** none created; validation only.

**Interfaces:**
- Consumes: Task 1's snapshot; running PAP at `${PAP_PUBLIC_HOST}` (`.env`), admin credentials in `.env`.
- Produces: confirmation that PAP accepts the generated snapshot format; on failure, the footer-stripping contingency.

- [ ] **Step 1: Import the x100 snapshot into PAP** via the same API the bootstrap hook uses (`POST /api/snapshot/AgentGatewayPortfolioMCP-x100-import-check/import` with the file as body, scratch name so a failure can't collide with the real branch name).
- [ ] **Step 2: If PAP rejects the digest/footer** — strip the `DataStreamFooter` element (and only it) in a scratch variant, re-import; if that succeeds, make footer-stripping a generator option (`--strip-footer`) and note the finding in the plan-adjacent docs. Never touch the baseline snapshot.
- [ ] **Step 3: Delete the scratch branch** (`DELETE /api/version-control/branches/<id>` — the real branch is created by the hook at next PAP boot, Task 6).
- [ ] **Step 4: Record the outcome** (one line each: HTTP code, branch id, deletion ok) — this is the format gate for everything downstream.

### Task 3: k6 branch support

**Files:**
- Modify: `helm/loadtest/files/envelope.mjs` (add two pure helpers)
- Modify: `helm/loadtest/files/sideband-decision.js` (branch selection + tags + aud/URL threading)
- Modify: `scripts/create-secrets.sh` (one `--from-literal` line)
- Test: `test/unit/branch.test.mjs`

**Interfaces:**
- Consumes: `buildClaims({..., mcpAud})` (existing, unchanged — aud override arrives via `extraClaims.aud` in `buildSubjectToken`).
- Produces (in `envelope.mjs`, exported, pure):
  - `selectBranch(branchCount, rng = Math.random)` → integer 1..branchCount (floor of `rng()*branchCount + 1`).
  - `branchAudience(baseAud, branchIndex)` → string: `baseAud` when branchIndex ≤ 1, else `baseAud` with a `-<i>` suffix on the last path segment (`https://host/mcp/portfolio` → `https://host/mcp/portfolio-37`).
- Produces (behavioral): k6 script honors `__ENV.BRANCH_COUNT` (default `1`); when > 1, every request (all four scenarios) uses `branchAudience(mcpAud, selectBranch(BRANCH_COUNT))` for BOTH the token `aud` (via `extraClaims`) and the envelope `url`, and tags every metric (`latency`, `elapsed`, `successRate`, `denials`, `errors`, the check, and `params.tags`) with `branch: String(branchIndex)`. When 1: the global `envelopeUrl`/`mcpAud` path is used untouched and the `branch` tag is still emitted with value `"1"` (uniform tagging, zero behavior change).
- The wrong-audience deny fixture generalizes from `.replace(/\/mcp\/portfolio$/, '')` to `.replace(/\/mcp\/.*$/, '')`.

- [ ] **Step 1: Write the failing tests** — `test/unit/branch.test.mjs`:
  - `selectBranch(1)` always 1; `selectBranch(100)` ∈ [1,100] over 10 000 draws with all values hit and min=1, max=100; deterministic with an injected `rng`.
  - `branchAudience(base, 1) === base`; `branchAudience('https://h/mcp/portfolio', 37) === 'https://h/mcp/portfolio-37'`.
- [ ] **Step 2: Run — expected FAIL** (`npm test`).
- [ ] **Step 3: Implement** the two helpers in `envelope.mjs` and the k6 script changes above. Branch selection happens per iteration call (inside the four iteration functions, before `buildSubjectToken`), never in init context.
- [ ] **Step 4: Run all units — expected PASS** (`npm test`; existing tests must still pass unchanged).
- [ ] **Step 5: create-secrets.sh** gains `--from-literal=BRANCH_COUNT="${BRANCH_COUNT:-1}" \` in the `paz-loadtest` secret block (after `INSECURE_SKIP_TLS_VERIFY`).

### Task 4: Package publishing per branch + swap + probe

**Files:**
- Modify: `scripts/build-package.sh` (branch-name parameter)
- Create: `scripts/probe-branch.mjs`
- Create: `scripts/swap-policy.sh`
- Test: manual verification steps below (cluster); probe output shapes are asserted by swap-policy.sh at run time.

**Interfaces:**
- Consumes: `buildClaims`, `signToken` (smoke-helpers) for the probe; `build-package.sh`'s existing flow.
- Produces:
  - `build-package.sh`: `POLICY_BRANCH="${POLICY_BRANCH:-AgentGatewayPortfolioMCP}"`; branch lookup, package name (`${branch}-<timestamp>`), and `TARGET` (`results/${branch}.deploymentpackage`) all derive from it. `DECISION_NODE_ID` stays fixed (same GDP).
  - `probe-branch.mjs`: env-driven single decision (`MCP_AUD`, `PAZ_SIDEBAND_URL`, `SIDEBAND_SHARED_SECRET`, `SUBJECT_ISSUER`, `SUBJECT_SIGNING_KEY_PEM`); prints one JSON line `{"verdict":"permit"|"deny","code":<int|null>}`; exit 0 always (report-only; assertions live in the caller).
  - `swap-policy.sh <baseline|x100>`: publish (`POLICY_BRANCH=... build-package.sh`) → wait for both PAZ replicas' logs to confirm the package id (same grep pattern as `deploy.sh`) → run the tree-identity probe twice:
    - x100: `MCP_AUD=<base>/mcp/portfolio-2` must be **permit**;
    - baseline: `MCP_AUD=<base>/mcp/portfolio-2` must **not** be permit (deny or NOT_APPLICABLE-shape error both count — the assertion is "no permit").
    - Exits non-zero with the probe JSON printed on any mismatch.

- [ ] **Step 1: Parameterize build-package.sh** (three lines: branch var, name, TARGET; plus the branch-lookup uses the var it already builds).
- [ ] **Step 2: Write probe-branch.mjs** — mirror of `smoke.mjs`'s `decide()` (import the same `envelope.mjs` builders; `buildClaims` + `signToken` for a valid vip-user-001 token), one call, one JSON line.
- [ ] **Step 3: Write swap-policy.sh** — bash 3.2, reuses `common.sh`, `PAZ_RELEASE`/`NAMESPACE` from `.env`; port-forward to pod/`${PAZ_RELEASE}-pingauthorize-0` 18443:1443 for the probes (same pattern as `deploy.sh`), trap-cleanup.
- [ ] **Step 4: Offline checks** — `bash -n scripts/swap-policy.sh`; `node scripts/probe-branch.mjs` without env exits 0 printing an error-shaped JSON (missing-env guard). Cluster validation happens in Task 6's first swap.

### Task 5: Analysis — per-branch deciles + run comparison

**Files:**
- Modify: `scripts/generate-report.py` (conditional branch-decile section)
- Create: `scripts/compare-scaling-runs.py`
- Test: `test/unit/report_replacements_test.py` (extend)

**Interfaces:**
- Consumes: `results/<run_id>/agent-*-metrics.json` NDJSON streams; tags `phase`, `scenario`, `branch` on `http_req_duration` datapoints.
- Produces:
  - In the per-run HTML report: when any measured-phase duration datapoint carries a `branch` tag with value ≠ "1", a section "Per-branch latency spread": decile table (decile 1..10 over branch index, n / p50 / p95 / max) plus the branch-1 vs branch-100 rows called out. Absent the tag: no section, byte-shaped output unchanged for legacy runs.
  - `compare-scaling-runs.py runA runB ...`: reads each run dir's k6 summary (measured-phase via stream scan, reusing the scanner pattern from `generate-report.py`), emits one markdown table (per run: median-of-agents p50/p95, achieved rate, error count, dropped iterations, PAZ CPU mean from `usage-during-run.txt`) to stdout for the reports/ layer.
- [ ] **Step 1: Extend the Python unit test** with: a synthetic stream fixture carrying `branch` tags renders the section; a legacy stream (no branch tags) renders no section and leaves existing tables byte-identical (assert on the two existing fixture outputs already in the test).
- [ ] **Step 2: Run — expected FAIL.**
- [ ] **Step 3: Implement** the section in `generate-report.py` (scan already parses tags; thread `branch` through `read_stream`) and `compare-scaling-runs.py` (stdlib argparse, markdown print).
- [ ] **Step 4: Run — expected PASS** (`python3 -m unittest ...`, `npm test` untouched-green).

### Task 6: Profile + docs + branch import + campaign run (cluster)

**Files:**
- Create: `profiles/scale-100.env`
- Modify: `server-profiles/README.md` (two-snapshot layout + constants note), `README.md` (scope section gains the scaling campaign and its limitations)
- Cluster: PAP redeploy/import, 6 runs

**Interfaces:**
- Consumes: everything above.
- Produces: `results/<6 run_ids>/`, HTML reports per run, a comparison table for `reports/`.

- [ ] **Step 1: `profiles/scale-100.env`** — copy of `stage-1000.env` header style: same AGENTS=10, RATE=1000, DURATION=5m, WARMUP=30s, PRE_ALLOCATED_VUS=50, MAX_VUS=120, INSECURE_SKIP_TLS_VERIFY=true, plus `BRANCH_COUNT=100`, with a header comment stating its role (100-branch tree at the stage-1000 reference level; MCP_AUD/PAZ_SIDEBAND_URL still from .env — branch suffixes are derived in the generator, not the profile).
- [ ] **Step 2: Docs** — `server-profiles/README.md`: add the x100 snapshot line (generated, deterministic, regeneration command), note both branches coexist committed in PAP, note the shared GDP id. `README.md`: add a "Policy-tree scaling campaign" subsection under Scope: what it adds, the identical-structure simplification, the dedup caveat, the attribute-space limitation, no verdicts. Full declarative sentences per the documentation style rules.
- [ ] **Step 3: Commit check with Federico** — offer the commit(s) before touching the cluster (project rule: commit only when asked).
- [ ] **Step 4: Redeploy PAP so hook 81 imports both snapshots** — confirm the session is fresh with Federico, then `./scripts/deploy.sh` (full path: it re-verifies, republishes the baseline package, confirms both replicas load it, smoke 3/3). Auth-flavored error → STOP, hand back.
- [ ] **Step 5: Swap to x100 and run the interleaved campaign** — for each of B,S,B,S,B,S: `./scripts/swap-policy.sh <tree>` → `PROFILE=<stage-1000|scale-100> ./scripts/run-in-cluster.sh` → note the run_id. B = baseline (BRANCH_COUNT unset → 1), S = scale-100. Wall clock ≈ 50 min.
- [ ] **Step 6: Generate reports** — `generate-report.py` per run (the six S runs must show the branch-decile section; the three B runs must not), then `compare-scaling-runs.py <B1> <S1> <B2> <S2> <B3> <S3>` into `reports/`. Report the comparison table and the per-branch spread finding (or its absence) to Federico.
