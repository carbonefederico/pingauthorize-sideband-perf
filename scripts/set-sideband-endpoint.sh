#!/usr/bin/env bash
# set-sideband-endpoint.sh — swap the PAZ sideband endpoint between the two
# dispatch shapes the scaling campaign measures.
#
# Usage: ./scripts/set-sideband-endpoint.sh parameterized|plain
#
#   plain          base-path:/mcp         service:AgentGateway
#                  (the campaign of record's endpoint: dispatch by URL leaf
#                  conditions inside the tree)
#   parameterized  base-path:/mcp/{mcp}   service:AgentGateway portfolio-{mcp}
#                  (Ping's documented dispatch shape: the endpoint derives
#                  the per-MCP service per request; the tree's policy sets
#                  are targeted at those services)
#
# The service template uses a SPACE, not a period: PAP rejects periods in
# ServiceDefinition names (Task 2, 2026-11-24), so the derived values must
# be 'AgentGateway portfolio-<i>' to match the x100-services tree.
#
# Idempotent: no-ops when the named endpoint already carries the requested
# shape. Runs dsconfig inside pod ${PAZ_RELEASE}-pingauthorize-0.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_command kubectl

MODE="${1:-}"
case "${MODE}" in
  parameterized) BASE_PATH='/mcp/{mcp}'; SERVICE='AgentGateway portfolio-{mcp}' ;;
  plain)         BASE_PATH='/mcp';       SERVICE='AgentGateway' ;;
  *) echo "Usage: $0 parameterized|plain" >&2; exit 1 ;;
esac
ENDPOINT_NAME="AgentGateway Portfolio MCP"

DSCONFIG=(kubectl -n "${NAMESPACE}" exec "pod/${PAZ_RELEASE}-pingauthorize-0" -- \
  /opt/out/instance/bin/dsconfig -n --no-prompt)

echo "Setting sideband endpoint to ${MODE} (base-path ${BASE_PATH}, service ${SERVICE})..."
"${DSCONFIG[@]}" delete-sideband-api-endpoint --endpoint-name "${ENDPOINT_NAME}" 2>&1 | tail -1 || true
"${DSCONFIG[@]}" create-sideband-api-endpoint \
  --endpoint-name "${ENDPOINT_NAME}" \
  --set "base-path:${BASE_PATH}" \
  --set "service:${SERVICE}" \
  --set access-token-validator:PerfSubjectJWT 2>&1 | tail -1

"${DSCONFIG[@]}" list-sideband-api-endpoints 2>/dev/null | grep -F "${BASE_PATH}" | grep -F "${SERVICE}" \
  || { echo "ERROR: endpoint does not reflect the requested shape after creation" >&2; exit 1; }
echo "Endpoint is in ${MODE} shape."
