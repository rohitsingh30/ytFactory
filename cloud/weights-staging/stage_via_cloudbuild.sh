#!/usr/bin/env bash
# Wrapper around cloudbuild.yaml — submit a weights-staging build that
# downloads + uploads one or more HF repos to gs://ytfactory-model-
# weights-v2/flat/. See cloudbuild.yaml header for design rationale.
#
# Usage:
#   bash cloud/weights-staging/stage_via_cloudbuild.sh \
#     black-forest-labs/FLUX.2-dev
#
#   bash cloud/weights-staging/stage_via_cloudbuild.sh \
#     black-forest-labs/FLUX.2-dev,Tongyi-MAI/Z-Image-Turbo
#
#   # Async:
#   ASYNC=1 bash cloud/weights-staging/stage_via_cloudbuild.sh \
#     black-forest-labs/FLUX.2-dev

set -euo pipefail

# Bake in the ADC-token auth bypass per cloud/_shared/auth_setup.sh.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

REPOS="${1:-black-forest-labs/FLUX.2-dev}"
BUCKET="${2:-ytfactory-prod-v3-model-weights}"
PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"

cd "$(dirname "$0")"

echo "==> staging via Cloud Build"
echo "    repos:   ${REPOS}"
echo "    bucket:  gs://${BUCKET}/flat/<repo>/"
echo "    project: ${PROJECT}"
echo ""

# --no-source: cloudbuild.yaml carries everything inline; we don't
# upload our repo as build context (saves ~300 MB upload).
ARGS=(
  builds submit
  --config=cloudbuild.yaml
  --no-source
  --project="${PROJECT}"
  --substitutions="_REPOS=${REPOS},_BUCKET=${BUCKET}"
  --timeout=7200s
)

if [ "${ASYNC:-0}" = "1" ]; then
  ARGS+=(--async --format='value(id)')
  BUILD_ID=$(gcloud "${ARGS[@]}" 2>/dev/null | tr -d '[:space:]')
  echo "==> Build ID: ${BUILD_ID}"
  echo "==> Console:  https://console.cloud.google.com/cloud-build/builds/${BUILD_ID}?project=${PROJECT}"
  echo "==> Tail logs:"
  echo "    gcloud beta builds log ${BUILD_ID} --stream --project=${PROJECT}"
else
  # Sync: source ../_shared/submit_build.sh's polling pattern so the
  # VPC-SC log-streaming exit doesn't break us.
  source "$(cd "$(dirname "$0")" && pwd)/../_shared/submit_build.sh"
  out=$(gcloud "${ARGS[@]}" --async --format='value(id)' 2>/dev/null)
  BUILD_ID=$(echo "${out}" | tr -d '[:space:]')
  echo "==> Build ID: ${BUILD_ID}"
  echo "==> Poll URL: https://console.cloud.google.com/cloud-build/builds/${BUILD_ID}?project=${PROJECT}"

  deadline=$((SECONDS + 7500))
  while [ $SECONDS -lt $deadline ]; do
    status=$(gcloud builds describe "${BUILD_ID}" --project="${PROJECT}" --format='value(status)' 2>/dev/null || echo '?')
    case "$status" in
      SUCCESS) echo "==> Build ${BUILD_ID} SUCCESS"; exit 0 ;;
      FAILURE|CANCELLED|TIMEOUT|EXPIRED) echo "==> Build ${BUILD_ID} ${status}" >&2; exit 1 ;;
      WORKING|QUEUED|"") echo "    ...status=${status} (${SECONDS}s elapsed)"; sleep 30 ;;
      *) echo "    ...unknown status=${status}"; sleep 30 ;;
    esac
  done
  echo "==> Build ${BUILD_ID} timed out (${SECONDS}s)" >&2
  exit 1
fi
