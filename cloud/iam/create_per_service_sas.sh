#!/usr/bin/env bash
# ============================================================================
# Audit S1.21 — per-service Cloud Run runtime service accounts
# ============================================================================
#
# Pre-fix every Cloud Run service in the cluster (TTS, image, render,
# editing, web, weights-staging) ran as the single ``tts-runner@`` SA.
# Least-privilege violation: a compromised image-flux2-klein container
# walks away with full write access to the TTS bucket, the editing
# agent's Firestore data, the web-server's secret-mount permissions,
# etc.
#
# Post-fix each service category gets its own SA:
#
#   tts-runner        TTS containers (chatterbox / cosyvoice / f5 /
#                     higgs / indicf5 / indicparler) — name kept for
#                     IAM/secret/back-compat ergonomics; existing role
#                     bindings continue to apply only to TTS pods now.
#
#   image-runner      Image containers (flux2-klein / hidream / qwen /
#                     z-image-turbo).
#
#   render-runner     Render-worker-v2 + editing-agent (both touch the
#                     final mp4 + need write access to render output
#                     bucket / Firestore job docs).
#
#   web-runner        web-server (FastAPI control plane) +
#                     clone-video-worker (kicked off by web).
#                     web-next/ already has its own ``web-next-runner``.
#
#   weights-runner    weights-staging (one-shot Hugging Face → GCS
#                     copier; needs HF_HOME bucket write, nothing else).
#
#   cobalt-runner     cobalt-api (yt-dlp proxy; needs network egress
#                     only, no GCS/Firestore).
#
# Usage:
#   ./cloud/iam/create_per_service_sas.sh                # create all
#   ./cloud/iam/create_per_service_sas.sh --dry-run      # print only
#
# Idempotent — already-present SAs print "already exists" and continue.
# After running this, run ./cloud/iam/grant_per_service_telemetry.sh
# (which grants OTel roles to every per-service SA) and then redeploy
# each service via its cloud/<svc>/deploy.sh — the deploy scripts
# already pin the right SA per S1.21.
#
# Required env:
#   GCP_PROJECT   default: ytfactory-prod-v2
# ============================================================================
set -euo pipefail

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
DRY_RUN="${1:-}"

# (sa_name  description) pairs — one per category.
SA_NAMES=(
  "tts-runner|ytFactory TTS Cloud Run containers (audit S1.21)"
  "image-runner|ytFactory image-gen Cloud Run containers (audit S1.21)"
  "render-runner|ytFactory render-worker-v2 + editing-agent (audit S1.21)"
  "web-runner|ytFactory web-server + clone-video-worker (audit S1.21)"
  "weights-runner|ytFactory weights-staging one-shot init (audit S1.21)"
  "cobalt-runner|ytFactory cobalt-api yt-dlp proxy (audit S1.21)"
  "stats-refresh-runner|ytFactory stats-refresh Cloud Run JOB (audit S1.21)"
)

echo "==> Project:    ${PROJECT}"
echo "==> Per-service SAs:"
for entry in "${SA_NAMES[@]}"; do
  IFS='|' read -r sa desc <<< "${entry}"
  echo "    - ${sa}@${PROJECT}.iam.gserviceaccount.com"
done
echo

for entry in "${SA_NAMES[@]}"; do
  IFS='|' read -r sa desc <<< "${entry}"
  cmd=(
    gcloud iam service-accounts create "${sa}"
      --project="${PROJECT}"
      --display-name="${desc}"
      --quiet
  )
  if [[ "${DRY_RUN}" == "--dry-run" ]]; then
    printf '%s\n' "${cmd[*]}"
  else
    echo "==> create ${sa}"
    if ! "${cmd[@]}" 2>&1; then
      echo "    create failed (likely already exists; safe to ignore)"
    fi
  fi
done

if [[ "${DRY_RUN}" != "--dry-run" ]]; then
  echo
  echo "==> Done. Next steps:"
  echo "    1. ./cloud/iam/grant_per_service_telemetry.sh"
  echo "       (gives every per-service SA the OTel write roles)"
  echo "    2. Grant per-service-specific roles (TTS bucket on tts-runner,"
  echo "       image bucket on image-runner, render output bucket on"
  echo "       render-runner, secret-mount role on web-runner, etc)."
  echo "       The exact list lives in docs/iam_per_service.md."
  echo "    3. Redeploy each service with bash cloud/<svc>/deploy.sh."
fi
