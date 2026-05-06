#!/usr/bin/env bash
# Build + push + deploy the weights-staging Cloud Run Job.
# After deploy, kick off with:
#   gcloud run jobs execute ytfactory-weights-staging \
#     --project=ytfactory-prod --region=asia-southeast1 --wait
set -euo pipefail

PROJECT="${GCP_PROJECT:-ytfactory-prod}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
SERVICE="ytfactory-weights-staging"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"
BUCKET="ytfactory-model-weights"

cd "$(dirname "$0")"

# HF_TOKEN is needed only for gated repos (Llama for HiDream); the
# default set is all-public, but pass it through anyway so re-runs
# with gated repos Just Work.
HF_TOKEN_VAL="$(cat ~/.cache/huggingface/token 2>/dev/null || echo)"

echo "==> Building + pushing ${IMAGE}"
gcloud builds submit . --tag="${IMAGE}" --project="${PROJECT}" --timeout=900s

echo "==> Creating/updating Cloud Run Job ${SERVICE}"
# `jobs deploy` creates if missing, updates if present.
gcloud run jobs deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="tts-runner@${PROJECT}.iam.gserviceaccount.com" \
  --execution-environment=gen2 \
  --memory=32Gi --cpu=8 \
  --max-retries=1 \
  --task-timeout=7200 \
  --set-env-vars="STAGE_ROOT=/tmp/hf-stage,BUCKET_NAME=${BUCKET},HF_TOKEN=${HF_TOKEN_VAL},HF_HUB_ENABLE_HF_TRANSFER=1,UPLOAD_WORKERS=16,LOG_LEVEL=INFO"

echo "==> Job ready. Execute with:"
echo "    gcloud run jobs execute ${SERVICE} --project=${PROJECT} --region=${REGION} --wait"
