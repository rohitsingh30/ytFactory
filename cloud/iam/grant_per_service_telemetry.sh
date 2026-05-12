#!/usr/bin/env bash
# ============================================================================
# Audit S1.21 — grant OTel telemetry roles to every per-service runtime SA
# ============================================================================
#
# Companion to ./cloud/iam/grant_telemetry.sh, which grants the same
# roles to a single SA (tts-runner). Post-S1.21 we have six per-service
# SAs; this script grants the OTel SDK's required roles to each.
#
# Roles granted to each SA (project-level):
#   roles/cloudtrace.agent          — write spans to Cloud Trace
#   roles/monitoring.metricWriter   — write custom metrics
#   roles/logging.logWriter         — write structured logs
#
# Usage:
#   ./cloud/iam/grant_per_service_telemetry.sh             # apply all
#   ./cloud/iam/grant_per_service_telemetry.sh --dry-run   # print only
#
# Idempotent — re-running is safe.
#
# Required env:
#   GCP_PROJECT   default: ytfactory-prod-v2
# ============================================================================
set -euo pipefail

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
DRY_RUN="${1:-}"

# Keep this list in sync with cloud/iam/create_per_service_sas.sh::SA_NAMES.
PER_SERVICE_SAS=(
  "tts-runner"
  "image-runner"
  "render-runner"
  "web-runner"
  "weights-runner"
  "cobalt-runner"
  "stats-refresh-runner"
  "web-next-runner"  # already existed pre-S1.21; included here for completeness
)

ROLES=(
  roles/cloudtrace.agent
  roles/monitoring.metricWriter
  roles/logging.logWriter
)

echo "==> Project: ${PROJECT}"
echo "==> SAs:     ${PER_SERVICE_SAS[*]}"
echo "==> Roles:   ${ROLES[*]}"
echo

for sa in "${PER_SERVICE_SAS[@]}"; do
  member="serviceAccount:${sa}@${PROJECT}.iam.gserviceaccount.com"
  for role in "${ROLES[@]}"; do
    cmd=(
      gcloud projects add-iam-policy-binding "${PROJECT}"
        --member="${member}"
        --role="${role}"
        --condition=None
        --quiet
    )
    if [[ "${DRY_RUN}" == "--dry-run" ]]; then
      printf '%s\n' "${cmd[*]}"
    else
      echo "==> ${sa} <- ${role}"
      if ! "${cmd[@]}" >/dev/null 2>&1; then
        echo "    grant failed (already present? SA missing? — see"
        echo "    cloud/iam/create_per_service_sas.sh first)"
      fi
    fi
  done
done

if [[ "${DRY_RUN}" != "--dry-run" ]]; then
  echo
  echo "==> Done. Verify with:"
  echo "    gcloud projects get-iam-policy ${PROJECT} \\"
  echo "      --filter='bindings.members:serviceAccount:render-runner@*' \\"
  echo "      --format='table(bindings.role)'"
fi
