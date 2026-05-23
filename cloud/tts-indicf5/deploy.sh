#!/usr/bin/env bash
# Build + push + deploy ytfactory-tts-indicf5 to Cloud Run GPU L4.
# IndicF5's heavier resource profile (24Gi mem, 8 vCPU — the deployed
# version 7 spec captured 2026-05-06).

set -euo pipefail

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
SERVICE="ytfactory-tts-indicf5"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v3}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/tts-indicf5:${TAG}"

cd "$(dirname "$0")"

echo "==> Building + pushing ${IMAGE}"
# --region pins build to asia-southeast1 to colocate with AR — see
# docs/cost_guardrails.md (2026-05-17 cost audit).
gcloud builds submit . \
  --region="${REGION}" \
  --tag="${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=5400s

echo "==> Deploying ${SERVICE} to Cloud Run (L4 GPU, ${REGION})"
# max-instances=1 (was 2 pre-2026-05-17). See docs/cost_guardrails.md.
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="tts-runner@${PROJECT}.iam.gserviceaccount.com" \
  --gpu=1 \
  --gpu-type=nvidia-l4 \
  --no-gpu-zonal-redundancy \
  --no-cpu-throttling \
  --memory=16Gi \
  --cpu=4 \
  --concurrency=2 \
  --max-instances=2 \
  --min-instances=0 \
  --timeout=3600 \
  --no-allow-unauthenticated \
  --set-env-vars="GCS_BUCKET=ytfactory-tts-io,LOG_LEVEL=INFO,HF_HOME=/tmp/hf,TRANSFORMERS_CACHE=/tmp/hf" \
  --execution-environment=gen2 
URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")
echo ""
echo "==> Deployed: ${URL}"
echo "Set on the laptop:"
echo "  export CLOUDRUN_TTS_INDICF5_URL=${URL}"
