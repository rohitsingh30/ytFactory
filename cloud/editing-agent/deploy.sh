#!/usr/bin/env bash
# Build + deploy ytfactory-editing-agent — Cloud Run SERVICE.
#
# Sibling of cloud/render-worker-v2 (which is a JOB). Runs as a
# SERVICE because each /edit request is bounded (~30s polish,
# ~2-5min assemble) — pure HTTP, no Firestore.
#
# Build context = repo root (so we can COPY pipeline/editing/ and
# pipeline/cloud/ from the same code the laptop executor uses).

set -euo pipefail

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"  # same Artifact Registry repo as the other services
SERVICE="ytfactory-editing-agent"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

echo "==> Building + pushing ${IMAGE}"
echo "    (build context = ${REPO_ROOT})"
gcloud builds submit . \
  --config=cloud/editing-agent/cloudbuild.yaml \
  --substitutions="_IMAGE=${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=1800s

echo "==> Deploying Cloud Run SERVICE ${SERVICE}"
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="tts-runner@${PROJECT}.iam.gserviceaccount.com" \
  --execution-environment=gen2 \
  --memory=8Gi --cpu=4 \
  --concurrency=4 \
  --min-instances=0 \
  --max-instances=4 \
  --timeout=900 \
  --no-allow-unauthenticated \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT},YTFACTORY_BUCKET=ytfactory-prod-v2-artifacts,LOG_LEVEL=INFO,IMAGE_SHA=${TAG}"

URL="$(gcloud run services describe "${SERVICE}" \
  --project="${PROJECT}" --region="${REGION}" \
  --format='value(status.url)')"

echo ""
echo "==> Service deployed: ${URL}"
echo ""
echo "    Wire on the laptop / render-worker-v2:"
echo "      export CLOUDRUN_EDITING_AGENT_URL=${URL}"
echo ""
echo "    Smoke test /readyz (auth required):"
echo "      TOKEN=\$(gcloud auth print-identity-token --audiences=${URL})"
echo "      curl -H \"Authorization: Bearer \$TOKEN\" ${URL}/readyz"
echo ""
echo "    Admin tab: visit /app/cloud — editing-agent will appear in the"
echo "    VIDEO chip alongside render-worker-v2 and clone-video-worker."
