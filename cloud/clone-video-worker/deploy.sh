#!/usr/bin/env bash
# Build + push + deploy the clone-video-worker (CPU-only) to Cloud Run.
# Usage:
#   ./deploy.sh [tag]
#
# Requires the Azure OpenAI env vars in your shell (or sourced from .env)
# so they get baked into the Cloud Run service config:
#   AZURE_OPENAI_ENDPOINT
#   AZURE_OPENAI_API_KEY
#   AZURE_OPENAI_API_VERSION       (optional; default 2025-04-01-preview)
#   AZURE_OPENAI_MODEL             (optional; default gpt-4o-mini)
#   AZURE_OPENAI_WHISPER_DEPLOYMENT  (optional; enables transcripts)

set -euo pipefail

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

TAG="${1:-$(date +%Y%m%d-%H%M%S)}"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
SERVICE="ytfactory-clone-video-worker"
REPO="ytfactory-tts"   # reuse the existing AR repo
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

cd "$(dirname "$0")"

# Source .env from the repo root if present so the Azure secrets land
# in this shell before we hand them to Cloud Run.
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
if [ -f "${ROOT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${ROOT_DIR}/.env"
  set +a
fi

if [ -z "${AZURE_OPENAI_ENDPOINT:-}" ] || [ -z "${AZURE_OPENAI_API_KEY:-}" ]; then
  echo "ERROR: AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY must be set." >&2
  echo "       Source your .env or export them before running this." >&2
  exit 1
fi

AZURE_OPENAI_API_VERSION="${AZURE_OPENAI_API_VERSION:-2025-04-01-preview}"
AZURE_OPENAI_MODEL="${AZURE_OPENAI_MODEL:-gpt-4o-mini}"

echo "==> Building + pushing ${IMAGE}"
gcloud builds submit . \
  --tag="${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=1200s

echo "==> Deploying ${SERVICE} (CPU-only) to Cloud Run, ${REGION}"

# Build env-var arg, omitting optional Whisper deployment when not set.
ENV_VARS="AZURE_OPENAI_ENDPOINT=${AZURE_OPENAI_ENDPOINT}"
ENV_VARS+=",AZURE_OPENAI_API_KEY=${AZURE_OPENAI_API_KEY}"
ENV_VARS+=",AZURE_OPENAI_API_VERSION=${AZURE_OPENAI_API_VERSION}"
ENV_VARS+=",AZURE_OPENAI_MODEL=${AZURE_OPENAI_MODEL}"
if [ -n "${AZURE_OPENAI_WHISPER:-}" ]; then
  ENV_VARS+=",AZURE_OPENAI_WHISPER=${AZURE_OPENAI_WHISPER}"
fi
if [ -n "${AZURE_OPENAI_WHISPER_DEPLOYMENT:-}" ]; then
  ENV_VARS+=",AZURE_OPENAI_WHISPER_DEPLOYMENT=${AZURE_OPENAI_WHISPER_DEPLOYMENT}"
fi
if [ -n "${AZURE_OPENAI_WHISPER_ENDPOINT:-}" ]; then
  ENV_VARS+=",AZURE_OPENAI_WHISPER_ENDPOINT=${AZURE_OPENAI_WHISPER_ENDPOINT}"
fi
if [ -n "${AZURE_OPENAI_WHISPER_API_KEY:-}" ]; then
  ENV_VARS+=",AZURE_OPENAI_WHISPER_API_KEY=${AZURE_OPENAI_WHISPER_API_KEY}"
fi
if [ -n "${AZURE_OPENAI_WHISPER_API_VERSION:-}" ]; then
  ENV_VARS+=",AZURE_OPENAI_WHISPER_API_VERSION=${AZURE_OPENAI_WHISPER_API_VERSION}"
fi

# Optional cookies secret. We auto-mount when a Secret Manager secret
# named ${COOKIES_SECRET} (default: yt-dlp-cookies) exists in the
# project. yt-dlp picks it up via YT_DLP_COOKIES_FILE=/secrets/cookies.txt.
COOKIES_SECRET="${COOKIES_SECRET:-yt-dlp-cookies}"
COOKIES_ARGS=()
if gcloud secrets describe "${COOKIES_SECRET}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo "==> Found Secret Manager secret '${COOKIES_SECRET}' — mounting at /secrets/cookies.txt"
  COOKIES_ARGS+=(
    "--update-secrets=/secrets/cookies.txt=${COOKIES_SECRET}:latest"
  )
  ENV_VARS+=",YT_DLP_COOKIES_FILE=/secrets/cookies.txt"
else
  echo "==> No '${COOKIES_SECRET}' secret found — yt-dlp will run without cookies"
  echo "    (YouTube + TikTok will likely block the data-center IP)"
  echo ""
  echo "    To enable: export your browser cookies as cookies.txt, then:"
  echo "      gcloud secrets create ${COOKIES_SECRET} --project=${PROJECT} --data-file=cookies.txt"
  echo "      ./deploy.sh   # re-run this script — it auto-detects the secret"
fi

gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --memory=4Gi \
  --cpu=2 \
  --cpu-boost \
  --concurrency=4 \
  --max-instances=4 \
  --min-instances=0 \
  --timeout=600 \
  --no-allow-unauthenticated \
  --execution-environment=gen2 \
  --set-env-vars="${ENV_VARS}" \
  "${COOKIES_ARGS[@]}"

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")
echo ""
echo "==> Deployed: ${URL}"
echo "Set on the laptop:"
echo "  export CLOUDRUN_CLONE_VIDEO_URL=${URL}"
echo ""
echo "If callers don't have GCP auth, add to your invoker IAM with:"
echo "  gcloud run services add-iam-policy-binding ${SERVICE} \\"
echo "    --region=${REGION} --project=${PROJECT} \\"
echo "    --member=serviceAccount:<sa>@${PROJECT}.iam.gserviceaccount.com \\"
echo "    --role=roles/run.invoker"
