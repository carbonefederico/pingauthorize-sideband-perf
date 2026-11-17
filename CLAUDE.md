# Project rules — pingauthorize-sideband-perf

## ⛔ KUBERNETES AUTHENTICATION — ABSOLUTE RULE
**Claude is NOT allowed to trigger Kubernetes authentication.** No
kubectl, no helm, no token refresh, no login attempt — directly, inside a
script, from a background watcher (`kubectl wait`, `logs -f`), or
implicitly through any command chain. Federico runs every cluster login
himself, manually.

Before ANY cluster command: confirm with Federico that his session is
fresh (sessions expire ~hourly; a prior "go" is a point-in-time grant,
never a session-long one). On ANY auth-flavored error (Unauthorized,
get-token, token refresh, expired): STOP IMMEDIATELY — never retry, never
re-run, never wait and retry. Report and hand the login to Federico.

Most verification needs no cluster access: run artifacts live in
`results/<run_id>/` and `evidence/` — read those first.

## Project specifics
- Scripts are the interface: `./scripts/foo.sh`, never a Makefile.
- macOS bash 3.2 only — no `declare -A`, no Homebrew bash in scripts/.
- `results/` and `evidence/` are git-ignored run artifacts; `reports/` is
  the tracked, reviewer-facing layer.
- The benchmark characterizes performance; it applies no performance
  acceptance criteria and computes no PASS/FAIL verdict — reports carry
  measurements and explicit integrity observations only.
