#!/usr/bin/env bash
# ============================================================================
# P8 — Parallel re-deploy of every Cloud Run service that ships
# cloud/_shared/otel_init.py.
#
# What this does:
#   * Re-syncs cloud/_shared/otel_init.py into every per-service dir
#     (in case _shared changed since the last deploy).
#   * Kicks off ``cloud/<service>/deploy.sh ...`` in parallel.
#   * Tees each service's log to deploy_logs/<service>.log so you can
#     tail any single one while everything runs.
#   * Waits for all and prints a final pass/fail table.
#
# Usage:
#   ./cloud/_shared/redeploy_for_otel.sh                # all 13 services
#   ./cloud/_shared/redeploy_for_otel.sh tts-chatterbox # canary one
#   ./cloud/_shared/redeploy_for_otel.sh --dry-run      # show commands
# ============================================================================
set -uo pipefail   # NOT -e: we want to keep going on individual failures

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${ROOT}/deploy_logs"
mkdir -p "${LOG_DIR}"

DRY_RUN=0
SELECTED=()
for arg in "$@"; do
    if [[ "$arg" == "--dry-run" ]]; then
        DRY_RUN=1
    else
        SELECTED+=("$arg")
    fi
done

# (service-dir, deploy-cmd) pairs. The deploy.sh signatures vary per
# service — encode them here so the orchestrator stays stupid-simple.
declare -a SERVICES=(
    "tts-chatterbox|./deploy.sh ytfactory-tts-chatterbox"
    "tts-cosyvoice|./deploy.sh ytfactory-tts-cosyvoice"
    "tts-f5|./deploy.sh"
    "tts-higgs|./deploy.sh ytfactory-tts-higgs"
    "tts-indicf5|./deploy.sh"
    "tts-indicparler|./deploy.sh ytfactory-tts-indicparler"
    "image-flux2-klein|./deploy.sh"
    "image-z-image-turbo|./deploy.sh"
    "image-qwen|./deploy.sh ytfactory-image-qwen"
    "image-hidream|./deploy.sh ytfactory-image-hidream"
    "editing-agent|./deploy.sh"
    "clone-video-worker|./deploy.sh"
    "render-worker-v2|./deploy.sh"
    # web-server is the dashboard backend itself. Including it in the
    # OTel redeploy ensures the dashboard's Cloud Logging reader path
    # (pipeline.observability.cloud_log_reader) is shipped alongside
    # the per-service stdout JSON exporter — without this, every other
    # service emits structured ytfactory.event records, but the
    # dashboard backend can't read them.
    "web-server|./deploy.sh"
)

# Re-sync the canonical otel_init.py into every per-service dir.
echo "==> Re-syncing cloud/_shared/otel_init.py into every service..."
bash "${ROOT}/_shared/sync.sh"
echo

# Filter to selected services (if any).
JOBS=()
for entry in "${SERVICES[@]}"; do
    svc="${entry%%|*}"
    cmd="${entry#*|}"
    if [[ ${#SELECTED[@]} -gt 0 ]]; then
        skip=1
        for s in "${SELECTED[@]}"; do
            [[ "$svc" == "$s" ]] && skip=0
        done
        [[ $skip -eq 1 ]] && continue
    fi
    JOBS+=("${entry}")
done

echo "==> Will deploy ${#JOBS[@]} service(s):"
for entry in "${JOBS[@]}"; do
    echo "      ${entry%%|*}"
done
echo

if [[ ${DRY_RUN} -eq 1 ]]; then
    for entry in "${JOBS[@]}"; do
        svc="${entry%%|*}"
        cmd="${entry#*|}"
        echo "(cd ${ROOT}/${svc} && ${cmd})"
    done
    exit 0
fi

# Fire off in parallel. Each child writes its own log + a sentinel
# file containing its exit code so the parent can tally.
PIDS=()
for entry in "${JOBS[@]}"; do
    svc="${entry%%|*}"
    cmd="${entry#*|}"
    log="${LOG_DIR}/${svc}.log"
    rc_file="${LOG_DIR}/${svc}.rc"
    rm -f "${rc_file}"
    echo "==> [${svc}] starting (log: ${log})"
    (
        cd "${ROOT}/${svc}"
        eval "${cmd}" > "${log}" 2>&1
        echo $? > "${rc_file}"
    ) &
    PIDS+=($!)
done

echo
echo "==> Waiting for ${#PIDS[@]} parallel deploys to finish..."
echo "    (tail any individual log: tail -f ${LOG_DIR}/<service>.log)"
echo

for pid in "${PIDS[@]}"; do
    wait "$pid" || true
done

echo
echo "==> Results:"
fail_count=0
ok_count=0
for entry in "${JOBS[@]}"; do
    svc="${entry%%|*}"
    rc_file="${LOG_DIR}/${svc}.rc"
    log="${LOG_DIR}/${svc}.log"
    if [[ -f "${rc_file}" ]]; then
        rc=$(cat "${rc_file}")
        if [[ "${rc}" == "0" ]]; then
            printf '   %-32s OK\n' "${svc}"
            ok_count=$((ok_count + 1))
        else
            printf '   %-32s FAILED rc=%s   tail: %s\n' "${svc}" "${rc}" "${log}"
            fail_count=$((fail_count + 1))
        fi
    else
        printf '   %-32s NO RC FILE     tail: %s\n' "${svc}" "${log}"
        fail_count=$((fail_count + 1))
    fi
done

echo
echo "==> ${ok_count} succeeded, ${fail_count} failed"

if [[ ${fail_count} -gt 0 ]]; then
    exit 1
fi
