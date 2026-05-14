#!/usr/bin/env bash
# Build + push + deploy cloud/asr-whisper to Cloud Run GPU L4.
# Usage:
#   ./deploy.sh [tag]
#
# Example:
#   ./deploy.sh
#   ./deploy.sh v1

set -euo pipefail

# Bake in the ADC-token auth bypass — see cloud/_shared/auth_setup.sh
# and the memory file feedback_gcloud_reauth_use_adc_bypass.md.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

SERVICE="ytfactory-asr-whisper"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
# Reuse the existing ytfactory-tts artifact registry repo because the
# operator account doesn't have artifactregistry.repositories.create
# permission. Cloud Run doesn't care about repo name semantics — the
# image lives at ytfactory-tts/ytfactory-asr-whisper:tag and works
# the same as if it lived at ytfactory-asr/ytfactory-asr-whisper.
# Bigbang PR can move to a dedicated ytfactory-asr repo once the
# admin grants create perms.
REPO="ytfactory-tts"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

cd "$(dirname "$0")"

echo "==> Building + pushing ${IMAGE}"
gcloud builds submit . \
  --tag="${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=5400s

echo "==> Deploying ${SERVICE} to Cloud Run (L4 GPU, ${REGION})"
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
  --cpu=8 \
  --cpu-boost \
  --concurrency=1 \
  --max-instances=2 \
  --min-instances=0 \
  --timeout=900 \
  --no-allow-unauthenticated \
  --set-env-vars="WHISPER_MODEL=large-v3,WHISPER_DEVICE=cuda,WHISPER_COMPUTE=float16,LOG_LEVEL=INFO" \
  --execution-environment=gen2

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")
echo ""
echo "==> Deployed: ${URL}"
echo "Set on the laptop:"
echo "  export CLOUDRUN_ASR_URL=${URL}"
