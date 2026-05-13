#!/usr/bin/env bash
# Build + push + deploy a single-model IMAGE service to Cloud Run GPU L4.
set -euo pipefail

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

SERVICE="${1:-image-qwen}"  # Audit D3.22 — default to dir name; pass arg only to override
TAG="${2:-$(date +%Y%m%d-%H%M%S)}"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

cd "$(dirname "$0")"

echo "==> Building + pushing ${IMAGE}"
gcloud builds submit . --tag="${IMAGE}" --project="${PROJECT}" --timeout=5400s

echo "==> Deploying ${SERVICE} to Cloud Run (L4 GPU)"
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="image-runner@${PROJECT}.iam.gserviceaccount.com" \
  --gpu=1 --gpu-type=nvidia-l4 --no-gpu-zonal-redundancy \
  --no-cpu-throttling --memory=24Gi --cpu=8 \
  --concurrency=1 --max-instances=2 --min-instances=0 \
  --timeout=3600 --no-allow-unauthenticated \
  --set-env-vars="GCS_BUCKET=ytfactory-tts-io,LOG_LEVEL=INFO" \
  --execution-environment=gen2

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")
echo "==> Deployed: ${URL}"
case "${SERVICE}" in
  ytfactory-image-qwen)     echo "  export CLOUDRUN_IMAGE_QWEN_URL=${URL}" ;;
  ytfactory-image-hidream)  echo "  export CLOUDRUN_IMAGE_HIDREAM_URL=${URL}" ;;
esac
