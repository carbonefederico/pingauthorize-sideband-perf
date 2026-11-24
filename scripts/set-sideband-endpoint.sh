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
# Idempotent: no-ops when the named endpoint already carries the requested
# shape. Runs dsconfig inside pod ${PAZ_RELEASE}-pingauthorize-0.
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

DSCONFIG=(kubectl -n "${NAMESPACE}" exec "pod/${PAZ_RELEASE}-pingauthorize-0" -- \
  /opt/out/instance/bin/dsconfig -n --no-prompt)

echo "Setting sideband endpoint to ${MODE} (base-path ${BASE_PATH}, service ${SERVICE})..."
"${DSCONFIG[@]}" delete-sideband-api-endpoint --endpoint-name "${ENDPOINT_NAME}" 2>&1 | tail -1 || true
# resource-path only applies to the parameterized shape: {mcp} preserves the
# leaf as HttpRequest.ResourcePath. Without it the matched base path is
# consumed, ResourcePath resolves empty, and condition evaluation on it
# errors INDETERMINATE (live finding, 2026-11-24).
RESOURCE_PATH_ARGS=()
[[ "${MODE}" == "parameterized" ]] && RESOURCE_PATH_ARGS=(--set "resource-path:{mcp}")

"${DSCONFIG[@]}" create-sideband-api-endpoint \
  --endpoint-name "${ENDPOINT_NAME}" \
  --set "base-path:${BASE_PATH}" \
  --set "service:${SERVICE}" \
  "${RESOURCE_PATH_ARGS[@]}" \
  --set access-token-validator:PerfSubjectJWT 2>&1 | tail -1

"${DSCONFIG[@]}" list-sideband-api-endpoints 2>/dev/null | grep -F "${BASE_PATH}" | grep -F "${SERVICE}" \
  || { echo "ERROR: endpoint does not reflect the requested shape after creation" >&2; exit 1; }
echo "Endpoint is in ${MODE} shape."
