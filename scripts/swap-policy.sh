#!/usr/bin/env bash
# swap-policy.sh — publish a policy tree to PAZ and verify it is live.
#
# Usage: ./scripts/swap-policy.sh baseline|x100|x100-services
#
# The one-command tree swap between runs: builds the deployment package from
# the named PAP branch (POLICY_BRANCH), publishes it to the S3 package store,
# waits until BOTH PAZ replicas' logs confirm that exact package id, then
# runs the tree-identity probe. The probe asserts the NEGATIVE case too —
# /mcp/portfolio-2 must permit on the x100 tree and must NOT permit on the
# baseline tree — so a failed swap-back cannot pass on the previous tree's
# confirmation.
#
# Exits non-zero on any mismatch, printing the probe JSON lines.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_command kubectl
require_value PAP_ADMIN_PASSWORD

TREE="${1:-}"
case "${TREE}" in
  baseline)      BRANCH="AgentGatewayPortfolioMCP";               ENDPOINT_MODE="plain" ;;
  x100)          BRANCH="AgentGatewayPortfolioMCP-x100";          ENDPOINT_MODE="plain" ;;
  x100-services) BRANCH="AgentGatewayPortfolioMCP-x100-services"; ENDPOINT_MODE="parameterized" ;;
  *) echo "Usage: $0 baseline|x100|x100-services" >&2; exit 1 ;;
esac

echo "=== Swapping policy tree: ${TREE} (PAP branch ${BRANCH}, endpoint ${ENDPOINT_MODE}) ==="

# --- 1. publish the package through PAP -------------------------------------
build_output="$(POLICY_BRANCH="${BRANCH}" "${ROOT_DIR}/scripts/build-package.sh")"
package_id="$(echo "${build_output}" | sed -n 's/^PACKAGE_ID=//p')"
[[ -n "${package_id}" ]] || {
  echo "ERROR: build-package.sh did not report a PACKAGE_ID; cannot confirm the load." >&2
  exit 1
}
echo "Published package ${package_id}."

# --- 1b. endpoint shape must match the tree's dispatch model ----------------
"${ROOT_DIR}/scripts/set-sideband-endpoint.sh" "${ENDPOINT_MODE}"

# Record the mode so the NEXT boot (profile .subst layer) matches the tree
# now live: a pod restart must not resurrect the other shape.
if grep -q '^SIDEBAND_ENDPOINT_MODE=' "${ROOT_DIR}/.env" 2>/dev/null; then
  sed -i '' "s/^SIDEBAND_ENDPOINT_MODE=.*/SIDEBAND_ENDPOINT_MODE=${ENDPOINT_MODE}/" "${ROOT_DIR}/.env"
else
  printf '\n# Written by swap-policy.sh: endpoint shape matching the live tree\nSIDEBAND_ENDPOINT_MODE=%s\n' "${ENDPOINT_MODE}" >> "${ROOT_DIR}/.env"
fi

# --- 2. wait until BOTH PAZ replicas confirm that exact package id ----------
echo "Waiting for the PAZ nodes to load package ${package_id} (poll-interval 30s)..."
deadline=$((SECONDS + 180))
loaded0=0
loaded1=0
while (( SECONDS < deadline )); do
  # Match the id itself, not a bare 'Successfully deployed package' that an
  # earlier package's line would also satisfy.
  loaded0="$(kubectl -n "${NAMESPACE}" logs "pod/${PAZ_RELEASE}-pingauthorize-0" --since=5m 2>/dev/null | \
    grep -c "Successfully deployed package.*${package_id}\|${package_id}.*[Ss]uccessfully" || true)"
  loaded1="$(kubectl -n "${NAMESPACE}" logs "pod/${PAZ_RELEASE}-pingauthorize-1" --since=5m 2>/dev/null | \
    grep -c "Successfully deployed package.*${package_id}\|${package_id}.*[Ss]uccessfully" || true)"
  if [[ "${loaded0}" -ge 1 && "${loaded1}" -ge 1 ]]; then
    echo "Both PAZ replicas confirmed package ${package_id}."
    break
  fi
  sleep 10
done
if [[ "${loaded0}" -lt 1 || "${loaded1}" -lt 1 ]]; then
  echo "ERROR: the PAZ nodes did not confirm package ${package_id} within 3 minutes." >&2
  exit 1
fi

# --- 3. tree-identity probe (branch-2 leaf) ---------------------------------
kubectl -n "${NAMESPACE}" port-forward "pod/${PAZ_RELEASE}-pingauthorize-0" 18443:1443 > /dev/null 2>&1 &
pf_pid=$!
trap 'kill ${pf_pid} 2>/dev/null || true' EXIT
until nc -z localhost 18443 2>/dev/null; do sleep 0.5; done

probe_env=(
  PAZ_SIDEBAND_URL="https://localhost:18443/sideband/request"
  SIDEBAND_SHARED_SECRET="${SIDEBAND_SHARED_SECRET}"
  SUBJECT_ISSUER="${SUBJECT_ISSUER}"
  SUBJECT_SIGNING_KEY_PEM="$(cat "${SUBJECT_SIGNING_KEY_FILE}")"
)
probe() { # probe <mcp-aud> -> prints the probe's JSON line
  # Lab TLS: PAZ serves the chart's self-signed private certificate, and
  # Node refuses it without this (same override smoke.sh applies).
  NODE_TLS_REJECT_UNAUTHORIZED=0 env "${probe_env[@]}" MCP_AUD="$1" \
    node "${ROOT_DIR}/scripts/probe-branch.mjs"
}

# Tree-identity assertion per tree: on the baseline tree /mcp/portfolio-2
# is NOT applicable (PAZ serializes N/A as an embedded 4xx deny — the probe
# must NOT permit); on both x100 trees the branch-2 set exists and the
# request must permit. Per-tree, not permit-then-negate: a baseline swap
# correctly returns deny and must PASS here (review finding 2, 2026-11-24).
branch2="${MCP_AUD}-2"   # e.g. .../mcp/portfolio -> .../mcp/portfolio-2
probe_line="$(probe "${branch2}")"
echo "Probe ${branch2}: ${probe_line}"
case "${TREE}" in
  baseline)
    if [[ "${probe_line}" == *'"verdict":"permit"'* ]]; then
      echo "ERROR: ${branch2} permitted on the baseline tree — the loaded tree is NOT baseline (or a stale x100 package is still live)."
      exit 1
    fi
    ;;
  *)
    if [[ "${probe_line}" != *'"verdict":"permit"'* ]]; then
      echo "ERROR: tree-identity probe failed — ${branch2} is not a permit."
      echo "Loaded tree is NOT ${TREE} (or the probe itself failed: ${probe_line})."
      exit 1
    fi
    ;;
esac

trap - EXIT
kill "${pf_pid}" 2>/dev/null || true
echo "Swap complete: tree ${TREE} is live on both PAZ replicas and probes permit."
