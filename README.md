# PAZ sideband decision-path performance test

This project measures the performance of PingAuthorize Server (PAZ) acting as
the Policy Decision Point behind an API gateway, in Kubernetes. The load
generator plays the gateway's role (the Policy Enforcement Point): k6 agent
pods build the Sideband API request envelope exactly as a gateway integration
does and POST it to PAZ's `/sideband/request`, one authorization decision per
call. The campaign reports decision latency (avg/median/p90/p95/p99), achieved
throughput, error rate, and PAZ pod CPU/memory at fixed reference load levels
of 500, 1000, 1500, and 2000 decisions per second.

The deployment under test runs PAZ in **deployment-package (embedded PDP)
mode**: the policy state from a specific, versioned commit is exported as an
immutable bundle and loaded into the PAZ process itself. This is the
production integration model, and it differs from the sibling project
`ai-mcp-gateways-paz-integrations`, whose PAZ resolved policies through PAP
on every decision (external PDP mode). In this stack PAP exists only to build
the package once and sits off the decision path entirely. The full design —
including goals, non-goals, and the campaign protocol — is
[the spec](docs/superpowers/specs/2026-11-10-paz-sideband-perf-design.md).

## Architecture

```mermaid
flowchart LR
    subgraph k8s["Kubernetes cluster (namespace paz-perf)"]
        subgraph "measured path"
        k6["k6 agents ×10<br/>(plays the gateway / PEP)"]
        svc["PAZ Service<br/>cluster-internal HTTPS"]
        paz["PAZ ×2 StatefulSet<br/>embedded PDP"]
        end
        jwks["JWKS server ×1<br/>(publishes the subject key's JWKs<br/>for kid perf-subject-key)"]
        pap["PAP ×1<br/>authoring + inspection (public via ingress)"]
    end
    s3["S3 bucket<br/>(versioned package store)"]

    k6 -- "POST /sideband/request<br/>PDG-TOKEN + Bearer<br/>(fresh self-signed RS256 per iteration)" --> svc
    svc --> paz
    paz -. "JWKS fetch on validation<br/>(cached by PAZ)" .-> jwks
    pap -. "build-package.sh via PAP API<br/>→ PAP publishes package + pointer to s3://" .-> s3
    s3 -. "PAZ polls (30s)<br/>no restart on policy change" .-> paz
```

What is deliberately absent: no API gateway, no adapter, and
no ingress in front of the PDP (the measured path keeps its cluster-internal
Service). PAP is the one exposed workload — the policy administrator's
inspection surface, via the cluster's nginx-public ingress with a
cert-issued TLS host. Token validation is JWKS-based: the loadtest chart
publishes the subject key's JWKS from an in-cluster endpoint
(`<release>-jwks`), PAZ fetches and caches it (issuer-publishes/
verifier-fetches, as in the PF campaign), and that fetch-plus-cache sits on
the measured path. Policy delivery is production-shaped:
PAP authors the policy, `build-package.sh` publishes the immutable package
through PAP's package-store API to a private, versioned S3 bucket (PAP
writes both the package object and the pointer file, exactly as the
Deployment Manager's Publish action does), and the PAZ nodes poll and load
it without a restart — the same machinery an S3-backed production
deployment runs. The
policies are the sibling project's `AgentGatewayPortfolioMCP` branch,
packaged as-is, so the measured decisions exercise the real policy ladder
(audience gate, agent-identity delegation, VIP check, scope check) rather
than a permit-all stub.

## The policy under test and the traffic mix

The measured decisions come from the sibling project's
`AgentGatewayPortfolioMCP` policy branch, imported into PAP verbatim
(`server-profiles/pingauthorizepap/policies/AgentGatewayPortfolioMCP.SNAPSHOT`)
and published to PAZ as an immutable package rooted at the branch's Global
Decision Point. Under the `AgentGateway` service, the `Portfolio MCP` policy
set holds six policies, each a rule ladder over the JWT's claims (audience,
acting-agent identity, VIP-user claim, scope) and the MCP request body:

1. **Allow Session Methods** — MCP session methods (initialize / tools/list
   / ping): R1 audience gate, R2 delegated-agent identity, R3 VIP-user
   claim, R4 token-active.
2. **Allow Read Tools** — the read-permit path: the same R1–R3 ladder plus
   R4 `portfolio:read` scope; the cheapest full ladder on the tree.
3. **Deny Unpriceable Orders** — a single guard rule.
4. **Allow Orders with HITL Approval** — R1–R3 plus R4 `portfolio:write`
   scope and the approval-binding rule, which mirrors the order body
   (customerId / symbol / side / quantity / amount / txn) one-to-one against
   the token's `tctx` block; the most expensive ladder on the tree.
5. **Deny Oversized Orders** — a single guard rule (amount above the
   `Order Approval Threshold` constant).
6. **Default Deny** — the fallback verdict.

Load is distributed at three layers. Across the generator: the target rate
splits evenly over the ten k6 agent pods (RATE/10 per agent, enforced by
`distributeRate`, which fails the run loudly when the split drifts more
than 5%). Within each agent: the per-agent rate splits into four
ramped-arrival scenarios that map onto the policy paths above — 60% reads
(policy 2), 15% session methods (policy 1), 15% HITL orders (policy 4),
and 10% expected denials, which rotate through four negative fixtures
(undeclared acting agent and missing `act.sub` → R2, oversized order →
policy 5, wrong audience → R1) and so walk the ladder to a deny verdict.
Denials take the split remainder so rounding cannot break the sum, and a
per-scenario drift guard re-checks it. Across the server: the ten agents
POST to the cluster-internal PAZ Service, which load-balances across the
two StatefulSet pods.

## Scope of this campaign

The campaign is a **baseline characterization** of the sideband decision path
in deployment-package mode at fixed reference levels up to **2000
decisions/second**. It shares the PF campaign's scope discipline, and the
results support no conclusions about: the saturation point or maximum
sustainable throughput (no level is run to failure — the cluster is shared
with workloads this project does not own; 2000/s is the campaign bound,
where PAZ sat at 1,486 m of its 4,000 m per-pod CPU limit (37%) with a
still-smooth latency curve); behavior above 2000/s; overload, burst,
failure, or recovery
behavior; autoscaling (the PAZ topology is fixed at two pods); or a
comparison against external-PDP mode (this campaign measures
deployment-package mode only).

## Test environment and sizing

The campaign ran on a shared Kubernetes cluster of six `r5.xlarge` worker
nodes (4 vCPU / 32 GiB each, 3,400m allocatable CPU after system
reservations). The PAZ pods carried lab sizing of 2 CPU / 3 GiB requests
against 4 CPU / 6 GiB limits, one pod per node (the two PAZ nodes together
held 8 of the cluster's 20.4 allocatable vCPUs); PAP, the package-authoring
surface, carried 1 CPU / 2 GiB requests and 2 / 4 GiB limits and is idle
during runs. The ten k6 agent pods requested 500m CPU each with a 4 CPU
limit, and a required anti-affinity term kept every agent off the two PAZ
nodes. The reference results are figures for this environment: two PAZ pods
sized at half their CPU limit, a generator provisioned at ~25% of a node
per agent, and co-tenant workloads present but off the PAZ nodes. Node
inventory and pod-to-node placement for every run are recorded in
`results/<run_id>/env.json`.

## Measurement protocol

The protocol follows the PF campaign's (`pingfederate-token-exchange-perf`),
verbatim in its essentials:

- Each reference level is run **three times per campaign**, in ascending
  rounds (500 → 1000 → 1500 → 2000, repeated three times). The reference
  table reports the **median run** of the three, selected by measured p95,
  with the per-run p95s quoted alongside it.
- A run counts only if it passes the generator-health gates: zero dropped
  iterations, achieved rate ≥ 98% of target, success rate ≥ 99%. A run that
  fails a gate is rerun and both attempts are recorded, never swapped or
  averaged in.
- The first run after a fresh deployment is discarded: one throwaway sanity
  run warms the PAZ pods before the campaign's first counted round.
- Every latency column comes from the **same measured window** — all raw
  datapoints of the measured phase (the plateau of the ramped scenarios,
  tagged `phase=measured` per request), pooled across all ten agents,
  ramp excluded — recorded per run in `results/<run_id>/summary.json`
  (`pooled_latency`) and rendered in the per-run report.
- Each run: ten parallel k6 agent pods, each driving RATE/10 through four
  ramped-arrival scenarios (read/session/HITL/deny at the campaign's
  weighted mix) that ramp 0→full rate over 30 s (tagged `phase=warmup`,
  excluded from thresholds and from the measured window) and then hold for
  5 minutes. The ramp replaces the constant-arrival step start, which
  stalled PAZ at every 2026-11-16 stage-2000 attempt (see the VU-sizing
  note under Step 2).
- CPU columns come from the 15 s-cadence in-run sampler
  (`usage-during-run.txt`, written by `run-in-cluster.sh`): per-pod
  steady-state medians over the middle 60% of samples, for the PAZ pods
  (against the 4-CPU limit) and the loadtest agents. A `scripts/monitor.sh`
  pod-usage CSV, when one exists for the campaign, is preferred and used
  instead.
- Server-side ground truth: an end-of-run scrape of PAZ's `cn=Monitor` Gateway
  Timing providers (best-effort — a scrape failure never invalidates a run).
- **The k6 script and report tooling are frozen for the campaign.** Any
  change to the load shape, thresholds, or parsers between counted runs
  makes runs incomparable; tooling changes happen between campaigns, never
  mid-ladder (the 2026-11-16 phase-tag bug — a scenario-level tag that
  blinded the stream parser to every measured datapoint — is the cautionary
  example).

## Repository layout

```
helm/paz/            One configuration chart for the whole Ping stack: values for
                     the official pingidentity/ping-devops chart (0.15.0, tag 2607),
                     one release carrying PAP (public ingress, inspection surface)
                     and 2 PAZ replicas (cluster-internal, ephemeral /opt/out,
                     lab sizing requests 2 CPU/3Gi, limits 4/6Gi).
helm/loadtest/       The k6 agent chart: an indexed Job, N parallel agent pods,
                     each running files/sideband-decision.js with files/envelope.mjs.
server-profiles/     The PAZ dsconfig layer (JWKS token validator,
                     embedded-PDP Policy Decision Service, sideband endpoint)
                     and the PAP profile (snapshot import hook). See
                     server-profiles/README.md — including the version-specific
                     verifications to fill in on first deploy.
profiles/            Load profiles: stage-500/1000/1500/2000.env (the campaign
                     ladder) and agents.env. Each fully describes its run.
scripts/             One script per step: setup-aws (one-time S3 entities),
                     create-secrets, deploy (phases: headroom/prep/package/
                     runtime), verify, build-package, smoke, run-in-cluster,
                     monitor, scrape-monitor, generate-report.py,
                     analyze-run.py, analyze-timeline.py, validate,
                     uninstall.
keys/                Subject signing key (git-ignored). The matching public
                     JWK is published by the chart's JWKS server
                     (generated by scripts/generate-jwks.sh from the key).
results/             Per-run working artifacts, one directory per run (lab-
                     local, not in version control).
reports/             The published HTML reports — the reviewer-facing layer.
docs/                The spec and this plan.
.env                 Lab credentials and endpoints — git-ignored (see
                     .env.example for the documented shape).
```

## Quick start

Every command runs on your workstation, from the project root. Nothing is run
by hand inside the cluster. The namespace defaults to `paz-perf`.

### Step 0 — one-time setup

1. Provision the chart's licensing secret the way your environment
   standardly does (the chart secretRefs `devops-secret`; this project's
   scripts never touch credentials). Every script auto-creates `.env` from
   the committed defaults on first use; all its values ship with working
   lab defaults.
2. Push this repository to GitHub (the Ping products clone their profile
   from it at boot): `gh repo create carbonefederico/pingauthorize-sideband-perf
   --public --source . --push`.

The deploy needs nothing else: `create-secrets.sh` (inside `deploy.sh`)
builds the chart's TLS secrets and the `paz-secrets`/`paz-s3`/`paz-loadtest`
Secrets from `.env` (the chart's `devops-secret` licensing Secret is the
operator's to provision). PAP's admin password is the lab constant in
`.env` (`PAP_ADMIN_PASSWORD`, the DevOps default the images boot with);
scripts use it to authenticate against PAP client-side.

### Step 1 — deploy everything to test-ready (~15 minutes)

```bash
./scripts/deploy.sh headroom   # optional: per-node capacity picture first
./scripts/deploy.sh            # everything: secrets, the whole stack (one
                               # release: PAP + 2 PAZ replicas), publish the
                               # policy package through PAP, wait until both
                               # PAZ nodes load it from the store, smoke-test
                               # the three go/no-go decisions
```

The command ends when the stack is Ready, the package is loaded by both
replicas, and smoke passed 3/3 — at that point the only remaining step is
triggering the campaign itself (Step 2). Re-running `./scripts/deploy.sh`
at any time is safe: secrets and the helm release upgrade in place, the
package publish creates a fresh package, and the PAZ nodes pick it up
without a restart.

On first deploy, fill in the three version-specific verifications in
`server-profiles/README.md` (in-container paths, dsconfig property spelling,
monitor providers) — the deploy output prints what each needs.

The deploy's final smoke step runs the go/no-go itself (a valid token with
rotated `sub` → permit; a garbage token → a 200 carrying a 401 denial, so
the validator is proven live; a second rotated token → permit). To re-run
it alone later:

```bash
kubectl -n ${NAMESPACE} port-forward pod/${PAZ_RELEASE}-pingauthorize-0 18443:1443 &
./scripts/smoke.sh
```

### Step 2 — run the campaign (two terminals)

```bash
# Terminal A — resource monitor; leave running for the whole campaign:
./scripts/monitor.sh

# Terminal B — sanity run, then the ladder, three ascending rounds:
PROFILE=agents ./scripts/run-in-cluster.sh    # discarded warm-up run
PROFILE=stage-500 ./scripts/run-in-cluster.sh
PROFILE=stage-1000 ./scripts/run-in-cluster.sh
PROFILE=stage-1500 ./scripts/run-in-cluster.sh
PROFILE=stage-2000 ./scripts/run-in-cluster.sh
# ...repeat the ladder twice more (rounds 2 and 3)
```

Never run a level above the 2000/s bound. Each run saves per-agent k6 logs and
metric streams, `env.json`, the in-run CPU sampler output, and the
`cn=Monitor` scrape into
`results/<run_id>/` and exits non-zero if a k6 threshold failed.

#### VU sizing — why the extended levels needed tuning (2026-11-16)

Getting the 1500/s and 2000/s levels green required tuning the per-scenario
VU knobs (`PRE_ALLOCATED_VUS`, `MAX_VUS` in the stage profiles), and the
failure modes were all in the load generator, not PAZ. The deciding
evidence: at 2000/s the server delivered full rate with 100% checks and a
clean median in every attempt, while the dropped-iterations gate tripped on
artifacts that all traced to the warmup→measured transition. With
`constant-arrival-rate` scenarios, the measured window opens at full rate in
one step, and that burst of fresh TLS handshakes stalls PAZ at the boundary
(8s on a warmed JVM; 22s on one restarted minutes earlier), which cascades
into VU-pool exhaustion and shed iterations. Three concrete findings from
the streams: pre-allocating 300 VUs per scenario made the transition
stampede 15,000 VUs deep and collapsed PAZ outright (its liveness probes
killed it — do NOT raise `PRE_ALLOCATED_VUS` toward `MAX_VUS`); a burst of
5,000 stalled it 8s; and the same rate in steady state never stalled once
load was flowing.

The working configuration is now baked into the profiles and the k6 script:
`MAX_VUS=300` at 200 it/s per agent (arrival-rate × a ~1.4s worst-case
iteration needs that headroom; 240 shed ~200 iterations), small
`PRE_ALLOCATED_VUS` (the transition burst must stay under what PAZ absorbs
— 50 per scenario at 2000/s, below the 3,750 the 1500/s level absorbed
cleanly), and `ramping-arrival-rate` measured scenarios that ramp 0→full
over the warmup window so no step burst exists at all. With all three in
place the 2000/s level passes with zero dropped iterations and a 40ms p95.
A dropped-iteration count that grows with `MAX_VUS` rather than shrinking
is the signature of the transition burst, not the VU cap.

### Step 3 — report

```bash
python3 scripts/generate-report.py <run_id>   # newest run if omitted
```

Writes `results/<run_id>/report.html` (throughput, latency, latency
breakdown, PAZ pod and node CPU over time, per-agent detail) and
`summary.json` (pooled p95, success rate, achieved-vs-target, verdict,
monitor totals). Copy the report to `reports/` when it is a keeper.

## Benchmark assumptions

These assumptions define what the measured numbers mean. They are design
decisions, not accidents — changing any of them changes what the benchmark
measures.

1. **Token validation is JWKS-based, with one static signing key.** Subject
   tokens are self-signed RS256 by k6 (kid `perf-subject-key`) and validated
   by PAZ's JWKS validator, which fetches the public key from an in-cluster
   JWKS endpoint (`https://paz-perf-subject` — issuer-publishes/
   verifier-fetches, as in the PF campaign) and caches it. The fetch and
   cache are on the measured path; key *rotation* is not exercised (the
   campaign signs every token with the same static key).
2. **No persistent-store work on the decision path.** PAZ runs standalone
   with an embedded store and ephemeral data; the measured decisions evaluate
   the in-process policy package and touch no user store. The S3 package
   store's poller is active (30 s cadence) exactly as in a production store
   deployment — a background scan whose cost is part of the measured system.
   Subject identity rotates through 100 synthetic VIP users, and PAZ keeps no
   per-subject state on this path, so 100 hot identities exercise the same
   code paths as a cold population.
3. **The envelope is the production envelope.** The load generator builds the
   sideband request exactly as the sibling project's paz-extauthz-adapter
   does (schema pinned by unit tests). The measured traffic is a weighted
   policy mix — 60% reads, 15% MCP session methods (top-level jsonrpc),
   15% HITL order approvals, 10% expected denials, per agent rate — so
   every measured window exercises all four policy paths, not just the
   cheapest one. The measured hop is the hop a gateway integration adds:
   the sideband request plus PAZ's decision. No gateway, adapter, or MCP
   backend is in the measured path — those would measure additional
   components.
4. **Policy constants are the sibling's.** The sideband endpoint's service
   (`AgentGateway`), the MCP audience constant
   (`https://agentgateway-stack-ag.ping-devops.com/mcp/portfolio`), and the
   whole policy ladder come from the sibling's snapshot unchanged. The
   measured permits exercise the same conditions the MCP gateway's permits
   do.
5. **Monitor providers stay as deployed, for the whole campaign.** If the
   Gateway Timing providers need enabling, they are enabled once before the
   first counted run and never toggled — every number carries the same
   overhead. No paired on/off runs.
6. **The nodes are shared.** Co-tenant load during a run is visible in the
   report's node-CPU panel and is an uncontrolled environmental factor. The
   campaign bound (2000/s) exists because of this; reference levels are
   measured as-is, never driven to failure. The median-of-three protocol
   exists to make residual co-tenant noise visible as run-to-run spread
   rather than hiding it inside a single number.
7. **Warm state.** Ladder stages run sequentially against the same
   deployment; all counted runs are continuously warmed (the pre-campaign
   sanity run plus each stage's 30 s warmup). Cold-start behavior is not
   characterized.

## Reference results — campaign of 2026-11-17

Twelve counted runs, three ascending rounds of the 500 → 1000 → 1500 → 2000
ladder, all twelve passing the generator-health gates (zero dropped
iterations, 100% achieved rate, 100% decision success). Each row is the
median run of its level's three, selected by measured p95; the per-run p95s
in parentheses show run-to-run spread. The first counted run of the campaign
was run id `20261117102611`; a pre-campaign sanity run was not needed — the
PAZ pods had been up and serving full-rate traffic since November 13.

| Stage | Target rate | Requests | avg | median | p90 | p95 | p99 | PAZ CPU/pod | Agent CPU |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 500/s | 500/s | 150,001 | 1.85 ms | 1.66 ms | 2.29 ms | 2.87 ms | 6.22 ms | 318 m | 141 m |
| 1000/s | 1000/s | 300,022 | 2.12 ms | 1.72 ms | 2.92 ms | 4.24 ms | 8.93 ms | 633 m | 270 m |
| 1500/s | 1500/s | 450,074 | 3.04 ms | 1.88 ms | 5.41 ms | 8.51 ms | 20.73 ms | 991 m | 435 m |
| 2000/s | 2000/s | 600,063 | 6.64 ms | 2.75 ms | 15.9 ms | 26.25 ms | 51.84 ms | 1,486 m | 620 m |

Per-run p95s by level: 500/s — 2.78 / 2.87 / 3.26 ms (median run
`20261117110514`); 1000/s — 4.24 / 4.18 / 5.26 ms (median run
`20261117103702`); 1500/s — 8.25 / 8.51 / 13.47 ms (median run
`20261117112114`); 2000/s — 26.25 / 19.49 / 27.83 ms (median run
`20261117105622`). CPU medians are per PAZ pod (4-CPU limit) and per
loadtest agent, steady-state middle-60% of the 15 s in-run sampler. The
median run of the table row and the CPU medians quoted alongside it come
from the same run id. The shape of the ladder: the median decision latency
barely moves across the four levels (1.66 → 1.72 → 1.88 → 2.75 ms through
4× the load), while the tail bends — p95 grows 9× and p99 8× — and PAZ CPU
per pod scales almost exactly linearly with offered rate. That is the
queueing signature of rising utilization, not saturation: at the campaign
bound PAZ still held ~2.5 CPUs of headroom per pod.

## Configuration and safety notes

- `.env` holds credentials and is excluded from Git; profiles hold shape
  settings only, never secrets. `keys/` holds the subject signing key and is
  also excluded. The sideband shared secret is generated once and persisted
  to `.env` so PAZ and k6 always share one value.
- The subject signing key is a fixed demo artifact: one 10-year self-signed
  keypair, the key git-ignored in `keys/` and the derived JWKS document
  committed at `helm/paz/loadtest-jwks/jwks.json` (regenerate it with
  `scripts/generate-jwks.sh` if you ever replace the pair, then redeploy —
  the JWKS server serves the new key set and PAZ's validator fetches it).
- `INSECURE_SKIP_TLS_VERIFY=true` is suitable for the chart's internal
  self-signed TLS during a lab test. Install trusted certificates and set it
  to `false` for a production-like test.
- PAZ runs with ephemeral storage (no PVC) and rebuilds from the profile on
  every start; policy data persists in this repository (server-profiles/)
  and in PAP.

## Cleanup

```bash
./scripts/uninstall.sh              # removes the paz and loadtest releases
./scripts/uninstall.sh --purge-secrets  # also deletes the three Secrets
```

The namespace is retained so credentials and the signing key survive between
campaigns; delete it explicitly (`kubectl delete namespace paz-perf`) only
when nothing in it is needed anymore.

## Design history note

The original design (docs/superpowers/specs/2026-11-10-paz-sideband-perf-design.md)
shipped the `.deploymentpackage` as a file inside the server profile —
chosen for determinism in a measurement rig. Before any load ran, the
decision was revisited: a profile-carried policy artifact means every policy
change restarts the containers, which is the documented anti-pattern for
PAZ policy lifecycle (policy changes belong to the Deployment Manager
package-store flow; profiles are for server configuration). The stack
therefore consumes policy through a real S3 deployment-package store with a
non-zero poll interval — the production consumption model — while git
continues to carry only server configuration. The spec predates this
decision; this README is authoritative.
