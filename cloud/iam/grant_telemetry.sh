#!/usr/bin/env bash
# ============================================================================
# P8 — IAM grants so every Cloud Run service can write OTel signals to GCP.
# ============================================================================
#
# Roles needed by the OTel SDK in cloud/_shared/otel_init.py:
#
#   roles/cloudtrace.agent          — write spans to Cloud Trace
#   roles/monitoring.metricWriter   — write custom metrics
#   roles/logging.logWriter         — write structured logs (Cloud Run
#                                     grants this by default but we set
#                                     it explicitly for clarity)
#
# All ytFactory cloud services run as ``tts-runner@<project>`` so a
# single project-level grant covers every service in one go.
#
# Required env (defaults match prod):
#   GCP_PROJECT       ytfactory-prod-v2
#   RUNTIME_SA        tts-runner@${GCP_PROJECT}.iam.gserviceaccount.com
#
# Usage:
#   ./cloud/iam/grant_telemetry.sh
#   ./cloud/iam/grant_telemetry.sh --dry-run
#
# Verify after:
#   gcloud projects get-iam-policy ytfactory-prod-v2 \
#     --format json --filter "bindings.members:tts-runner@*"
# ============================================================================
set -euo pipefail

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
RUNTIME_SA="${RUNTIME_SA:-tts-runner@${PROJECT}.iam.gserviceaccount.com}"
DRY_RUN="${1:-}"

ROLES=(
  roles/cloudtrace.agent
  roles/monitoring.metricWriter
  roles/logging.logWriter
)

echo "==> Project:    ${PROJECT}"
echo "==> Runtime SA: ${RUNTIME_SA}"
echo "==> Roles:      ${ROLES[*]}"
echo

for role in "${ROLES[@]}"; do
  cmd=(
    gcloud projects add-iam-policy-binding "${PROJECT}"
      --member="serviceAccount:${RUNTIME_SA}"
      --role="${role}"
      --condition=None
      --quiet
  )
  if [[ "${DRY_RUN}" == "--dry-run" ]]; then
    printf '%s\n' "${cmd[*]}"
  else
    echo "==> ${role}"
    if ! "${cmd[@]}" >/dev/null; then
      echo "    grant failed for ${role} (already present?)"
    fi
  fi
done

if [[ "${DRY_RUN}" != "--dry-run" ]]; then
  echo
  echo "==> Done. Verify with:"
  echo "    gcloud projects get-iam-policy ${PROJECT} \\"
  echo "      --filter='bindings.members:serviceAccount:${RUNTIME_SA}' \\"
  echo "      --format='table(bindings.role)'"
fi
