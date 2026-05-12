#!/usr/bin/env bash
# Build + push + deploy a single-model TTS service to Cloud Run GPU L4.
# Usage:
#   ./deploy.sh <service-name> [tag]
# Example:
#   ./deploy.sh ytfactory-tts-higgs v1
#   ./deploy.sh ytfactory-tts-chatterbox

set -euo pipefail

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

SERVICE="${1:?usage: ./deploy.sh <service-name> [tag]}"
TAG="${2:-$(date +%Y%m%d-%H%M%S)}"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

cd "$(dirname "$0")"

echo "==> Building + pushing ${IMAGE}"
gcloud builds submit . \
  --tag="${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=5400s

echo "==> Deploying ${SERVICE} to Cloud Run (L4 GPU, ${REGION})"
# max-instances=2 per single-model service so 3 services × 2 = 6 GPUs
# stays close to our quota of 5 (one will queue briefly under burst).
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
  --concurrency=1 \
  --max-instances=2 \
  --min-instances=0 \
  --timeout=3600 \
  --no-allow-unauthenticated \
  --set-env-vars="GCS_BUCKET=ytfactory-tts-io,LOG_LEVEL=INFO" \
  --execution-environment=gen2 \
  --add-volume="name=weights,type=cloud-storage,bucket=ytfactory-model-weights-v2" \
  --add-volume-mount="volume=weights,mount-path=/models/hf"

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")
echo ""
echo "==> Deployed: ${URL}"
echo "Set on the laptop:"
case "${SERVICE}" in
  ytfactory-tts-higgs)        echo "  export CLOUDRUN_TTS_HIGGS_URL=${URL}" ;;
  ytfactory-tts-chatterbox)   echo "  export CLOUDRUN_TTS_CHATTERBOX_URL=${URL}" ;;
  ytfactory-tts-cosyvoice)    echo "  export CLOUDRUN_TTS_COSYVOICE_URL=${URL}" ;;
  *)                          echo "  export CLOUDRUN_TTS_URL=${URL}" ;;
esac
