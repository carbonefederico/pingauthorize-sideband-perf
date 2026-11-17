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
the package once and sits off the decision path entirely.

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

- Each reference level is run **three times per campaign**, in ascending
  rounds (500 → 1000 → 1500 → 2000, repeated three times). The reference
  table reports the **median run** of the three, selected by measured p95,
  with the per-run p95s quoted alongside it.
- The benchmark applies **no performance acceptance criteria**: no p95
  bound, no success-rate floor, no achieved-rate floor, no verdict. There
  is no performance SLA to encode, and a completed experiment that shows
  poor performance is still a useful result. The reports are descriptive:
  measurements plus explicit collection observations and limitations.
  Missing evidence renders as "unavailable" — never a favorable default.
- The first run after a fresh deployment is discarded: one throwaway sanity
  run warms the PAZ pods before the campaign's first counted round.
- **The measurement window is the whole run, including the ramp** — the
  generator emits no phase filtering by design (the phase tags remain on
  every request, so a phase-scoped view can be computed from the raw
  streams later, but the published figures pool all samples). Every
  latency column in a run's `summary.json` pools raw per-request datapoints
  across all ten agents; percentiles are nearest-rank order statistics
  over the pooled samples (never averages of agent percentiles).
- Each run: ten parallel k6 agent pods, each driving RATE/10 through four
  ramped-arrival scenarios (read/session/HITL/deny at the campaign's
  weighted mix) that ramp 0→full rate over 30 s and then hold for
  5 minutes. The ramp replaces the constant-arrival step start, which
  stalled PAZ at every 2026-11-16 stage-2000 attempt (see the VU-sizing
  note under Step 2).
- **Each agent writes three artifacts**: the NDJSON metric stream
  (`agent-N-metrics.json`, every raw sample), k6's structured end-of-run
  summary (`agent-N-summary.json`, via `--summary-export` — the source for
  request/drop/error totals), and an exit-code sentinel
  (`/out/k6-exit-code`, written when k6 exits; the runner also records the
  pod's terminal exit code in `env.json`). The report never extracts
  measurement values from text logs.
- **Correctness is measured per request against the policy path under
  test**: permits for the permit fixtures, denials carrying each deny
  fixture's expected embedded status (403 for the delegation and
  oversized-order fixtures, 401 for the wrong-audience fixture), and a
  three-way classification (permit / deny / error) that keeps transport
  failures and malformed responses out of the decision statistics. The
  report shows expected-outcome percentage, classified errors, and
  incorrect decisions as separate columns.
- CPU columns come from the 15 s-cadence in-run sampler
  (`usage-during-run.txt`, written by `run-in-cluster.sh`), clipped to the
  observed traffic interval and **including the ramp** (consistent with
  the whole-run window). The median is per pod — each PAZ replica's median
  is reported separately in `summary.json` and in the report's CPU table.
- **Reports compute no verdict and parse no logs.** The generator reads
  env.json, the raw streams, the structured summaries, and the sampler
  output; everything else in the report is computed from those. It also
  records per-agent first/last response timestamps, observed response
  span, malformed-record counts, and a flat `limitations` list — the
  collection-observation layer that replaces gating.
- Server-side ground truth: an end-of-run scrape of PAZ's `cn=Monitor` Gateway
  Timing providers (best-effort — a scrape failure never invalidates a run).
- **The k6 script and report tooling are frozen for the campaign.** Any
  change to the load shape or measurement between counted runs makes runs
  incomparable; tooling changes happen between campaigns, never
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
dropped-iterations gate tripped on generator-side artifacts — but the
server-side events those bursts triggered (PAZ stalled for 8–22 s at the
transition, and once liveness-killed) are PAZ behavior and were not
server-side-investigated; the generator was the trigger, not the whole
story. The deciding
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
python3 scripts/generate-report.py <run_id>   # or a run-directory path
```

The generator is deliberately small (standard library only, no log
parsing, no verdicts). It reads `env.json`, the raw metric streams
(`agent-N-metrics.json[.gz]`), k6's structured summaries
(`agent-N-summary.json`), and the run-local sampler output, and writes:

- `results/<run_id>/report.html` — whole-run tables (requests observed vs
  expected volume, combined and per-scenario and per-agent latency with
  expected-outcome percentage, errors, and incorrect decisions as separate
  columns; full-call elapsed latency; per-agent execution status and
  response coverage; per-pod CPU/memory with sample counts), throughput
  and latency-over-time charts bucketed on absolute time, the pod-CPU
  chart, and the collection-observations list.
- `results/<run_id>/summary.json` — the machine-readable layer: per-agent
  artifacts and statistics, per-scenario and overall latency/correctness
  statistics (raw-sample pooled, nearest-rank percentiles), structured
  native totals (requests, iterations, dropped, classified errors),
  expected-vs-observed volume, per-pod CPU medians with sample counts, and
  the flat `limitations` list.

Copy the report to `reports/` when it is a keeper.

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
   **Token-uniqueness limitation (historical):** the campaign's `jti` was
   built from (user, per-VU iteration, second) without the agent identity
   or full VU id, so independent k6 agents — and, within one agent, VUs
   whose indices share the wrapped user index — could mint byte-identical
   tokens within the same second. Identical claims produce identical
   decisions, so no decision verdict is affected. Whether PAZ's validation
   cache absorbed duplicate tokens during the campaign, at what frequency,
   and with what effect on latency or CPU is **unknown** — the archive
   contains no cache-hit telemetry, and no directional claim ("slightly",
   "equally") is supported. The generator now includes run id, agent
   index, full VU id, and scenario in `jti` (fixed after the 2026-11-17
   reviews). The original campaign's figures stand as historical
   measurements of the documented workload with this recorded limitation;
   validating the corrected workload requires a fresh campaign with the
   fixed instrumentation.
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

## Reference results — campaign of 2026-11-17 (historical)

Twelve runs, three ascending rounds of the 500 → 1000 → 1500 → 2000
ladder, reprocessed with the final (oversimplified, verdict-free)
generator over the preserved raw streams and k6 structured summaries.
Each row is the median run of its level's three, selected by measured
p95; the per-run p95s quoted below the table show run-to-run spread.
The campaign is **historical**: it was captured by tooling that three
subsequent reviews found defective, and the instrumentation of record
 today (structured summaries, per-agent exit codes, per-request outcome
classification, full-call elapsed) postdates it. The reports issue **no
performance verdicts** — the benchmark characterizes performance at
specified loads; no performance SLA exists, so there are no pass/fail
criteria to apply. A pre-campaign sanity run was not needed — the PAZ
pods had been up and serving full-rate traffic since November 13.

**Measurement window: the whole run, including the 30 s ramp.** The
final generator deliberately ignores phase tags and pools every raw
sample; request counts and latency figures below therefore describe the
ramp plus the 5-minute plateau, and average throughput sits slightly
below the plateau target by construction (a linear ramp contributes half
its rate on average). Phase-scoped (plateau-only) views remain
computable from the preserved streams; the figures here are the
whole-run ones the report defines.

| Stage | Target rate | Requests (whole run) | avg | median | p90 | p95 | p99 | PAZ CPU/pod | Agent CPU |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 500/s | 500/s | 157,460 | 1.86 ms | 1.66 ms | 2.29 ms | 2.86 ms | 6.23 ms | 420 m | 160 m |
| 1000/s | 1000/s | 314,960 | 2.11 ms | 1.72 ms | 2.89 ms | 4.20 ms | 8.89 ms | 856 m | 327 m |
| 1500/s | 1500/s | 472,456 | 3.01 ms | 1.88 ms | 5.32 ms | 8.38 ms | 20.41 ms | 1,326 m | 492 m |
| 2000/s | 2000/s | 629,960 | 6.52 ms | 2.70 ms | 15.50 ms | 25.80 ms | 51.44 ms | 1,834 m | 696 m |

Per-run p95s by level: 500/s — 2.78 / 2.86 / 3.23 ms (median run
`20261117110514`); 1000/s — 4.14 / 4.20 / 5.27 ms (median run
`20261117103702`); 1500/s — 8.18 / 8.38 / 13.05 ms (median run
`20261117112114`); 2000/s — 19.05 / 25.80 / 27.14 ms (median run
`20261117105622`). Observed request counts track the expected volume for
a linear ramp (target × (duration + ramp/2)) within 0.1% at every level.
CPU medians are per pod over the whole-run interval from the 15 s
in-run sampler (`cpu_source: usage-during-run.txt` in every summary);
each PAZ replica's median is separate in `summary.json`, and the table
quotes their mean.

**Limitations of these historical measurements** (each run's
`summary.json` carries its own `limitations` list; these are the
campaign-wide ones):

- **Per-scenario outcome samples are missing.** The streams predate the
  per-request success/error tagging, so the outcome columns read
  "unavailable" and only k6's end-of-run aggregates (via the structured
  summaries) speak to decision outcomes at all. HTTP transport failures
  were zero in all runs; malformed-response errors would not have been
  distinguishable from denials with the classification in force at
  capture time.
- **Per-agent exit codes are unavailable** (the archive predates the
  sentinel and pod-status capture); execution status reads "unavailable"
  per agent.
- **`sideband_request_elapsed` is unavailable** — the full-call metric
  postdates this campaign; reprocessing cannot recover a custom metric
  never captured, so the full-call elapsed section is "unavailable"
  throughout.
- **Token-uniqueness.** `jti` was built without agent/VU identity, so
  duplicate tokens could occur within the same second; whether PAZ's
  validation cache absorbed them, and with what effect, is unknown (see
  Benchmark assumption 2).

A fresh campaign with the final instrumentation — structured summaries,
per-agent exit codes, per-request outcome classification, full-call
elapsed — is required to produce measurements with complete evidence.

Measurement definitions, precisely: the latency columns pool every raw
`http_req_duration` datapoint across all ten agents — per-agent
steady-state pooling, not a simultaneous aggregate-load window (agents
start within seconds of each other; the report charts bucket every raw
request timestamp directly against one common absolute-time origin).
Percentiles are nearest-rank order statistics over the pooled samples.
The headline latency is k6's `response.timings.duration` — sending +
waiting + receiving, excluding connection establishment, which
steady-state connection reuse amortizes to each VU's first iterations;
full-call elapsed (including connect) is a separate metric unavailable
in this campaign. Correctness counts come from the per-request
classification: expected-outcome percentage over success samples,
classified errors (transport/malformed), and incorrect decisions
(unexpected verdicts excluding errors) as separate figures. CPU medians
are per pod over the whole-run interval; the historical publication's
figures differed (a value-sorted trimmed mean over all samples, then a
30 s-late window) before settling on this definition.

The shape of the ladder: the median decision latency barely moves across
the four levels (1.66 → 1.72 → 1.88 → 2.70 ms through 4× the load), while
the tail bends — p95 grows 9× and p99 8× — and PAZ CPU per pod scales
almost exactly linearly with offered rate. This pattern is *consistent
with* rising utilization as the cause (a queueing interpretation), but the
campaign measured no per-replica queue depths, thread pools, GC telemetry,
or CPU throttling, so it remains a hypothesis; the CPU figures alone do
not rule out internal saturation. At the campaign bound the measured
1,834 m mean-of-medians sits at 46% of the pods' 4-CPU limit — but the
pods share 3.4-allocatable-CPU nodes with co-tenants, so the limit is a
ceiling, not a guaranteed capacity: headroom against the *node* is
smaller than headroom against the limit.

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
