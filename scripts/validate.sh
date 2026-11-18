#!/usr/bin/env bash
# validate.sh — the project's front door: every offline check in one run.
# Prints a one-line PASS/FAIL per check and exits non-zero on any failure.
# Cluster-side verification (deploy/verify/smoke) is separate — see README.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

fail=0
check() { # check <label> <command...>
  local label="$1"; shift
  if "$@" > /dev/null 2>&1; then
    echo "PASS: ${label}"
  else
    echo "FAIL: ${label}"
    fail=1
  fi
}

check "node unit tests (envelope, claims, decision)" npm test
check "python reporter unit tests" python3 "${ROOT_DIR}/test/unit/report_replacements_test.py"
render_wrapper() { # render_wrapper <release> <set-arg>
  # Render the project's WRAPPER chart (helm/paz: the upstream ping-devops
  # dependency plus this project's JWKS server), not the upstream chart
  # alone — the wrapper's own resources and the nested override structure
  # are what a deployment actually runs. helm lint on chart 0.15.0
  # mis-parses its own multi-doc secret-cert.yaml (lint-only quirk), so the
  # check is a full client render.
  helm dependency build "${ROOT_DIR}/helm/paz" > /dev/null 2>&1
  helm template "$1" "${ROOT_DIR}/helm/paz" --set-string "$2" > /dev/null 2>&1
}
check "render helm/paz wrapper (deps + JWKS + values)" render_wrapper test "ping-devops.pingauthorize.envs.SERVER_PROFILE_URL=https://example.invalid/x.git"
check "helm lint helm/loadtest" bash -c "helm lint helm/loadtest > /dev/null"
# The k6 script is ES-module JavaScript: node --check validates its syntax
# offline. When a k6 binary exists, also run the 1 s dry-run (exit 0 green,
# 99 = thresholds crossed with nothing listening; both prove execution — a
# script exception exits 107 and fails).
check "k6 script syntax (node --check)" node --check "${ROOT_DIR}/helm/loadtest/files/sideband-decision.js"
k6_dry_run() {
  env \
    PAZ_SIDEBAND_URL=https://localhost:1/sideband/request \
    SIDEBAND_SHARED_SECRET=x \
    MCP_AUD=https://x/mcp/portfolio \
    SUBJECT_ISSUER=https://x \
    SUBJECT_SIGNING_KEY="$(cat "${ROOT_DIR}/keys/subject-signing.key")" \
    RATE=250 AGENTS=10 DURATION=1s WARMUP=1s \
    k6 run --no-usage-report -q \
    "${ROOT_DIR}/helm/loadtest/files/sideband-decision.js" > /dev/null 2>&1
  local code=$?
  [[ ${code} -eq 0 || ${code} -eq 99 ]]
}
if command -v k6 > /dev/null 2>&1; then
  check "k6 script compiles (1s dry-run)" k6_dry_run
else
  echo "SKIP: k6 binary not installed — dry-run check skipped (syntax still checked above)"
fi
for script in scripts/*.sh; do
  check "bash -n ${script}" bash -n "${script}"
done
check "node syntax smoke.mjs" node --check scripts/smoke.mjs
check "node syntax smoke-helpers.mjs" node --check scripts/smoke-helpers.mjs
check "python3 report generator compiles" python3 -m py_compile scripts/generate-report.py

if [[ ${fail} -eq 0 ]]; then
  echo "All checks passed."
else
  echo "FAILURES above — fix before deploying."
fi
exit "${fail}"
