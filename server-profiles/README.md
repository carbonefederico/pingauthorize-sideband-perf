# Server profiles — PAZ sideband performance stack

The server profiles configure both Ping products at boot. The Ping images
clone this project's GitHub repository (the `SERVER_PROFILE_URL` in `.env`)
and apply `server-profiles/` on every start; nothing is configured by hand.
This profile tree is part of the project repository — there is no separate
profile repo. The stack runs PAZ in **embedded PDP mode fed by an S3
deployment package store** — the production consumption model: PAP authors
the policy, `build-package.sh` publishes the package to S3, and the PAZ nodes
poll and load it without a restart. PAP is off the decision path.

## Tree

```
pingauthorize/            PAZ (the measured node, 2 replicas)
├── pd.profile/dsconfig/
│   ├── 20-token-validator.dsconfig.subst   PerfSubjectJWT: a JWKS validator (RS256) over the in-cluster JWKS server
│   ├── 30-embedded-pdp.dsconfig.subst      pdp-mode:embedded + the S3 deployment package store (poll-interval 30s)
│   └── 40-sideband.dsconfig.subst          sideband shared secret + endpoint (AgentGateway service, /mcp)
└── hooks/                                  (none — the chart's stock hooks apply the pd.profile layer)

pingauthorizepap/         PAP (package authoring only; idle during load runs)
├── policies/
│   ├── AgentGatewayPortfolioMCP.SNAPSHOT   the full policy branch (a working MCP-gateway policy, see policies/README.md)
│   ├── AgentGatewayPortfolioMCP-x100.SNAPSHOT  the 100-branch tree, condition-gated (scaling campaign variant)
│   ├── AgentGatewayPortfolioMCP-x100-services.SNAPSHOT  the 100-branch tree, service-targeted, Ping's documented dispatch shape (generated)
│   └── README.md
└── hooks/81-install-policies.sh            imports + commits each staged snapshot into its own branch at boot
```

The policy snapshot and the PAP bootstrap hook reproduce a working
MCP-gateway authorization policy (`AgentGatewayPortfolioMCP`), because this
campaign measures the same policy ladder a real gateway integration
evaluates. The two snapshot-import contracts that shaped the hook, both
preserved here: importing a snapshot always
creates a NEW branch named in the URL (a taken name 409s), and the PDP serves
only COMMITTED snapshots — the hook commits after import, and PAZ's policy
data comes from the deployment package published on top of that commit.
The hook is snapshot-generic: it loops over every staged `*.SNAPSHOT` file,
so the two snapshots import as two sibling PAP branches that coexist
committed (`AgentGatewayPortfolioMCP` and `AgentGatewayPortfolioMCP-x100`).

The x100 snapshot is generated, not hand-authored:
`python3 scripts/generate-x100-snapshot.py` reads the baseline snapshot and
emits `AgentGatewayPortfolioMCP-x100.SNAPSHOT` deterministically (uuid5 ids,
recomputed stream digest), so regenerating is byte-identical and the
baseline file is never modified. Both snapshots share the same Global
Decision Point id (`a7c4e2d0-8f11-4b5a-9c3d-6e8f2a1b4c7d`) because the two
trees never coexist in a loaded package — `build-package.sh` selects the
tree with `POLICY_BRANCH=<branch name>`, and `swap-policy.sh` publishes a
tree and probes both replicas before a run starts. The services variant is
driven through a parameterized sideband endpoint
(`base-path:/mcp/{mcp}`, `service:AgentGateway {mcp}`,
`resource-path:{mcp}`). The endpoint shape is profile configuration:
`40-sideband.dsconfig.subst` boots the shape named by
`SIDEBAND_ENDPOINT_MODE` (plain | parameterized; default plain, resting
state parameterized), and `scripts/set-sideband-endpoint.sh` applies the
same change live on both replicas between runs — a live-only change does
not survive a pod restart, so the profile and the live state must agree.
After the scaling campaign the deployment rests on the parameterized
endpoint with the x100-services tree published.

## Deploy order (scripts/deploy.sh drives this)

0. `./scripts/setup-aws.sh` (once, logged into AWS): creates the versioned,
   private S3 bucket, a least-privilege IAM user, and writes the credentials
   into `.env`. This is the artifact plane — policy packages live in the
   bucket, not in git.
1. `./scripts/create-secrets.sh` (also runs inside deploy.sh): builds the
   TLS secrets and the `paz-secrets`/`paz-s3`/`paz-loadtest` Secrets.
2. One Helm release installs BOTH products together
   (`helm/paz/values.yaml` drives the upstream chart): PAP (whose boot hook
   imports and commits `AgentGatewayPortfolioMCP`) and the two PAZ replicas
   (which boot in embedded-PDP mode and register the S3 package store).
   deploy.sh waits for both to become Ready and runs `verify.sh`.
3. `./scripts/build-package.sh` publishes the policy package entirely
   through PAP's API: export the committed branch head snapshot → create a
   package rooted at the Global Decision Point → POST it to PAP's
   package-store endpoint — PAP writes the package bytes AND the pointer
   file into the S3 bucket (the same two objects the Deployment Manager
   UI's Publish action writes) → download an inspection copy into
   `results/`. deploy.sh then waits until both PAZ replicas' logs confirm
   the published package id.
4. From then on the PAZ poller picks up any later `build-package.sh` run
   within its 30 s interval — no restart. PAP stays deployed but idle; no
   decision ever touches it.

## Version-specific verifications (record your findings here)

These values depend on the pinned image (2607) and the profile overlay
layout. Verify each on first deploy and write what you found under its
heading; adjust the dsconfig files if reality differs.

### In-container server root for profile data files

The profile layers deploy under `/tmp/server-profile/server-profiles/` in
the PAZ container (the chart's profile mount). Verify on the running PAZ
with `kubectl exec <pod> -- find / -name '40-sideband*' 2>/dev/null`.
Finding (fill in on first deploy):

### Deployment package store property spellings

`dsconfig create-deployment-package-store --help` and
`dsconfig set-policy-decision-service-prop --help` on the running PAZ — this
profile uses `--type s3` with `poll-interval`, `s3-bucket-name`,
`s3-bucket-prefix`, `s3-region-name`, `s3-access-key-id`, `s3-secret-key`,
and the PDS properties `deployment-package-source-type:store` +
`deployment-package-store:<name>`. Documentation shows property-name drift
across versions; confirm against the image. Finding (fill in on first
deploy):

### Monitor providers

The Gateway Timing / Gateway Request Type Timing monitor providers may ship
enabled or need a dsconfig stanza on this image. Check
`bin/ldapsearch --baseDN cn=monitor "(cn=*Gateway*)" dn` on the running PAZ;
if absent, add the stanza per the plan's Task 3 Step 4 note (enabled once for
the whole campaign). Finding (fill in on first deploy):

## Policy constants baked in the snapshot

The policy tree targets these literals; the load generator must match them
(changing either side changes what is measured):

- **Service**: `AgentGateway` (the sideband endpoint's `service` property; the
  snapshot's top wrapper PolicySet targets it — 1 occurrence verified by
  grep in the snapshot).
- **MCP audience** (`Constants.Portfolio_MCP Audience`, an attribute constant
  resolver): `https://agentgateway-stack-ag.ping-devops.com/mcp/portfolio`.
  This is `.env`'s `MCP_AUD` default; tokens minted for any other audience
  fail the audience gate.
- **Global Decision Point id** (package root for build-package.sh):
  `a7c4e2d0-8f11-4b5a-9c3d-6e8f2a1b4c7d` (verified present in the snapshot).
- **Branch leaves and audiences (x100 snapshot only)**: branch 1 is the
  baseline's `portfolio` leaf and audience unchanged; branches 2..100 carry
  leaf `portfolio-<n>` and audience
  `https://agentgateway-stack-ag.ping-devops.com/mcp/portfolio-<n>`, and the
  load generator derives both from `MCP_AUD` per request
  (`branchAudience` in `helm/loadtest/files/envelope.mjs`).
