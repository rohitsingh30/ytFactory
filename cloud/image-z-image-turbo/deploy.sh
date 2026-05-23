#!/usr/bin/env bash
# Build + push + deploy the Z-Image-Turbo 6B image service to
# Cloud Run GPU L4 in asia-southeast1.
#
# Usage:
#   ./deploy.sh                # uses default service name + auto tag
#   ./deploy.sh <service-name> # custom service name
#   ./deploy.sh <service-name> <tag>

set -euo pipefail

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

SERVICE="${1:-ytfactory-image-z-image-turbo}"
TAG="${2:-$(date +%Y%m%d-%H%M%S)}"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v3}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# Inline async-submit + poll. We can't use submit_build.sh — it hardcodes
# --tag which conflicts with --config=cloudbuild.yaml. The cloudbuild.yaml
# is what stages the 33 GiB weights in a step that has ADC, then COPYs
# them into the image; a plain --tag build can't do that (Docker RUN
# steps have no metadata-server access).
echo "==> Building + pushing ${IMAGE}"
echo "    (build context = ${REPO_ROOT}, config = cloud/image-z-image-turbo/cloudbuild.yaml)"
# --region pins build to asia-southeast1 to colocate with AR. Without
# this the 33 GiB image push goes US→APAC and racks up ~₹400/build of
# intercontinental egress (caught 2026-05-17 cost audit). See
# docs/cost_guardrails.md.
BUILD_ID=$(gcloud builds submit . \
  --region="${REGION}" \
  --config=cloud/image-z-image-turbo/cloudbuild.yaml \
  --substitutions="_IMAGE=${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=5400s \
  --async \
  --format="value(id)" 2>/dev/null)
if [ -z "${BUILD_ID}" ] || ! echo "${BUILD_ID}" | grep -qE '^[a-f0-9-]{20,}$'; then
  echo "ERROR: failed to submit build (got '${BUILD_ID}')" >&2
  exit 1
fi
echo "==> Build ID: ${BUILD_ID}"
echo "==> Poll URL: https://console.cloud.google.com/cloud-build/builds/${BUILD_ID}?project=${PROJECT}&region=${REGION}"
DEADLINE=$((SECONDS + 5700))
while [ $SECONDS -lt $DEADLINE ]; do
  STATUS=$(gcloud builds describe "${BUILD_ID}" --region="${REGION}" --project="${PROJECT}" --format="value(status)" 2>/dev/null || echo "?")
  case "${STATUS}" in
    SUCCESS) echo "==> Build SUCCESS"; break ;;
    FAILURE|CANCELLED|TIMEOUT|EXPIRED|INTERNAL_ERROR) echo "==> Build ${STATUS}" >&2; exit 1 ;;
    *) echo "    ...status=${STATUS} (${SECONDS}s elapsed)"; sleep 30 ;;
  esac
done

echo "==> Deploying ${SERVICE} to Cloud Run (L4 GPU, ${REGION})"
# 32Gi mem + 8 CPU kept INTACT — we know this fits the load with
# low_cpu_mem_usage=True (multiple successful loads in prior logs).
# Shrinking to 16Gi/4CPU risks OOM on model load; that's the explicit
# constraint from the operator. concurrency=1, max-instances=1 to cap GPU
# spend; min-instances=0 since weights load fast from local SSD now.
#
# No more --add-volume gcsfuse mount — weights are baked into the image.
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="image-runner@${PROJECT}.iam.gserviceaccount.com" \
  --gpu=1 \
  --gpu-type=nvidia-l4 \
  --no-gpu-zonal-redundancy \
  --no-cpu-throttling \
  --memory=32Gi \
  --cpu=8 \
  --cpu-boost \
  --concurrency=1 \
  --max-instances=1 \
  --min-instances=0 \
  --timeout=3600 \
  --no-allow-unauthenticated \
  --set-env-vars="GCS_BUCKET=ytfactory-tts-io,LOG_LEVEL=INFO" \
  --execution-environment=gen2 \
  --clear-volume-mounts \
  --clear-volumes

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")
echo ""
echo "==> Deployed: ${URL}"
echo "Set on the laptop:"
echo "  export CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL=${URL}"
