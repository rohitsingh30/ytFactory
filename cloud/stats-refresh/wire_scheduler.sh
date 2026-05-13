#!/usr/bin/env bash
# ============================================================================
# A3 — Wire Cloud Scheduler ytfactory-stats-refresh-cron.
# ============================================================================
#
# Hourly cron (Asia/Kolkata) that triggers the ytfactory-stats-refresh
# Cloud Run JOB via OIDC. Mirrors the existing ytfactory-tick scheduler
# (control/routes/scheduler_routes.py) — same SA, same auth pattern.
#
# Run AFTER cloud/stats-refresh/deploy.sh has created the JOB.
#
# Usage:
#   ./cloud/stats-refresh/wire_scheduler.sh
#   ./cloud/stats-refresh/wire_scheduler.sh --pause     # pause the cron
#   ./cloud/stats-refresh/wire_scheduler.sh --resume    # resume the cron
#   ./cloud/stats-refresh/wire_scheduler.sh --delete    # remove the cron
# ============================================================================

set -euo pipefail

# Audit D3.23 — bypass interactive gcloud reauth via ADC.
# Source the shared helper so a single `gcloud auth
# application-default login` covers every wire-up script.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
JOB="ytfactory-stats-refresh"
SCHEDULE_NAME="ytfactory-stats-refresh-cron"
SCHEDULE="${SCHEDULE:-0 * * * *}"        # hourly on the hour
TZ="${TZ:-Asia/Kolkata}"
SCHED_SA="ytfactory-scheduler@${PROJECT}.iam.gserviceaccount.com"

case "${1:-}" in
  --pause)
    gcloud scheduler jobs pause "${SCHEDULE_NAME}" \
      --project="${PROJECT}" --location="${REGION}"
    exit 0 ;;
  --resume)
    gcloud scheduler jobs resume "${SCHEDULE_NAME}" \
      --project="${PROJECT}" --location="${REGION}"
    exit 0 ;;
  --delete)
    gcloud scheduler jobs delete "${SCHEDULE_NAME}" \
      --project="${PROJECT}" --location="${REGION}" --quiet
    exit 0 ;;
esac

# Cloud Scheduler invokes a Cloud Run JOB by hitting the JOB-execute
# REST API directly with an OIDC token.
URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB}:run"

if gcloud scheduler jobs describe "${SCHEDULE_NAME}" \
       --project="${PROJECT}" --location="${REGION}" >/dev/null 2>&1; then
  echo "==> Updating existing scheduler ${SCHEDULE_NAME}"
  gcloud scheduler jobs update http "${SCHEDULE_NAME}" \
    --project="${PROJECT}" --location="${REGION}" \
    --schedule="${SCHEDULE}" \
    --time-zone="${TZ}" \
    --uri="${URI}" \
    --http-method=POST \
    --oidc-service-account-email="${SCHED_SA}" \
    --oidc-token-audience="https://${REGION}-run.googleapis.com/" \
    --description="Hourly YouTube stats refresh — token-issue plan A3."
else
  echo "==> Creating scheduler ${SCHEDULE_NAME}"
  gcloud scheduler jobs create http "${SCHEDULE_NAME}" \
    --project="${PROJECT}" --location="${REGION}" \
    --schedule="${SCHEDULE}" \
    --time-zone="${TZ}" \
    --uri="${URI}" \
    --http-method=POST \
    --oidc-service-account-email="${SCHED_SA}" \
    --oidc-token-audience="https://${REGION}-run.googleapis.com/" \
    --description="Hourly YouTube stats refresh — token-issue plan A3."
fi

echo
echo "==> Scheduler wired. Verify with:"
echo "    gcloud scheduler jobs describe ${SCHEDULE_NAME} \\"
echo "      --project=${PROJECT} --location=${REGION}"
echo
echo "==> The scheduler SA needs roles/run.invoker on the JOB:"
echo "    gcloud run jobs add-iam-policy-binding ${JOB} \\"
echo "      --project=${PROJECT} --region=${REGION} \\"
echo "      --member=serviceAccount:${SCHED_SA} \\"
echo "      --role=roles/run.invoker"
