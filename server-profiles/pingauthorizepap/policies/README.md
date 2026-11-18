# Portfolio MCP policies — deployment runbook

`AgentGatewayPortfolioMCP.SNAPSHOT` is a complete policy branch taken from a
working MCP-gateway authorization policy, so this
campaign measures the identical policy ladder that gateway integration
evaluates. The bootstrap hook
(`../hooks/81-install-policies.sh`) imports it
into a branch named after the snapshot file and COMMITS it — PAP branches
deploy directly, and the PDP serves only committed snapshots.

The two snapshot-import contracts to know: importing
a snapshot ALWAYS creates a branch — the name in the import URL is the new
branch's name, never an existing branch id (a taken name 409s); and an
uncommitted working copy is invisible to the PDP. The hook is restart-safe
(it skips when the branch is already COMMITTED).

This project's PAZ never reads the branch at runtime: the deployment package
(scripts/build-package.sh, rooted at the snapshot's Global Decision Point
`a7c4e2d0-8f11-4b5a-9c3d-6e8f2a1b4c7d`) carries the policy state into the
PAZ process. PAP exists to build and rebuild that package; keep the branch
committed so rebuilds start from a known state.
