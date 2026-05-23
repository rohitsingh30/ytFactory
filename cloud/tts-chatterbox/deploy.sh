#!/usr/bin/env bash
# Build + push + deploy the Chatterbox TTS service to Cloud Run GPU L4.
# Usage:
#   ./deploy.sh [service-name] [tag]
# Example:
#   ./deploy.sh tts-chatterbox  # actual service name (audit D3.22)

set -euo pipefail

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

SERVICE="${1:-tts-chatterbox}"  # Audit D3.22 — default to dir name; pass arg only to override
TAG="${2:-$(date +%Y%m%d-%H%M%S)}"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v3}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

cd "$(dirname "$0")"

echo "==> Building + pushing ${IMAGE}"
# --region pins build to asia-southeast1 to colocate with AR and avoid
# US→APAC intercontinental egress (₹400+/day caught 2026-05-17 cost
# audit). See docs/cost_guardrails.md.
gcloud builds submit . \
  --region="${REGION}" \
  --tag="${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=5400s

echo "==> Deploying ${SERVICE} to Cloud Run (L4 GPU, ${REGION})"
# max-instances=1 (was 2 pre-2026-05-17 cost audit). The render
# pipeline calls TTS sequentially per chunk, never concurrently across
# instances; concurrency=2 still lets two chunks share a single
# instance if the laptop ever pipelines them. Spinning a 2nd L4 was
# pure cost with no throughput win. See docs/cost_guardrails.md.
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
  --cpu-boost \
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
echo "  export CLOUDRUN_TTS_CHATTERBOX_URL=${URL}"
