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
# **Audit S1.21 — pre-fix every ytFactory Cloud Run service ran as
# the single ``tts-runner@`` SA. Post-fix each service category gets
# its own SA. This script still grants the OTel roles to ``tts-runner``
# (the TTS containers' SA, kept for back-compat), but is no longer the
# whole story. Every per-service SA needs the same grant — use the
# companion script instead:**
#
#   bash cloud/iam/grant_per_service_telemetry.sh
#
# That script grants OTel roles to every per-service SA (tts-runner,
# image-runner, render-runner, web-runner, weights-runner,
# cobalt-runner, stats-refresh-runner, web-next-runner). See
# docs/iam_per_service.md for the full mapping.
#
# This script remains for the narrow case where only the TTS-flavour
# OTel grant needs (re-)applying.
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
