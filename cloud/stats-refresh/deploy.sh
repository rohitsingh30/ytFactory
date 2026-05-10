#!/usr/bin/env bash
# ============================================================================
# Build + deploy ytfactory-stats-refresh — Cloud Run JOB.
# ============================================================================
#
# A2 of the 2026-05-10 token-issue permanent fix.
#
# Cloud-side replacement for "human runs `python -m pipeline.research.youtube`
# on the laptop and commits the cache". Triggered hourly by Cloud
# Scheduler ytfactory-stats-refresh-cron (see wire_scheduler.sh) or
# manually for backfills. Refreshes every channel discoverable via
# pipeline/channels/*.yaml, writes per-account JSON to:
#
#   gs://$YTFACTORY_STATE_BUCKET/data/research/youtube/<account>.json
#
# Build context = repo root.
#
# Usage:
#   ./cloud/stats-refresh/deploy.sh                  # build + deploy
#   ./cloud/stats-refresh/deploy.sh --skip-build     # update env / SA only
#
# After the JOB exists:
#   ./cloud/stats-refresh/wire_scheduler.sh         # hourly cron
#
# Manual seed (B3 in the plan):
#   gcloud run jobs execute ytfactory-stats-refresh \
#     --project ytfactory-prod-v2 --region asia-southeast1 --wait
# ============================================================================

set -euo pipefail

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
JOB="ytfactory-stats-refresh"
TAG="${TAG:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${JOB}:${TAG}"
RUNTIME_SA="tts-runner@${PROJECT}.iam.gserviceaccount.com"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

if [[ "${1:-}" != "--skip-build" ]]; then
  echo "==> Building + pushing ${IMAGE}"
  gcloud builds submit . \
    --config=cloud/stats-refresh/cloudbuild.yaml \
    --substitutions="_IMAGE=${IMAGE}" \
    --project="${PROJECT}" \
    --timeout=1200s
fi

echo "==> Creating/updating Cloud Run JOB ${JOB}"

# All 9 youtube-token-* Secret Manager secrets — same list the
# render-worker mounts. Per the 2026-05-09 cutover.
SECRET_MOUNTS_RAW="
/secrets/youtube-token-mystoriesanimated/value=youtube-token-mystoriesanimated:latest,
/secrets/youtube-token-cosmosdecoded/value=youtube-token-cosmosdecoded:latest,
/secrets/youtube-token-historyrecapped/value=youtube-token-historyrecapped:latest,
/secrets/youtube-token-hindutavaanimated/value=youtube-token-hindutavaanimated:latest,
/secrets/youtube-token-sportsrecapped/value=youtube-token-sportsrecapped:latest,
/secrets/youtube-token-scrollpulse/value=youtube-token-scrollpulse:latest,
/secrets/youtube-token-rhymetimejunction/value=youtube-token-rhymetimejunction:latest,
/secrets/youtube-token-afddfdf/value=youtube-token-afddfdf:latest,
/secrets/youtube-token-zgsbhqszdheo/value=youtube-token-zgsbhqszdheo:latest
"
SECRET_MOUNTS=$(echo "${SECRET_MOUNTS_RAW}" | tr -d '\n ')

gcloud run jobs deploy "${JOB}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="${RUNTIME_SA}" \
  --execution-environment=gen2 \
  --memory=512Mi --cpu=1 \
  --max-retries=1 \
  --task-timeout=900 \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=${PROJECT}|YTFACTORY_STATE_BUCKET=${PROJECT}-state|LOG_LEVEL=INFO" \
  --set-secrets="${SECRET_MOUNTS}"

echo
echo "==> Done. Manual one-shot to seed the cache (B3):"
echo "    gcloud run jobs execute ${JOB} \\"
echo "      --project=${PROJECT} --region=${REGION} --wait"
echo
echo "==> Then wire the hourly scheduler:"
echo "    ./cloud/stats-refresh/wire_scheduler.sh"
