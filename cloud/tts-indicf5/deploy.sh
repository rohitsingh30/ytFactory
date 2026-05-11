#!/usr/bin/env bash
# Build + push + deploy ytfactory-tts-indicf5 to Cloud Run GPU L4.
# Mirrors cloud/tts-higgs/deploy.sh but pinned to this service name +
# indicf5's heavier resource profile (24Gi mem, 8 vCPU — the deployed
# version 7 spec captured 2026-05-06).

set -euo pipefail

TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
SERVICE="ytfactory-tts-indicf5"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/tts-indicf5:${TAG}"

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
  --memory=24Gi \
  --cpu=8 \
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
echo "  export CLOUDRUN_TTS_INDICF5_URL=${URL}"
