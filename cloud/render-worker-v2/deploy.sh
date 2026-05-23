#!/usr/bin/env bash
# Build + deploy ytfactory-render-worker-v2 — Cloud Run JOB.
#
# This is the LAYER 2 (cloud-native) render path. It replaces the
# laptop agent for production renders.
#
# Triggered per-render by the control plane (control/cloud_run.py)
# via the google-cloud-run SDK or `gcloud run jobs execute --async`.
#
# Build context = repo root.

set -euo pipefail

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v3}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
JOB="ytfactory-render-worker-v2"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${JOB}:${TAG}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

echo "==> Building + pushing ${IMAGE}"
echo "    (build context = ${REPO_ROOT})"
# Inline async-submit + poll (can't use submit_build.sh — it hardcodes
# --tag which conflicts with --config). Same VPC-SC log-streaming
# avoidance as submit_build.sh.
#
# --region pins build to asia-southeast1 to colocate with AR — see
# docs/cost_guardrails.md (2026-05-17 cost audit).
#
# 2026-05-18: do NOT swallow stderr from `gcloud builds submit` with
# 2>/dev/null. The legacy form hid "project does not exist" errors,
# leaving the script to fail with the cryptic "got ''" branch below
# instead of surfacing the actual gcloud reason. Route stderr to a
# capture file and dump it on failure.
_GCLOUD_BUILD_ERR=$(mktemp)
BUILD_ID=$(gcloud builds submit . \
  --region="${REGION}" \
  --config=cloud/render-worker-v2/cloudbuild.yaml \
  --substitutions="_IMAGE=${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=3600s \
  --async \
  --format="value(id)" 2>"${_GCLOUD_BUILD_ERR}")
if [ -z "${BUILD_ID}" ] || ! echo "${BUILD_ID}" | grep -qE '^[a-f0-9-]{20,}$'; then
  echo "ERROR: failed to submit build (got '${BUILD_ID}')" >&2
  echo "---- gcloud stderr ----" >&2
  cat "${_GCLOUD_BUILD_ERR}" >&2
  echo "---- end gcloud stderr ----" >&2
  rm -f "${_GCLOUD_BUILD_ERR}"
  exit 1
fi
rm -f "${_GCLOUD_BUILD_ERR}"
echo "==> Build ID: ${BUILD_ID}"
echo "==> Poll URL: https://console.cloud.google.com/cloud-build/builds/${BUILD_ID}?project=${PROJECT}&region=${REGION}"
DEADLINE=$((SECONDS + 3900))
while [ $SECONDS -lt $DEADLINE ]; do
  STATUS=$(gcloud builds describe "${BUILD_ID}" --region="${REGION}" --project="${PROJECT}" --format="value(status)" 2>/dev/null || echo "?")
  case "${STATUS}" in
    SUCCESS) echo "==> Build SUCCESS"; break ;;
    FAILURE|CANCELLED|TIMEOUT|EXPIRED|INTERNAL_ERROR) echo "==> Build ${STATUS}" >&2; exit 1 ;;
    *) echo "    ...status=${STATUS} (${SECONDS}s elapsed)"; sleep 30 ;;
  esac
done

# Azure OpenAI wiring — these MUST be on the JOB or preflight aborts every
# render at stage=bootstrap with "AZURE_OPENAI_ENDPOINT missing". They were
# previously left as a manual post-deploy step which got skipped on ~every
# redeploy (--set-env-vars below replaces ALL env vars). Baked in now so
# `bash deploy.sh` is sufficient.
#
# Override per-deploy by exporting AZURE_OPENAI_ENDPOINT / _API_VERSION /
# _MODEL / _TOKEN_PARAM before invoking the script. Defaults match the chat
# assistant's Azure deployment (ytfactory-web service env, 2026-05-12).
#
# AZURE_OPENAI_TOKEN_PARAM: gpt-5.x / o1 / o3 reasoning deployments require
# `max_completion_tokens` and reject `max_tokens` outright. Setting this
# env upfront skips the runtime fail-then-retry handshake on EVERY LLM call
# (a long-form render does dozens). Default = max_completion_tokens
# because the live model (gpt-5.3-chat) is a reasoning deployment. Set to
# `max_tokens` if you flip the model env back to gpt-4o or earlier.
AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-https://testshoffer.openai.azure.com}"
AZURE_OPENAI_API_VERSION="${AZURE_OPENAI_API_VERSION:-2025-04-01-preview}"
AZURE_OPENAI_MODEL="${AZURE_OPENAI_MODEL:-gpt-5.3-chat}"
AZURE_OPENAI_TOKEN_PARAM="${AZURE_OPENAI_TOKEN_PARAM:-max_completion_tokens}"

# Resolve live Cloud Run URLs at deploy time. Hardcoding host hashes is
# brittle — they change when a service is recreated. `gcloud run services
# describe` returns the current canonical URL.
_resolve_url() {
  local svc="$1"
  gcloud run services describe "${svc}" \
    --project="${PROJECT}" --region="${REGION}" \
    --format="value(status.url)" 2>/dev/null
}
TTS_CHATTERBOX_URL="$(_resolve_url tts-chatterbox)"
TTS_INDICF5_URL="$(_resolve_url ytfactory-tts-indicf5)"
IMAGE_Z_IMAGE_TURBO_URL="$(_resolve_url ytfactory-image-z-image-turbo)"
ASR_URL="$(_resolve_url ytfactory-asr-whisper)"
for pair in "tts-chatterbox:${TTS_CHATTERBOX_URL}" \
            "ytfactory-tts-indicf5:${TTS_INDICF5_URL}" \
            "ytfactory-image-z-image-turbo:${IMAGE_Z_IMAGE_TURBO_URL}" \
            "ytfactory-asr-whisper:${ASR_URL}"; do
  name="${pair%%:*}"; url="${pair#*:}"
  if [ -z "${url}" ]; then
    echo "ERROR: could not resolve Cloud Run URL for ${name}" >&2
    exit 1
  fi
done

echo "==> Creating/updating Cloud Run JOB ${JOB}"
gcloud run jobs deploy "${JOB}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="render-runner@${PROJECT}.iam.gserviceaccount.com" \
  --execution-environment=gen2 \
  --memory=8Gi --cpu=4 \
  --max-retries=0 \
  --task-timeout=3600 \
  --update-secrets="AZURE_OPENAI_API_KEY=azure-openai-key:latest" \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=${PROJECT}|YTFACTORY_BUCKET=ytfactory-prod-v3-artifacts|CLOUDRUN_TTS_CHATTERBOX_URL=${TTS_CHATTERBOX_URL}|CLOUDRUN_TTS_INDICF5_URL=${TTS_INDICF5_URL}|CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL=${IMAGE_Z_IMAGE_TURBO_URL}|CLOUDRUN_ASR_URL=${ASR_URL}|CLOUDRUN_TTS_DISABLE_FALLBACK=1|CLOUDRUN_IMAGE_DISABLE_FALLBACK=1|YTFACTORY_RENDER_MODE=real|YTFACTORY_LLM_BACKEND=azure_openai|AZURE_OPENAI_ENDPOINT=${AZURE_OPENAI_ENDPOINT}|AZURE_OPENAI_API_VERSION=${AZURE_OPENAI_API_VERSION}|AZURE_OPENAI_MODEL=${AZURE_OPENAI_MODEL}|AZURE_OPENAI_TOKEN_PARAM=${AZURE_OPENAI_TOKEN_PARAM}|YTFACTORY_ASR_PROVIDER=faster_whisper|YTFACTORY_PROMPT_REFINER=1|LOG_LEVEL=INFO"

# Audit T1.11 — was --set-secrets="AZURE_OPENAI_API_KEY=...".
# --set-secrets is REPLACE-not-merge, so any subsequent
# --update-secrets=ANTHROPIC_API_KEY=... toggle (recommended in this
# script's own help text below) regressed AZURE_OPENAI_API_KEY off
# the deployment on the next redeploy. Switched to --update-secrets
# (additive) so the YTFACTORY_LLM_BACKEND env-toggle workflow is
# safe to repeat.

echo ""
echo "==> Job deployed (mode=real, llm=azure_openai, endpoint=${AZURE_OPENAI_ENDPOINT}, model=${AZURE_OPENAI_MODEL}, token_param=${AZURE_OPENAI_TOKEN_PARAM})."
echo ""
echo "    Smoke-test preflight without consuming work:"
echo "      gcloud run jobs execute ${JOB} \\"
echo "        --project=${PROJECT} --region=${REGION} \\"
echo "        --update-env-vars=YTFACTORY_PREFLIGHT_ONLY=1 --wait"
echo ""
echo "    Trigger one render execution manually:"
echo "      gcloud run jobs execute ${JOB} \\"
echo "        --project=${PROJECT} --region=${REGION} \\"
echo "        --update-env-vars='YTFACTORY_JOB_ID=<some-job-id>'"
echo ""
echo "    Or set on the control plane and let it trigger automatically:"
echo "      export YTFACTORY_RENDER_BACKEND=cloudrun"
echo "      export YTFACTORY_CLOUDRUN_JOB=${JOB}"
echo "      export YTFACTORY_CLOUDRUN_REGION=${REGION}"
echo ""
echo "    Switch to Anthropic SDK (separate billing):"
echo "      gcloud run jobs update ${JOB} --region=${REGION} \\"
echo "        --update-env-vars=YTFACTORY_LLM_BACKEND=anthropic_sdk \\"
echo "        --update-secrets=ANTHROPIC_API_KEY=ytfactory-anthropic-key:latest"
echo ""
echo "    Falling back to stub mode (no LLM key needed):"
echo "      gcloud run jobs update ${JOB} --region=${REGION} \\"
echo "        --update-env-vars=YTFACTORY_RENDER_MODE=stub"
