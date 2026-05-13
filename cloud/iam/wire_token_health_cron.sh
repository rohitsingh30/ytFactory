#!/usr/bin/env bash
# ============================================================================
# D4 — Wire Cloud Scheduler ytfactory-token-health-cron + alert webhook.
# ============================================================================
#
# Daily probe of /api/cron/token-health on the ytfactory-web service.
# Cloud Scheduler invokes the URL with an OIDC token; the existing M2M
# middleware (web/server.py::_M2M_PATH_PREFIXES `/api/cron/`) trusts
# K_SERVICE-validated callers without the user_is_admin check.
#
# When summary.broken > 0, the response body is forwarded to whatever
# webhook YTFACTORY_TOKEN_HEALTH_WEBHOOK is set to (Slack / Discord /
# generic JSON POST). If unset, the cron just logs to Cloud Logging
# and you'll see broken counts via:
#
#   gcloud logging read 'resource.type="cloud_scheduler_job"
#     AND resource.labels.job_id="ytfactory-token-health-cron"' \
#     --project ytfactory-prod-v2 --limit 50 --format=json | jq
#
# Run AFTER the web service is deployed with the /api/cron/token-health
# endpoint on it (it ships in the same image as /api/admin/token-health
# — D3 of the plan).
#
# Usage:
#   ./cloud/iam/wire_token_health_cron.sh
#   ./cloud/iam/wire_token_health_cron.sh --pause
#   ./cloud/iam/wire_token_health_cron.sh --resume
#   ./cloud/iam/wire_token_health_cron.sh --delete
#   ./cloud/iam/wire_token_health_cron.sh --run-now    # one-shot trigger
# ============================================================================

set -euo pipefail

# Audit D3.23 — bypass interactive gcloud reauth via ADC.
# Source the shared helper so a single `gcloud auth
# application-default login` covers every wire-up script.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
WEB_SERVICE="${WEB_SERVICE:-ytfactory-web}"
SCHEDULE_NAME="ytfactory-token-health-cron"
SCHEDULE="${SCHEDULE:-0 4 * * *}"     # 04:00 IST daily (= 22:30 UTC prev day)
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
  --run-now)
    gcloud scheduler jobs run "${SCHEDULE_NAME}" \
      --project="${PROJECT}" --location="${REGION}"
    exit 0 ;;
esac

WEB_URL=$(gcloud run services describe "${WEB_SERVICE}" \
  --project="${PROJECT}" --region="${REGION}" --format='value(status.url)')

URI="${WEB_URL}/api/cron/token-health"

if gcloud scheduler jobs describe "${SCHEDULE_NAME}" \
       --project="${PROJECT}" --location="${REGION}" >/dev/null 2>&1; then
  echo "==> Updating existing scheduler ${SCHEDULE_NAME}"
  gcloud scheduler jobs update http "${SCHEDULE_NAME}" \
    --project="${PROJECT}" --location="${REGION}" \
    --schedule="${SCHEDULE}" \
    --time-zone="${TZ}" \
    --uri="${URI}" \
    --http-method=GET \
    --oidc-service-account-email="${SCHED_SA}" \
    --oidc-token-audience="${WEB_URL}" \
    --description="Daily YouTube OAuth-token health probe — token-issue plan D4."
else
  echo "==> Creating scheduler ${SCHEDULE_NAME}"
  gcloud scheduler jobs create http "${SCHEDULE_NAME}" \
    --project="${PROJECT}" --location="${REGION}" \
    --schedule="${SCHEDULE}" \
    --time-zone="${TZ}" \
    --uri="${URI}" \
    --http-method=GET \
    --oidc-service-account-email="${SCHED_SA}" \
    --oidc-token-audience="${WEB_URL}" \
    --description="Daily YouTube OAuth-token health probe — token-issue plan D4."
fi

echo
echo "==> Wired. Verify with:"
echo "    gcloud scheduler jobs describe ${SCHEDULE_NAME} \\"
echo "      --project=${PROJECT} --location=${REGION}"
echo
echo "==> Trigger one-shot test:"
echo "    $0 --run-now"
echo
echo "==> Read recent cron output:"
echo "    gcloud logging read \\"
echo "      'resource.type=\"cloud_scheduler_job\" AND"
echo "       resource.labels.job_id=\"${SCHEDULE_NAME}\"' \\"
echo "      --project=${PROJECT} --limit 10 --format=json"
echo
echo "==> Outbound alert hook (optional, host on a side service):"
echo "    Forward the response body where summary.broken > 0 to a Slack /"
echo "    Discord webhook. Sample one-liner inside the JOB:"
echo
echo "      jq -e '.summary.broken > 0' <<<\"\$RESPONSE\" && \\"
echo "        curl -X POST \"\$YTFACTORY_TOKEN_HEALTH_WEBHOOK\" \\"
echo "          -H 'content-type: application/json' \\"
echo "          --data \"\$RESPONSE\""
