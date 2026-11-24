#!/usr/bin/env bash
# set-sideband-endpoint.sh — swap the PAZ sideband endpoint between the two
# dispatch shapes the scaling campaign measures.
#
# Usage: ./scripts/set-sideband-endpoint.sh parameterized|plain
#
#   plain          base-path:/mcp         service:AgentGateway
#                  (the campaign of record's endpoint: dispatch by URL leaf
#                  conditions inside the tree)
#   parameterized  base-path:/mcp/{mcp}   service:AgentGateway {mcp}
#                  (Ping's documented dispatch shape: the endpoint derives
#                  the per-MCP service per request; the tree's policy sets
#                  are targeted at those services)
#
# The service template is 'AgentGateway {mcp}': {mcp} captures the full
# leaf ('portfolio-2'), so the derived value is 'AgentGateway portfolio-2'
# — the exact ServiceDefinition name. A literal 'portfolio-' in the
# template double-prints it ('AgentGateway portfolio-portfolio-2', live
# finding 2026-11-24). No periods: PAP rejects periods in names.
#
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_command kubectl

MODE="${1:-}"
case "${MODE}" in
  parameterized) BASE_PATH='/mcp/{mcp}'; SERVICE='AgentGateway {mcp}' ;;
  plain)         BASE_PATH='/mcp';       SERVICE='AgentGateway' ;;
  *) echo "Usage: $0 parameterized|plain" >&2; exit 1 ;;
esac
ENDPOINT_NAME="AgentGateway Portfolio MCP"

# dsconfig talks to ONE server's mirrored-config backend; the change replicates
# asynchronously and PAZ-1 demonstrably served a stale endpoint under live traffic
# (finding, 2026-11-24: half the decisions hit the old shape). Apply to BOTH replicas
# and verify on each.
RESOURCE_PATH_ARGS=()
[[ "${MODE}" == "parameterized" ]] && RESOURCE_PATH_ARGS=(--set "resource-path:{mcp}")

for pod_idx in 0 1; do
  DSCONFIG=(kubectl -n "${NAMESPACE}" exec "pod/${PAZ_RELEASE}-pingauthorize-${pod_idx}" -- \
    /opt/out/instance/bin/dsconfig -n --no-prompt)
  echo "Setting sideband endpoint on ${PAZ_RELEASE}-pingauthorize-${pod_idx} to ${MODE} (base-path ${BASE_PATH}, service ${SERVICE})..."
  "${DSCONFIG[@]}" delete-sideband-api-endpoint --endpoint-name "${ENDPOINT_NAME}" 2>&1 | tail -1 || true
  "${DSCONFIG[@]}" create-sideband-api-endpoint \
    --endpoint-name "${ENDPOINT_NAME}" \
    --set "base-path:${BASE_PATH}" \
    --set "service:${SERVICE}" \
    ${RESOURCE_PATH_ARGS[@]+"${RESOURCE_PATH_ARGS[@]}"} \
    --set access-token-validator:PerfSubjectJWT 2>&1 | tail -1
  "${DSCONFIG[@]}" list-sideband-api-endpoints 2>/dev/null | grep -F "${BASE_PATH}" | grep -F "${SERVICE}" \
    || { echo "ERROR: endpoint does not reflect the requested shape on replica ${pod_idx}" >&2; exit 1; }
done
echo "Endpoint is in ${MODE} shape on both replicas."
