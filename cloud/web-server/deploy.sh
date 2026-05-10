#!/usr/bin/env bash
# ============================================================================
# CANONICAL (Phase 4 — 2026-05-10). This is the single deploy target.
#
# Phase 4 consolidation reversed the earlier "control-plane is canonical"
# call. ytfactory-web absorbs all of control's routers (agent, scheduler,
# state API, niche schema, channels, niches, voices, burners, etc.) at
# the bottom of web/server.py. ytfactory-control is retired.
#
# Run in this order:
#   ./cloud/render-worker-v2/deploy.sh    # the render JOB (unchanged)
#   ./cloud/web-server/deploy.sh          # THE service (this script)
#
# Full runbook: docs/deploy.md.
# ============================================================================
#
# Build + push + deploy ytfactory-web (the orchestrator) to Cloud Run.
#
# Cloud Run SERVICE (always-on, public). Hosts /, /renders, /dashboard,
# /api/*. Renders go through the render-worker JOB
# (YTFACTORY_RENDER_BACKEND=cloudrun is baked into the image).
set -euo pipefail

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
SERVICE="ytfactory-web"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

echo "==> Building + pushing ${IMAGE}"
gcloud builds submit . \
  --config=cloud/web-server/cloudbuild.yaml \
  --substitutions="_IMAGE=${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=1800s

# Auth env (2026-05-10 — Stage 2 sign-in flow):
#
# - YTFACTORY_WEB_OAUTH_CLIENT — Google web-app OAuth client JSON. Used
#   by pipeline.auth for the browser sign-in flow.
# - YTFACTORY_SESSION_SECRET — HMAC key for the yt_session cookie.
# - YTFACTORY_AGENT_TOKEN — shared bearer for M2M callers (Cloud
#   Scheduler, laptop agent, skill_dispatch).
# - YTFACTORY_CLIENT_SECRET — kept for the per-channel YouTube OAuth
#   flow (oauth_web_routes.py, burner-channel onboarding); separate
#   client (installed type, localhost:8089 callback).
#
# All four mounted via --set-secrets; values rotate by writing a new
# secret version, no redeploy needed.

echo "==> Deploying ${SERVICE} to Cloud Run"
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="tts-runner@${PROJECT}.iam.gserviceaccount.com" \
  --execution-environment=gen2 \
  --memory=2Gi --cpu=1 --cpu-boost \
  --concurrency=30 \
  --max-instances=2 \
  --min-instances=1 \
  --timeout=300 \
  --port=8080 \
  --allow-unauthenticated \
  --set-secrets="^|^AZURE_OPENAI_API_KEY=azure-openai-key:latest|YTFACTORY_CLIENT_SECRET=ytfactory-oauth-client:latest|YTFACTORY_WEB_OAUTH_CLIENT=ytfactory-web-oauth-client:latest|YTFACTORY_SESSION_SECRET=ytfactory-session-secret:latest|YTFACTORY_AGENT_TOKEN=ytfactory-agent-token:latest" \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=${PROJECT}|YTFACTORY_BUCKET=ytfactory-prod-v2-artifacts|YTFACTORY_STATE_BUCKET=ytfactory-prod-v2-state|YTFACTORY_QUEUE_BACKEND=firestore|YTFACTORY_SIM_WORKER=0|YTFACTORY_RENDER_BACKEND=cloudrun|YTFACTORY_CLOUDRUN_JOB=ytfactory-render-worker-v2|YTFACTORY_CLOUDRUN_REGION=${REGION}|YTFACTORY_COOKIE_SECURE=1|YTFACTORY_ADMIN_DOMAINS=docx.co.in|YTFACTORY_PUBLIC_FRONTEND_URL=https://ytfactory-web-next-7hwnzw7lya-as.a.run.app|YTFACTORY_AUTH_REDIRECT_URI=https://ytfactory-web-next-7hwnzw7lya-as.a.run.app/api/auth/google/callback|CLOUDRUN_TTS_CHATTERBOX_URL=https://ytfactory-tts-chatterbox-283470729204.${REGION}.run.app|CLOUDRUN_TTS_INDICF5_URL=https://ytfactory-tts-indicf5-283470729204.${REGION}.run.app|CLOUDRUN_IMAGE_FLUX2_KLEIN_URL=https://ytfactory-image-flux2-klein-283470729204.${REGION}.run.app"

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")
echo ""
echo "==> Deployed: ${URL}"
echo "    Open: ${URL}/renders"
