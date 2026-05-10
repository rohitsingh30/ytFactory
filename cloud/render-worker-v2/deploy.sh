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
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=${PROJECT}|YTFACTORY_BUCKET=ytfactory-prod-v2-artifacts|CLOUDRUN_TTS_CHATTERBOX_URL=https://ytfactory-tts-chatterbox-283470729204.${REGION}.run.app|CLOUDRUN_TTS_INDICPARLER_URL=https://ytfactory-tts-indicparler-283470729204.${REGION}.run.app|CLOUDRUN_IMAGE_FLUX2_KLEIN_URL=https://ytfactory-image-flux2-klein-283470729204.${REGION}.run.app|CLOUDRUN_TTS_DISABLE_FALLBACK=1|CLOUDRUN_IMAGE_DISABLE_FALLBACK=1|YTFACTORY_RENDER_MODE=real|YTFACTORY_LLM_BACKEND=azure_openai|YTFACTORY_ASR_PROVIDER=faster_whisper|LOG_LEVEL=INFO"

echo ""
echo "==> Job deployed (mode=real, llm=azure_openai)."
echo ""
echo "    PRE-FLIGHT — wire Azure OpenAI secrets onto the JOB:"
echo "      # The same Azure deployment your chat assistant already uses."
echo "      gcloud run jobs update ${JOB} \\"
echo "        --project=${PROJECT} --region=${REGION} \\"
echo "        --update-secrets=AZURE_OPENAI_API_KEY=azure-openai-key:latest \\"
echo "        --update-env-vars=^|^AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/|AZURE_OPENAI_MODEL=gpt-4o-mini"
echo ""
echo "    Trigger one execution manually:"
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
