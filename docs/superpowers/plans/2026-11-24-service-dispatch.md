# Service-Dispatch Variant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the Ping-recommended dispatch shape (parameterized endpoint, service-targeted policy sets) as a third measured configuration of the scaling campaign, run it once at 1000/s, and leave it as the deployment's resting state.

**Architecture:** `generate-x100-snapshot.py` gains a `--services` mode emitting per-branch `ServiceDefinition`s named `AgentGateway.portfolio-<i>` with the branch's policy set targeted at its own service (no leaf conditions on clones 2..100); the dsconfig layer gains a parameterized endpoint (`base-path:/mcp/{mcp}`, `service:AgentGateway.{mcp}`) applied by a small `set-sideband-endpoint.sh`; `swap-policy.sh` gains an `x100-services` target that publishes the tree, swaps the endpoint, confirms both replicas, and probes; k6 is untouched.

**Tech Stack:** Python 3 stdlib, bash 3.2, existing helm/k6/dsconfig stack.

**Spec:** `docs/superpowers/specs/2026-11-24-service-dispatch-design.md`

## Global Constraints

- bash 3.2 only in `scripts/`; no `declare -A`.
- No performance acceptance criteria, no PASS/FAIL verdicts.
- Campaign of record untouched: `AgentGatewayPortfolioMCP.SNAPSHOT` byte-identical; existing runs/reports unmodified.
- The condition-gated x100 snapshot stays committed unchanged (`AgentGatewayPortfolioMCP-x100.SNAPSHOT`).
- GDP id `a7c4e2d0-8f11-4b5a-9c3d-6e8f2a1b4c7d` reused in the services snapshot.
- ServiceDefinition names MUST be exactly `AgentGateway.portfolio-<i>` for i in 2..100 — they must match what the endpoint's `service:AgentGateway.{mcp}` derives per request (settlement from plan recon, 2026-11-24).
- Kubernetes auth: Federico runs logins; on auth-flavored errors STOP and hand back. Do not route around permission prompts.
- Resting state after the campaign: x100-services tree + parameterized endpoint live, probe green.
- Test commands: `npm test`, `python3 -m unittest test.unit.test_x100_services`.

## Review Focus

- **Service-name mismatch** (`AgentGateway portfolio-2` vs `AgentGateway.portfolio-2`): a typo'd name silently sends every request N/A — the probe's permit assertion in Task 4 catches it live, and `test_tree` in Task 1 pins the exact names.
- **Endpoint/tree coherence:** every swap must leave (endpoint, tree) pairs that match — parameterized endpoint only with a services tree, plain endpoint only with the others. A mixed pair yields N/A decisions that still 200. Pinned by probe assertions in Task 4.
- **Clones must NOT carry leaf conditions** in services mode — a leftover condition is dead weight that biases nothing but proves the generator was copied, not transformed. Pinned by `test_tree`.
- **Digest recomputation** after any generator change — the stream digest covers every element; a stale footer fails PAP import (Task 2 finding from the first campaign). Pinned by generator tests asserting the recomputed footer.
- **Baseline snapshot purity:** services mode must not touch the baseline or the condition-gated x100 outputs. Pinned by byte-hash tests.

---

### Task 1: Services-mode generator

**Files:**
- Modify: `scripts/generate-x100-snapshot.py`
- Test: `test/unit/test_x100_services.py`

**Interfaces:**
- Consumes: the existing generator's structure (reachable_subtree, clone_id, stream_digest, audience_value).
- Produces: `server-profiles/pingauthorizepap/policies/AgentGatewayPortfolioMCP-x100-services.SNAPSHOT`, byte-identical on regeneration; CLI `python3 scripts/generate-x100-snapshot.py --services` (flag-less invocation keeps producing the condition-gated x100 file exactly as today).

Clone contract for services mode (per branch i in 2..100):
- One `ServiceDefinition` element: `objectType: ServiceDefinition`, `name: AgentGateway.portfolio-<i>`, `type: SERVICE`, `serviceType: NONE`, otherwise a copy of the existing `AgentGateway` ServiceDefinition's shape with fresh uuid5 ids (namespace `uuid5(UUID_NAMESPACE, "services-mode")` for the whole mode's uuid5 namespace).
- One per-branch `Target` element (type `Target`, `targeted: {services: [<branch service id>]}`) with a fresh uuid5 id.
- The `Portfolio MCP` set clone: `targets: ["<branch target id>"]`, condition replaced with `{"empty": {}}` (no leaf condition), name `portfolio-<i> MCP`, description audience text replaced as in condition mode, audience attribute clone identical to condition mode.
- Element order: originals, then per-branch (audience attr, subtree clones, ServiceDefinition, Target) in branch order, then the wrapper tail; footer digest recomputed.

- [ ] **Step 1: Write the failing test** — `test/unit/test_x100_services.py`, mirroring `test_x100_snapshot.py`'s class:
  - `test_baseline_untouched`: hash of baseline and of `AgentGatewayPortfolioMCP-x100.SNAPSHOT` unchanged by generation.
  - `test_deterministic`: two runs byte-identical.
  - `test_structure`: JSON list; element count = 161 + 99×40 (38 subtree + 1 audience attr + 1 ServiceDefinition + 1 Target per branch); one footer, last.
  - `test_tree`: for i in 2..100: the ServiceDefinition `AgentGateway.portfolio-<i>` exists with a fresh id; the branch's Portfolio set has `targets` = [target-id] whose `targeted.services` = [service-id]; the set's condition is `{"empty": {}}`; its R1 rules reference the branch's audience attribute (`.../mcp/portfolio-<i>`); the set has NO comparison on attr `354ae71a-...` anywhere.
  - `test_wrapper_unchanged`: the `AgentGateway` wrapper set keeps its single original child (the condition-gated `Portfolio MCP`) and its Target; the GDP id unchanged.
- [ ] **Step 2: Run — expected FAIL** (no --services flag): `python3 -m unittest test.unit.test_x100_services`.
- [ ] **Step 3: Implement** `--services` in the generator: parameterize `generate(output_path, services=False)`; `main()` parses `--services` (argparse; no other knobs). Factor the per-branch clone loop so both modes share subtree cloning and audience-attribute creation; services mode adds the ServiceDefinition + Target elements and swaps condition→target as above.
- [ ] **Step 4: Run both generator suites — expected PASS**: `python3 -m unittest test.unit.test_x100_services test.unit.test_x100_snapshot`.
- [ ] **Step 5: Generate the artifact.**

### Task 2: PAP import validation (cluster)

**Files:** none.

- [ ] **Step 1: Port-forward PAP** (svc 8443 → local 18444) and POST the services snapshot to `POST /api/snapshot/AgentGatewayPortfolioMCP-x100-services-import-check/import`. Expected: HTTP 200. On 400 digest error: the generator's digest logic regressed — fix Task 1, never strip the footer.
- [ ] **Step 2: Delete the scratch branch** (HTTP 204); re-list branches to confirm only the three known ones remain.

### Task 3: Endpoint swap script + dsconfig layer

**Files:**
- Create: `scripts/set-sideband-endpoint.sh`
- Modify: `server-profiles/pingauthorize/pd.profile/dsconfig/40-sideband.dsconfig.subst` (comment only — documenting that the resting state is the parameterized endpoint, applied by set-sideband-endpoint.sh, since the .subst layer only applies at deploy)
- Test: offline `bash -n` + live probe steps in Task 4.

**Interfaces:**
- Consumes: PAZ admin port 1443 (dsconfig against the running PAZ), `SIDEBAND_SHARED_SECRET` from `.env`.
- Produces: `set-sideband-endpoint.sh parameterized|plain` — idempotent dsconfig against pod `${PAZ_RELEASE}-pingauthorize-0` via kubectl exec:
  - `parameterized`: delete endpoint `AgentGateway Portfolio MCP`, re-create with `--endpoint-name "AgentGateway Portfolio MCP" --set base-path:/mcp/{mcp} --set service:AgentGateway.{mcp} --set access-token-validator:PerfSubjectJWT`; shared secret/header lines skipped when already present (idempotence via `dsconfig list-sideband-api-endpoints` grep).
  - `plain`: the inverse (the existing block's shape: `base-path:/mcp`, `service:AgentGateway`).
- [ ] **Step 1: Write `set-sideband-endpoint.sh`** following `swap-policy.sh`'s conventions (common.sh source, require_value, usage message, set -euo pipefail).
- [ ] **Step 2: `bash -n` the script.**

### Task 4: Swap extension

**Files:**
- Modify: `scripts/swap-policy.sh`

**Interfaces:**
- Produces: `swap-policy.sh x100-services` — publishes the services tree (`POLICY_BRANCH=AgentGatewayPortfolioMCP-x100-services build-package.sh`), applies `set-sideband-endpoint.sh parameterized`, waits for both replicas' package confirmation, probes:
  - `/mcp/portfolio-2` with a branch-2 token must **permit**;
  - on `baseline` and `x100` targets, first apply `set-sideband-endpoint.sh plain`, then the existing probe assertions unchanged.
- [ ] **Step 1: Extend the case statement and the probe section** — each target names its endpoint mode (`baseline`→plain, `x100`→plain, `x100-services`→parameterized) and applies it after publish, before the wait.
- [ ] **Step 2: `bash -n` + offline case-coverage check** (`swap-policy.sh nonsense` still errors with usage).

### Task 5: Docs + profile + campaign run

**Files:**
- Modify: `profiles/scale-100.env` (header note: same profile drives both x100 modes — the tree determines dispatch), `README.md` (scaling-campaign section gains the third configuration + docs citations), `server-profiles/README.md` (third snapshot line, parameterized endpoint resting state)
- Cluster: import branch already done in Task 2, run 1× `PROFILE=scale-100`

- [ ] **Step 1: Docs** — full declarative sentences; cite the sideband endpoint config, path-parameters, and combining-algorithms pages.
- [ ] **Step 2: Commit + push with Federico's go** (profile delivery is via git; ask before pushing — established pattern).
- [ ] **Step 3: Import the services branch into PAP for real** — the bootstrap hook only runs at PAP boot; instead import via the same port-forward API call as Task 2 but with the real branch name `AgentGatewayPortfolioMCP-x100-services` and commit it (the hook's commit pattern: POST /commit, verify head ≠ SYSTEM head).
- [ ] **Step 4: `./scripts/swap-policy.sh x100-services`** — publish + endpoint swap + confirm + probe.
- [ ] **Step 5: `PROFILE=scale-100 ./scripts/run-in-cluster.sh`** — the run.
- [ ] **Step 6: Reports** — `generate-report.py` on the new run; `compare-scaling-runs.py 20261124125701 20261124132419 <new-run>`; report the three-way table and the per-branch spread to Federico. Deployment rests on x100-services + parameterized endpoint.
