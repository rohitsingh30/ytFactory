#!/usr/bin/env bash
# Build + deploy cobalt downloader API to Cloud Run.
# Usage: ./deploy.sh [tag]

set -euo pipefail

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

TAG="${1:-$(date +%Y%m%d-%H%M%S)}"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
SERVICE="ytfactory-cobalt-api"
REPO="ytfactory-tts"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

cd "$(dirname "$0")"

echo "==> Building + pushing ${IMAGE}"
gcloud builds submit . \
  --tag="${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=900s

echo "==> Deploying ${SERVICE} (CPU-only, public) to Cloud Run, ${REGION}"

# Get the deployed URL after deploy so API_URL points at the right
# host (cobalt rejects requests where the Host header doesn't match
# its configured API_URL — Cloud Run won't tell us our URL until
# AFTER deployment, so we deploy twice: first with a placeholder,
# then update with the actual URL.)
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="cobalt-runner@${PROJECT}.iam.gserviceaccount.com" \
  --memory=2Gi \
  --cpu=2 \
  --cpu-boost \
  --concurrency=8 \
  --max-instances=4 \
  --min-instances=0 \
  --timeout=600 \
  --no-allow-unauthenticated \
  --execution-environment=gen2 \
  --port=8080
# Audit S1.22 — pin to the dedicated tts-runner SA so the
# default Compute Engine SA's broad project privileges aren't
# inherited by a yt-dlp / cobalt downloader container.

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")

# Now patch API_URL so cobalt accepts requests on its real hostname.
gcloud run services update "${SERVICE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --update-env-vars="API_URL=${URL}/" \
  --quiet

echo ""
echo "==> Deployed: ${URL}"
echo ""
echo "Set on the laptop / clone-video-worker:"
echo "  export CLOUDRUN_COBALT_URL=${URL}"
