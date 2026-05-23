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

PROJECT="${GCP_PROJECT:-ytfactory-prod-v3}"
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
# --region pins the build to a regional Cloud Build worker pool in the
# same region as Artifact Registry (asia-southeast1). Without this the
# build runs in the global pool (US Iowa) and every image push crosses
# the Pacific to AR, racking up inter-region intercontinental egress
# (₹400+/day on z-image-turbo's 33 GiB image — caught 2026-05-17 cost
# audit). Source upload + image push now stay regional.
gcloud builds submit . \
  --region="${REGION}" \
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
  --cpu=4 \
  --cpu-boost \
  --concurrency=2 \
  --max-instances=1 \
  --min-instances=0 \
  --timeout=900 \
  --no-allow-unauthenticated \
  --set-env-vars="WHISPER_MODEL=large-v3,WHISPER_DEVICE=cuda,WHISPER_COMPUTE=float16,LOG_LEVEL=INFO,HF_HOME=/tmp/hf,TRANSFORMERS_CACHE=/tmp/hf" \
  --execution-environment=gen2

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")
echo ""
echo "==> Deployed: ${URL}"
echo "Set on the laptop:"
echo "  export CLOUDRUN_ASR_URL=${URL}"
