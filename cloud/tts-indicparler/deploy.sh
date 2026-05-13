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

SERVICE="${1:-tts-indicparler}"  # Audit D3.22 — default to dir name; pass arg only to override
TAG="${2:-$(date +%Y%m%d-%H%M%S)}"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

cd "$(dirname "$0")"

echo "==> Building + pushing ${IMAGE}"
# Source .env if present (for HF_TOKEN — used for gated weight pulls).
if [ -f "$(dirname "$0")/../../.env" ]; then
  set -a
  . "$(dirname "$0")/../../.env"
  set +a
fi
# Audit S1.11 — HF_TOKEN now flows via Cloud Build's
# availableSecrets → BuildKit secret mount, NOT via --build-arg
# (which would bake it into the image layer history). The cloudbuild.yaml
# reads `hf-token` from Secret Manager. Operators must:
#
#   1. Store the token: gcloud secrets create hf-token --replication-policy=automatic
#      printf '%s' "$HF_TOKEN" | gcloud secrets versions add hf-token --data-file=-
#   2. Grant the Cloud Build SA secretmanager.secretAccessor on it.
#
# When the secret is missing, the build still succeeds (weights
# download on first /readyz call instead).
echo "  (HF_TOKEN delivered via Secret Manager → BuildKit secret)"
gcloud builds submit . \
  --config=cloudbuild.yaml \
  --project="${PROJECT}" \
  --timeout=5400s \
  --substitutions=_IMAGE="${IMAGE}"

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
  --add-volume-mount="volume=weights,mount-path=/models/hf,readonly=true"

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
