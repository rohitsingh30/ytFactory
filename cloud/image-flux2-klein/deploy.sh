#!/usr/bin/env bash
# Build + push + deploy the FLUX.2 [klein] 4B image service to
# Cloud Run GPU L4 in asia-southeast1.
#
# Usage:
#   ./deploy.sh                # uses default service name + auto tag
#   ./deploy.sh <service-name> # custom service name
#   ./deploy.sh <service-name> <tag>

set -euo pipefail

SERVICE="${1:-ytfactory-image-flux2-klein}"
TAG="${2:-$(date +%Y%m%d-%H%M%S)}"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"
BUCKET="ytfactory-model-weights"

cd "$(dirname "$0")"

echo "==> Building + pushing ${IMAGE}"
gcloud builds submit . \
  --tag="${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=5400s

echo "==> Deploying ${SERVICE} to Cloud Run (L4 GPU, ${REGION})"
# Same shape as cloud/tts-chatterbox/deploy.sh — proven on the L4
# fleet. Concurrency=1 (one /generate per container at a time);
# max-instances=2 caps total GPU spend across the service.
# --add-volume mounts the persistent weights bucket at HF_HOME,
# so the server can `from_pretrained("/models/hf/flat/<repo>",
# local_files_only=True)`.
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
  --cpu-boost \
  --concurrency=1 \
  --max-instances=2 \
  --min-instances=0 \
  --timeout=3600 \
  --no-allow-unauthenticated \
  --set-env-vars="GCS_BUCKET=ytfactory-tts-io,LOG_LEVEL=INFO" \
  --execution-environment=gen2 \
  --add-volume="name=weights,type=cloud-storage,bucket=${BUCKET}" \
  --add-volume-mount="volume=weights,mount-path=/models/hf"

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")
echo ""
echo "==> Deployed: ${URL}"
echo "Set on the laptop:"
case "${SERVICE}" in
  ytfactory-image-flux2-klein)    echo "  export CLOUDRUN_IMAGE_FLUX2_KLEIN_URL=${URL}" ;;
  ytfactory-image-z-image-turbo)  echo "  export CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL=${URL}" ;;
  ytfactory-image-qwen)           echo "  export CLOUDRUN_IMAGE_QWEN_URL=${URL}" ;;
  ytfactory-image-hidream)        echo "  export CLOUDRUN_IMAGE_HIDREAM_URL=${URL}" ;;
esac
