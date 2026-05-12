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

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

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
  --ignore-file=cloud/web-server/.gcloudignore \
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
# All five mounted via --set-secrets; values rotate by writing a new
# secret version, no redeploy needed. NOTE: --set-secrets is destructive
# (replaces the entire secret list), so every secret used by ytfactory-web
# MUST be listed here, including:
#   - YOUTUBE_API_KEY  (added 2026-05-10 for the dashboard cards — without
#     this, control/routes/dashboard_routes.py reports
#     "YOUTUBE_API_KEY not set" and stats are blank).

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
  --set-secrets="^|^AZURE_OPENAI_API_KEY=azure-openai-key:latest|YTFACTORY_CLIENT_SECRET=ytfactory-oauth-client:latest|YTFACTORY_WEB_OAUTH_CLIENT=ytfactory-web-oauth-client:latest|YTFACTORY_SESSION_SECRET=ytfactory-session-secret:latest|YTFACTORY_AGENT_TOKEN=ytfactory-agent-token:latest|YOUTUBE_API_KEY=youtube-api-key:latest|/secrets/youtube-channel-ids/value=youtube-channel-ids:latest" \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=${PROJECT}|YTFACTORY_BUCKET=ytfactory-prod-v2-artifacts|YTFACTORY_STATE_BUCKET=ytfactory-prod-v2-state|YTFACTORY_QUEUE_BACKEND=firestore|YTFACTORY_SIM_WORKER=0|YTFACTORY_RENDER_BACKEND=cloudrun|YTFACTORY_CLOUDRUN_JOB=ytfactory-render-worker-v2|YTFACTORY_CLOUDRUN_REGION=${REGION}|YTFACTORY_COOKIE_SECURE=1|YTFACTORY_ADMIN_DOMAINS=docx.co.in|YTFACTORY_PUBLIC_FRONTEND_URL=https://ytfactory-web-next-7hwnzw7lya-as.a.run.app|YTFACTORY_AUTH_REDIRECT_URI=https://ytfactory-web-next-7hwnzw7lya-as.a.run.app/api/auth/google/callback|CLOUDRUN_TTS_CHATTERBOX_URL=https://ytfactory-tts-chatterbox-283470729204.${REGION}.run.app|CLOUDRUN_TTS_INDICF5_URL=https://ytfactory-tts-indicf5-283470729204.${REGION}.run.app|CLOUDRUN_IMAGE_FLUX2_KLEIN_URL=https://ytfactory-image-flux2-klein-283470729204.${REGION}.run.app|AZURE_OPENAI_ENDPOINT=https://testshoffer.openai.azure.com|AZURE_OPENAI_API_VERSION=2025-04-01-preview|AZURE_OPENAI_MODEL=gpt-5.3-chat"
# AZURE_OPENAI_ENDPOINT/API_VERSION/MODEL are non-secret triplet
# REQUIRED for chat_service.py and niche_specs_routes.py to talk to
# Azure (control/chat_service.py:96-105 + control/routes/niche_specs_routes.py:148-151
# both `return None` if AZURE_OPENAI_ENDPOINT is empty → chat falls back
# to "AI not configured" + niche-draft falls back to deterministic stub).
# Pre-2026-05-10 deploys had only AZURE_OPENAI_API_KEY (the secret),
# leaving the chat assistant + niche-draft auto-generate silently dead.
# Full post-mortem: docs/azure_openai_deploy_env.md.

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")
echo ""
echo "==> Deployed: ${URL}"
echo "    Open: ${URL}/renders"
