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

# Preflight: this service shares the `web-runner@` runtime SA with
# ytfactory-web. Verify it has the full role set before deploying so we
# don't repeat the 2026-05-13 silent OAuth-callback 500 on the sibling
# service. See cloud/iam/verify_web_runner.sh + memory file
# feedback_web_runner_iam_silent_post_deploy_500.md.
echo "==> Verifying web-runner IAM bindings (preflight)"
"$(cd "$(dirname "$0")" && pwd)/../iam/verify_web_runner.sh"

TAG="${1:-$(date +%Y%m%d-%H%M%S)}"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v3}"
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
# --region pins build to asia-southeast1 to colocate with AR — see
# docs/cost_guardrails section in CLAUDE.md (2026-05-17 cost audit).
gcloud builds submit . \
  --region="${REGION}" \
  --tag="${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=1200s

echo "==> Deploying ${SERVICE} (CPU-only) to Cloud Run, ${REGION}"

# Build env-var arg, omitting optional Whisper deployment when not set.
# Audit S1.15 — API keys (AZURE_OPENAI_API_KEY,
# AZURE_OPENAI_WHISPER_API_KEY) are now passed via Secret Manager
# (--set-secrets) instead of plaintext --set-env-vars. Anyone with
# roles/run.viewer on the project would have read the plaintext via
# `gcloud run services describe`. Required Secret Manager secrets:
#   azure-openai-key            (canonical Azure OpenAI key)
#   azure-openai-whisper-key    (only when AZURE_OPENAI_WHISPER_*
#                                set is in use; create with the same
#                                key when both endpoints share auth)
# Operator: create with
#   printf '%s' "$KEY" | gcloud secrets create azure-openai-key \
#       --data-file=- --replication-policy=automatic
# and grant the run-tts-runner SA secretmanager.secretAccessor.
SECRETS=("AZURE_OPENAI_API_KEY=azure-openai-key:latest")
ENV_VARS="AZURE_OPENAI_ENDPOINT=${AZURE_OPENAI_ENDPOINT}"
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
  # When the Whisper endpoint uses a distinct key, route it via
  # Secret Manager too (same SA grant as the canonical Azure key).
  SECRETS+=("AZURE_OPENAI_WHISPER_API_KEY=azure-openai-whisper-key:latest")
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
  --service-account="web-runner@${PROJECT}.iam.gserviceaccount.com" \
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
  --update-secrets="$(IFS=,; echo "${SECRETS[*]}")" \
  "${COOKIES_ARGS[@]}"
# Audit S1.22 — pinned to the dedicated tts-runner SA so a
# compromised clone-video-worker container can't borrow the
# default Compute Engine SA's broad project privileges.

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
