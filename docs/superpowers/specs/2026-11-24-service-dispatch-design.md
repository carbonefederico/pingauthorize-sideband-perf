# Design: service-dispatch variant of the policy-tree scaling campaign

## Intent and why

Ping's documentation recommends dispatching sideband requests to policies
through the endpoint's parameterized `service` attribute (base-path
parameters, service-targeted policy sets) rather than condition-matching
inside the tree, and documents `FirstApplicable`'s sequential sibling scan
as the cost driver that dispatch-by-service avoids. The condition-gated
x100 campaign measured that scan directly: median latency rose from
2.53 ms (branch 1) to 3.65 ms (branch 100) across the 100 condition-gated
siblings. This design adds the recommended shape as a third measured
configuration and makes it the deployment's resting state, per Federico:
"the one that we are just running which is closer to reality."

The two existing measurements stand unchanged (baseline 1-branch run
`20261124125701`, condition-gated x100 run `20261124132419`, both today at
1000/s); this campaign adds one run of the service-targeted x100 tree at
the same level. One variable moves at a time: tree shape only.

## Goals

- Generate `AgentGatewayPortfolioMCP-x100-services.SNAPSHOT`: 100 services,
  each `Portfolio MCP` set clone targeted at its own service, no leaf
  conditions on the clones — the Ping-recommended dispatch shape.
- Drive it through a parameterized sideband endpoint
  (`base-path:/mcp/{mcp}`, `service:AgentGateway.{mcp}`).
- Measure one run at 1000/s (`BRANCH_COUNT=100`) and compare against the
  two completed runs: does endpoint-side dispatch remove the in-tree scan?
- End the campaign with the recommended shape live (endpoint swap + tree
  published + probe green), as the deployment's resting state.

## Non-goals

- No changes to the campaign of record, the baseline snapshot, the
  condition-gated x100 snapshot (it stays committed as the measured
  intermediate), or the already-collected runs.
- No re-measurement of the baseline tree (today's run 20261124125701 is
  the baseline for all comparisons).
- No new load levels; 1000/s only, one run (the two prior configurations
  also have one run each at this level today).
- No policy-content diversity, attribute-space growth, or saturation
  testing — same exclusions as the scaling campaign.

## Design decisions

- **Generator services mode.** `generate-x100-snapshot.py` gains a
  `--services` flag emitting the services variant: per branch i (2..100), a
  `ServiceDefinition` named `AgentGateway portfolio-i`, a per-branch Target
  element targeting that service, and the `Portfolio MCP` set clone with
  `targets` pointing at that Target instead of a leaf condition. Branch 1
  keeps the original condition-gated set (it is how the baseline tree
  serves `/mcp/portfolio`; the wrapper set still targets `AgentGateway`).
  Same deterministic uuid5 ids (namespace salted with the mode), same
  digest recomputation, baseline untouched. Unit tests mirror the x100
  suite: determinism, structure (161 + 99×40 elements: subtree + service
  + target + audience attr), tree wiring (targets resolve, no leaf
  condition on clones 2..100), id hygiene.
- **Parameterized endpoint.** The dsconfig layer gains a `--services`
  variant of the sideband endpoint block: one endpoint
  `base-path:/mcp/{mcp}`, `service:AgentGateway.{mcp}`, same validator and
  secret. The endpoint and the tree swap together (below) because the
  baseline tree's wrapper set targets service `AgentGateway` exactly, and
  a parameter-derived `AgentGateway.portfolio` cannot be assumed to match
  it (exact-match is the safe assumption; not verified against docs).
- **Swap mechanism extended.** `swap-policy.sh` gains a third target,
  `x100-services`, which: publishes the services snapshot's package,
  applies the endpoint dsconfig change (a small `scripts/set-sideband-endpoint.sh`
  driven by an env `SIDEBAND_ENDPOINT_MODE=parameterized|plain`, or the
  dsconfig lines inline in the swap script — builder's call, one command
  surface), waits for package confirmation on both replicas, and probes:
  `/mcp/portfolio-2` must permit with a token minted for the branch-2
  audience; on `baseline` the probe must not permit. The existing
  `baseline` and `x100` targets keep their current behavior, plus endpoint
  restoration (plain endpoint) so every swap leaves a coherent
  (endpoint, tree) pair.
- **k6 untouched.** URL and aud threading are already generic
  (`branchAudience`, per-request branch draw, branch tags). The profile
  stays `scale-100.env`.
- **Report.** `compare-scaling-runs.py` already takes any run list; the
  comparison table gains a third row
  (baseline | condition-gated x100 | service-targeted x100). The README's
  scaling-campaign section gains the third configuration and the docs
  citation (endpoint dispatch per Ping's sideband + path-parameters docs;
  FirstApplicable sibling-scan cost per the combining-algorithms doc).
- **Resting state.** After the run, the deployment stays on
  x100-services + parameterized endpoint (probe green). Documented in the
  server-profiles README alongside the dsconfig note.

## Constraints

- Kubernetes auth rules as always: Federico runs logins; stop on any
  auth-flavored error. Session fresh 2026-11-24.
- No performance acceptance criteria; no verdicts — measurements only.
- The wrong-audience deny fixture strips `/mcp/.*` to the gateway root in
  the token aud only; the URL keeps the branch, so the parameterized
  endpoint still binds the request to the branch service — semantics
  unchanged.

## Left to the builder

- Whether the endpoint swap runs inline in swap-policy.sh or as a small
  separate script (one command surface from the operator's view either way).
- Exact dsconfig invocations for the parameterized endpoint (the dsconfig
  `.subst` templating layer's variable conventions).
- Report wording for the three-configuration comparison.
