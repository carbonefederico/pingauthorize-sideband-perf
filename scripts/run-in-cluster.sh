#!/usr/bin/env bash
# run-in-cluster.sh — provision the k6 Job, stream it, and collect results
# into results/<run_id>/. Ported from the PF campaign's runner; the changes
# are the SUT inventory (2 PAZ pods, label pingauthorize) and the metric
# stream file names. Exits non-zero if the k6 Job fails (script or collection
# failure), after saving everything the report needs.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_command kubectl
require_command helm
"${ROOT_DIR}/scripts/create-secrets.sh"

agent_count="${AGENTS:-1}"
run_id="$(date -u +%Y%m%d%H%M%S)"

helm_args=(
  --namespace "${NAMESPACE}"
  --set-string runId="${run_id}"
  --set-string agentCount="${agent_count}"
)

# Everything the report generator needs lands in results/<run_id>/: the
# per-agent k6 logs and metric streams (the pods TTL-clean after 1h, so
# capture them here), plus the pod-usage CSV if the monitor is running.
run_dir="${ROOT_DIR}/results/${run_id}"
mkdir -p "${run_dir}"

# Run metadata: every resolved variable that defines the run, plus the
# cluster context and the SUT pod inventory. No performance thresholds are
# captured: the benchmark characterizes performance and applies no
# acceptance criteria (fourth review).
{
  echo "{"
  echo "  \"run_id\": \"${run_id}\","
  echo "  \"profile\": \"${PROFILE:-<none, .env only>}\","
  echo "  \"timestamp_utc\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
  for v in AGENTS RATE DURATION WARMUP PRE_ALLOCATED_VUS MAX_VUS \
           SUBJECT_ISSUER SUBJECT_USER_COUNT MCP_AUD NAMESPACE PAZ_RELEASE LOADTEST_RELEASE; do
    [[ -n "${!v:-}" ]] && printf '  "%s": "%s",\n' "${v}" "${!v}"
  done
  echo "  \"k6_image\": \"$(helm -n "${NAMESPACE}" get values "${LOADTEST_RELEASE}" -a 2>/dev/null | grep -oE 'repository: .*|tag: .*' | tr -d ' ' | paste -sd':' - | head -1 || echo unknown)\","
  echo "  \"nodes\": ["
  kubectl get nodes -o json 2>/dev/null | python3 -c "
import json, sys
for n in json.load(sys.stdin)['items']:
    print('    ' + json.dumps({'name': n['metadata']['name'], 'instance_type': n['metadata']['labels'].get('beta.kubernetes.io/instance-type', '?'), 'allocatable_cpu': n['status']['allocatable']['cpu']}) + ',')
" 2>/dev/null || true
  echo "  ],"
  echo "  \"paz_pods\": ["
  kubectl -n "${NAMESPACE}" get pods -l "app.kubernetes.io/instance=${PAZ_RELEASE},app.kubernetes.io/name=pingauthorize" -o json 2>/dev/null | python3 -c "
import json, sys
for p in json.load(sys.stdin)['items']:
    print('    ' + json.dumps({'name': p['metadata']['name'], 'node': p['spec']['nodeName'], 'started': p['status']['startTime']}) + ',')
" 2>/dev/null || true
  echo "  ]"
  echo "}"
} > "${run_dir}/env.json"
# Strip trailing commas before '}' or ']' (the looped printf emits trailing commas).
python3 -c 'import re,sys; p=sys.argv[1]; t=open(p).read(); open(p,"w").write(re.sub(r",(\s*[}\]])", lambda m: m.group(1), t))' "${run_dir}/env.json"

helm -n "${NAMESPACE}" uninstall "${LOADTEST_RELEASE}" >/dev/null 2>&1 || true
helm -n "${NAMESPACE}" install "${LOADTEST_RELEASE}" "${ROOT_DIR}/helm/loadtest" "${helm_args[@]}"

job_name="${LOADTEST_RELEASE}-${run_id}"
echo "Following ${job_name}; the command exits non-zero if k6 fails to run or the Job fails."
kubectl -n "${NAMESPACE}" wait --for=condition=Ready pod \
  -l "job-name=${job_name}" --timeout=5m

# Results collection happens AFTER the load completes: the k6 container holds
# its pod alive for COLLECT_GRACE_SECONDS after k6 exits (see job.yaml), so
# the complete, flushed k6.json can be copied once per pod — no mid-run
# snapshot race, no datapoint loss.
echo "Waiting for load to finish (${agent_count} agents)..."
# Plain indexed array, not an associative one: macOS ships bash 3.2, which
# has no `declare -A`, and the collector died on that line right after the
# pods went Ready (2026-11-16, stage-1500).
collected=()
deadline=$((SECONDS + 1200))

# In-load resource sampler: kubectl top at 15s cadence for the whole loadtest
# duration, into results/<run_id>/. Exists because stage-2000 failed with a
# generator-side CPU starvation that could only be inferred post-run — the
# five slow agents' pods were gone by the time we looked. This makes
# starvation visible DURING the run (agent CPU pegged, PAZ idle).
(
  while [[ ${SECONDS} -lt 1200 ]]; do
    top_line="$(kubectl -n "${NAMESPACE}" top pods \
      -l "job-name=${job_name}" \
      --no-headers 2>/dev/null)" || true
    paz_line="$(kubectl -n "${NAMESPACE}" top pods \
      -l "app.kubernetes.io/instance=${PAZ_RELEASE},app.kubernetes.io/name=pingauthorize" \
      --no-headers 2>/dev/null)" || true
    {
      echo "=== $(date -u +%H:%M:%S) ==="
      echo "agents: ${top_line:-<none>}"
      echo "paz:    ${paz_line:-<none>}"
    } >> "${run_dir}/usage-during-run.txt"
    sleep 15
  done
) &
sampler_pid=$!
trap 'kill "${sampler_pid}" 2>/dev/null || true' EXIT

while [[ ${SECONDS} -lt ${deadline} ]]; do
  all_done=1
  for i in $(seq 0 $((agent_count - 1))); do
    [[ "${collected[$i]:-}" == "yes" ]] && continue
    pod="$(kubectl -n "${NAMESPACE}" get pods -l "job-name=${job_name}" \
      -o jsonpath="{range .items[?(@.metadata.annotations['batch\.kubernetes\.io/job-completion-index']=='${i}')]}{.metadata.name}{end}" 2>/dev/null)"
    if [[ -z "${pod}" ]]; then
      all_done=0
      continue
    fi
    # The sentinel is written only after k6 has exited and closed its output.
    if kubectl -n "${NAMESPACE}" exec "${pod}" -c k6 -- test -f /out/k6-exit-code 2>/dev/null; then
      if kubectl -n "${NAMESPACE}" cp "${pod}:/out/k6.json" "${run_dir}/agent-${i}-metrics.json" -c k6 >/dev/null 2>&1 \
        && kubectl -n "${NAMESPACE}" cp "${pod}:/out/k6-summary.json" "${run_dir}/agent-${i}-summary.json" -c k6 >/dev/null 2>&1; then
        collected[$i]=yes
      else
        all_done=0
      fi
    else
      all_done=0
    fi
  done
  [[ "${all_done}" -eq 1 ]] && break
  sleep 5
done
collection_failed=0
for i in $(seq 0 $((agent_count - 1))); do
  if [[ "${collected[$i]:-}" != "yes" ]]; then
    echo "WARNING: result collection incomplete for agent-${i}" >&2
    collection_failed=1
  fi
done

kubectl -n "${NAMESPACE}" logs -f "job/${job_name}" || true

# Wait for the Job to reach a terminal condition.
kubectl -n "${NAMESPACE}" wait --for=condition=complete "job/${job_name}" --timeout=2m 2>/dev/null \
  || kubectl -n "${NAMESPACE}" wait --for=condition=failed "job/${job_name}" --timeout=5s 2>/dev/null || true

# Evidence capture happens BEFORE any failure exit (2026-11-17 follow-up
# review, finding 2): a failed Job is exactly when the artifacts matter,
# and the old code exited before capturing agent logs and the monitor
# scrape. The Job's success/failure verdict is taken after capture.
succeeded="$(kubectl -n "${NAMESPACE}" get "job/${job_name}" -o jsonpath='{.status.succeeded}' 2>/dev/null || echo 0)"
# Independently captured terminal evidence (third review, finding 2):
# the report must not infer execution success from file presence, so the
# runner records the Job's terminal state explicitly in env.json.
if [[ "${succeeded}" == "${agent_count}" ]]; then
  job_terminal_state="complete"
else
  failed_count="$(kubectl -n "${NAMESPACE}" get "job/${job_name}" -o jsonpath='{.status.failed}' 2>/dev/null || echo 0)"
  job_terminal_state="failed (succeeded=${succeeded}, failed=${failed_count})"
fi

# Capture every agent's end-of-run summary + per-pod exit status, then the
# best-effort cn=Monitor scrape (never fails the run — see scrape-monitor.sh).
# Per-agent k6 exit codes go into env.json (agent_exit_codes) so the report
# can gate on execution evidence per agent, not just the Job aggregate.
echo "Capturing per-agent results to ${run_dir}..."
exit_codes="{"
for i in $(seq 0 $((agent_count - 1))); do
  pod="$(kubectl -n "${NAMESPACE}" get pods -l "job-name=${job_name}" \
    -o jsonpath="{range .items[?(@.metadata.annotations['batch\.kubernetes\.io/job-completion-index']=='${i}')]}{.metadata.name}{end}" 2>/dev/null)"
  if [[ -n "${pod}" ]]; then
    kubectl -n "${NAMESPACE}" logs "${pod}" -c k6 > "${run_dir}/agent-${i}.log" 2>/dev/null || true
    # The pod's terminal phase tells terminated from still-running; the
    # container's exitCode from the pod status is k6's own exit code.
    code="$(kubectl -n "${NAMESPACE}" get pod "${pod}" -o jsonpath='{.status.containerStatuses[?(@.name=="k6")].state.terminated.exitCode}' 2>/dev/null)"
    [[ -n "${code}" ]] || code="null"
  else
    code="null"
  fi
  [[ "${i}" -gt 0 ]] && exit_codes="${exit_codes},"
  exit_codes="${exit_codes}\"agent-${i}\":${code}"
done
exit_codes="${exit_codes}}"
python3 - "$run_dir" "$exit_codes" "$job_terminal_state" "$collection_failed" <<'PYEOF'
import json, sys
run_dir, raw, job_state = sys.argv[1], sys.argv[2], sys.argv[3]
path = f"{run_dir}/env.json"
try:
    env = json.load(open(path))
except (OSError, json.JSONDecodeError):
    env = {}
try:
    env["agent_exit_codes"] = json.loads(raw)
except json.JSONDecodeError:
    env["agent_exit_codes"] = {}
env["job_terminal_state"] = job_state
# The in-run sampler belongs to this exact run; avoid global pointer races.
env["artifact_schema_version"] = 2
env["collection_incomplete"] = bool(int(sys.argv[4]))
json.dump(env, open(path, "w"), indent=2)
PYEOF
kubectl -n "${NAMESPACE}" get "job/${job_name}" -o wide > "${run_dir}/job.txt" 2>&1 || true
"${ROOT_DIR}/scripts/scrape-monitor.sh" "${run_dir}" || true

if [[ "${succeeded}" != "${agent_count}" || "${collection_failed}" != "0" ]]; then
  echo "k6 Job failed (a k6 script/check error or pod failure); evidence captured to ${run_dir}." >&2
  kubectl -n "${NAMESPACE}" get "job/${job_name}" -o wide >&2
  exit 1
fi
echo "Results saved: ${run_dir}/"
echo "Generate the dashboard with: python3 scripts/generate-report.py ${run_dir}"
