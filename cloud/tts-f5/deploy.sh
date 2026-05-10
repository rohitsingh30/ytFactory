#!/usr/bin/env bash
# Build + push + deploy ytfactory-tts to Cloud Run GPU (asia-southeast1).
#
# Usage:
#   ./deploy.sh           # build, push, deploy with default tag (timestamp)
#   ./deploy.sh v3        # explicit tag
#
# Pre-reqs (one-time, already done for ytfactory-prod):
#   - Artifact Registry repo: ytfactory-tts in asia-southeast1
#   - GCS bucket: gs://ytfactory-tts-io
#   - Service account: tts-runner@ytfactory-prod.iam.gserviceaccount.com
#   - Cloud Run L4 GPU quota approved for asia-southeast1

set -euo pipefail

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
SERVICE="ytfactory-tts"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"

IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/server:${TAG}"

cd "$(dirname "$0")"

echo "==> Building + pushing ${IMAGE}"
# Region/machine-type omitted — use Cloud Build default (global,
# e2-medium). e2-highcpu-32 in asia-southeast1 needs a separate Cloud
# Build worker quota we don't have. Default builds are slower (~10 min
# for our v2 6 GB image, ~30-50 min expected for v3's 14 GB) but free
# for the first 120 min/day.
# 5400s = 90 min ceiling — v3 pulls 4 sets of HF weights (~14 GB).
gcloud builds submit . \
  --tag="${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=5400s

echo "==> Deploying to Cloud Run (L4 GPU, asia-southeast1)"
# Why each flag:
#   --gpu=1 --gpu-type=nvidia-l4    matches our quota (NoZonalRedundancy)
#   --no-gpu-zonal-redundancy       cheaper variant; matches the quota name
#   --no-cpu-throttling             allow GPU work between requests
#   --memory=16Gi --cpu=4           Cloud Run GPU minimum
#   --concurrency=1                 1 GPU job per instance (mirrors
#                                   gpu_one_render_at_a_time rule)
#   --max-instances=5               matches our 5-GPU quota
#   --min-instances=0               scale to zero when idle (key cost lever)
#   --timeout=3600                  long-form chunks can be slow first call
#   --no-allow-unauthenticated      IAM-protected; caller needs ID token
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
  --max-instances=5 \
  --min-instances=0 \
  --timeout=3600 \
  --no-allow-unauthenticated \
  --set-env-vars="GCS_BUCKET=ytfactory-tts-io,LOG_LEVEL=INFO" \
  --execution-environment=gen2 \
  --add-volume="name=weights,type=cloud-storage,bucket=ytfactory-model-weights" \
  --add-volume-mount="volume=weights,mount-path=/models/hf"

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")
echo ""
echo "==> Deployed."
echo "    Service: ${SERVICE}"
echo "    URL:     ${URL}"
echo "    Tag:     ${TAG}"
echo ""
echo "Smoke test:"
echo "  TOKEN=\$(gcloud auth print-identity-token)"
echo "  curl -H \"Authorization: Bearer \$TOKEN\" ${URL}/healthz"
echo ""
echo "Set this on the laptop side:"
echo "  export CLOUDRUN_TTS_URL=${URL}"
