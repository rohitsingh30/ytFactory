#!/usr/bin/env bash
# Build + push + deploy the weights-staging Cloud Run Job.
# After deploy, kick off with:
#   gcloud run jobs execute ytfactory-weights-staging \
#     --project=ytfactory-prod-v2 --region=asia-southeast1 --wait
set -euo pipefail

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
SERVICE="ytfactory-weights-staging"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"
BUCKET="ytfactory-model-weights-v2"

cd "$(dirname "$0")"

# HF_TOKEN is needed only for gated repos (Llama for HiDream); the
# default set is all-public, but pass it through anyway so re-runs
# with gated repos Just Work.
HF_TOKEN_VAL="$(cat ~/.cache/huggingface/token 2>/dev/null || echo)"

echo "==> Building + pushing ${IMAGE}"
gcloud builds submit . --tag="${IMAGE}" --project="${PROJECT}" --timeout=900s

echo "==> Creating/updating Cloud Run Job ${SERVICE}"
# `jobs deploy` creates if missing, updates if present.
#
# HF_HUB_DISABLE_XET=1 — critical for asia-southeast1.
# Without it, `huggingface_hub>=1.0` routes any repo with Xet-backed
# blobs (newer image / large model repos like FLUX.2-klein-4B and
# Z-Image-Turbo) through `cas-bridge.xethub.hf.co/xet-bridge-us/`,
# which is direct US-east-1 S3 with no Asia POP. Cross-Pacific from
# Cloud Run asia-southeast1 = ~40-50 Mbps → 13 GB Z-Image-Turbo
# silently downloads for 50+ min before timing out. With the flag,
# HF falls back to the regular CDN/CloudFront LFS path which serves
# from an Asia POP at ~300-400 Mbps → 13 GB in ~5-7 min. Discovered
# 2026-05-07 during Phase-1 image-weights staging. Same fix applies
# at the laptop (~5× speedup, 45→250 Mbps from BLR).
gcloud run jobs deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="tts-runner@${PROJECT}.iam.gserviceaccount.com" \
  --execution-environment=gen2 \
  --memory=32Gi --cpu=8 \
  --max-retries=1 \
  --task-timeout=7200 \
  --set-env-vars="STAGE_ROOT=/tmp/hf-stage,BUCKET_NAME=${BUCKET},HF_TOKEN=${HF_TOKEN_VAL},HF_HUB_ENABLE_HF_TRANSFER=1,HF_HUB_DISABLE_XET=1,UPLOAD_WORKERS=16,LOG_LEVEL=INFO"

echo "==> Job ready. Execute with:"
echo "    gcloud run jobs execute ${SERVICE} --project=${PROJECT} --region=${REGION} --wait"
