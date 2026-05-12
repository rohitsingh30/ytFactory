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

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
JOB="ytfactory-render-worker-v2"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${JOB}:${TAG}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

echo "==> Building + pushing ${IMAGE}"
echo "    (build context = ${REPO_ROOT})"
gcloud builds submit . \
  --config=cloud/render-worker-v2/cloudbuild.yaml \
  --substitutions="_IMAGE=${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=3600s

# Azure OpenAI wiring — these MUST be on the JOB or preflight aborts every
# render at stage=bootstrap with "AZURE_OPENAI_ENDPOINT missing". They were
# previously left as a manual post-deploy step which got skipped on ~every
# redeploy (--set-env-vars below replaces ALL env vars). Baked in now so
# `bash deploy.sh` is sufficient.
#
# Override per-deploy by exporting AZURE_OPENAI_ENDPOINT / _API_VERSION /
# _MODEL before invoking the script. Defaults match the chat assistant's
# Azure deployment (ytfactory-web service env, 2026-05-12).
AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-https://testshoffer.openai.azure.com}"
AZURE_OPENAI_API_VERSION="${AZURE_OPENAI_API_VERSION:-2025-04-01-preview}"
AZURE_OPENAI_MODEL="${AZURE_OPENAI_MODEL:-gpt-5.3-chat}"

echo "==> Creating/updating Cloud Run JOB ${JOB}"
gcloud run jobs deploy "${JOB}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="tts-runner@${PROJECT}.iam.gserviceaccount.com" \
  --execution-environment=gen2 \
  --memory=8Gi --cpu=4 \
  --max-retries=0 \
  --task-timeout=3600 \
  --set-secrets="AZURE_OPENAI_API_KEY=azure-openai-key:latest" \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=${PROJECT}|YTFACTORY_BUCKET=ytfactory-prod-v2-artifacts|CLOUDRUN_TTS_CHATTERBOX_URL=https://ytfactory-tts-chatterbox-283470729204.${REGION}.run.app|CLOUDRUN_TTS_INDICPARLER_URL=https://ytfactory-tts-indicparler-283470729204.${REGION}.run.app|CLOUDRUN_IMAGE_FLUX2_KLEIN_URL=https://ytfactory-image-flux2-klein-283470729204.${REGION}.run.app|CLOUDRUN_TTS_DISABLE_FALLBACK=1|CLOUDRUN_IMAGE_DISABLE_FALLBACK=1|YTFACTORY_RENDER_MODE=real|YTFACTORY_LLM_BACKEND=azure_openai|AZURE_OPENAI_ENDPOINT=${AZURE_OPENAI_ENDPOINT}|AZURE_OPENAI_API_VERSION=${AZURE_OPENAI_API_VERSION}|AZURE_OPENAI_MODEL=${AZURE_OPENAI_MODEL}|YTFACTORY_ASR_PROVIDER=faster_whisper|LOG_LEVEL=INFO"

echo ""
echo "==> Job deployed (mode=real, llm=azure_openai, endpoint=${AZURE_OPENAI_ENDPOINT}, model=${AZURE_OPENAI_MODEL})."
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
