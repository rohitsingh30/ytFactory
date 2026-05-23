#!/usr/bin/env bash
# ============================================================================
# CANONICAL (Phase 4 — 2026-05-10). This is the single deploy target.
#
# Phase 4 consolidation reversed the earlier "control-plane is canonical"
# call. ytfactory-web absorbs all of control's routers (agent, scheduler,
# state API, niche schema, channels, niches, voices, etc.) at
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

# Pre-flight reminder (post-S1.21 + post-2026-05-13 silent-Firestore-500 trap):
#
# The `web-runner@` SA needs MORE than just secret accessor — it needs
# Firestore, both state + artifacts buckets, IAM signBlob, and
# run.invoker. The 2026-05-12 SA flip from `tts-runner@` lost all of
# those silently because none fail at deploy time — they only fail
# on first user request (Firestore: sign-in callback; signBlob: any
# signed-URL endpoint; run.invoker: render kickoff). 13-hour outage.
#
# Now we BOTH:
#   1. Run a read-only IAM verifier as preflight (this script, below).
#      Aborts the deploy if any required binding is missing.
#   2. Surface a one-liner repair command (`bash cloud/iam/grant_web_runner.sh`).
#
# Memory: feedback_web_runner_iam_silent_post_deploy_500.md
# Doc:    docs/iam_per_service.md § "Roles per SA → web-runner".

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

# Preflight: IAM bindings for the runtime SA. Aborts deploy with a
# concrete grant command if anything is missing. Read-only — never
# mutates IAM (operator may not have project-IAM-admin).
# Skippable via YTFACTORY_SKIP_IAM_PREFLIGHT=1 — useful when the
# operator can't grant IAM but the bindings already exist (e.g. a
# prior deploy succeeded). Verify failures still surface concrete
# repair commands in the script's stderr.
if [ "${YTFACTORY_SKIP_IAM_PREFLIGHT:-0}" != "1" ]; then
  echo "==> Verifying web-runner IAM bindings (preflight)"
  "$(cd "$(dirname "$0")" && pwd)/../iam/verify_web_runner.sh"
else
  echo "==> Skipping IAM preflight (YTFACTORY_SKIP_IAM_PREFLIGHT=1)"
fi

PROJECT="${GCP_PROJECT:-ytfactory-prod-v3}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
SERVICE="ytfactory-web"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

echo "==> Building + pushing ${IMAGE}"
# --region pins build to asia-southeast1 (2026-05-17 cost-audit rule 3).
gcloud builds submit . \
  --region="${REGION}" \
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
#   flow (oauth_web_routes.py); separate client (installed type,
#   localhost:8089 callback).
#
# All five mounted via --set-secrets; values rotate by writing a new
# secret version, no redeploy needed. NOTE: --set-secrets is destructive
# (replaces the entire secret list), so every secret used by ytfactory-web
# MUST be listed here, including:
#   - YOUTUBE_API_KEY  (added 2026-05-10 for the dashboard cards — without
#     this, control/routes/dashboard_routes.py reports
#     "YOUTUBE_API_KEY not set" and stats are blank).

# Resolve live Cloud Run URLs at deploy time. Hardcoding host hashes is
# brittle — they change when a service is recreated. `gcloud run services
# describe` returns the current canonical URL.
_resolve_url() {
  local svc="$1"
  gcloud run services describe "${svc}" \
    --project="${PROJECT}" --region="${REGION}" \
    --format="value(status.url)" 2>/dev/null
}
WEB_NEXT_URL="$(_resolve_url ytfactory-web-next)"
TTS_CHATTERBOX_URL="$(_resolve_url tts-chatterbox)"
TTS_INDICF5_URL="$(_resolve_url ytfactory-tts-indicf5)"
IMAGE_Z_IMAGE_TURBO_URL="$(_resolve_url ytfactory-image-z-image-turbo)"
ASR_URL="$(_resolve_url ytfactory-asr-whisper)"
for pair in "ytfactory-web-next:${WEB_NEXT_URL}" \
            "tts-chatterbox:${TTS_CHATTERBOX_URL}" \
            "ytfactory-tts-indicf5:${TTS_INDICF5_URL}" \
            "ytfactory-image-z-image-turbo:${IMAGE_Z_IMAGE_TURBO_URL}" \
            "ytfactory-asr-whisper:${ASR_URL}"; do
  name="${pair%%:*}"; url="${pair#*:}"
  if [ -z "${url}" ]; then
    echo "ERROR: could not resolve Cloud Run URL for ${name}" >&2
    exit 1
  fi
done

echo "==> Deploying ${SERVICE} to Cloud Run"
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="web-runner@${PROJECT}.iam.gserviceaccount.com" \
  --execution-environment=gen2 \
  --memory=2Gi --cpu=1 --cpu-boost \
  --concurrency=30 \
  --max-instances=2 \
  --min-instances=1 \
  --timeout=300 \
  --port=8080 \
  --allow-unauthenticated \
  --set-secrets="^|^AZURE_OPENAI_API_KEY=azure-openai-key:latest|YTFACTORY_CLIENT_SECRET=ytfactory-oauth-client:latest|YTFACTORY_WEB_OAUTH_CLIENT=ytfactory-web-oauth-client:latest|YTFACTORY_SESSION_SECRET=ytfactory-session-secret:latest|YTFACTORY_AGENT_TOKEN=ytfactory-agent-token:latest|YOUTUBE_API_KEY=youtube-api-key:latest|/secrets/youtube-channel-ids/value=youtube-channel-ids:latest" \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=${PROJECT}|YTFACTORY_BUCKET=${PROJECT}-artifacts|YTFACTORY_STATE_BUCKET=${PROJECT}-state|YTFACTORY_QUEUE_BACKEND=firestore|YTFACTORY_SIM_WORKER=0|YTFACTORY_RENDER_BACKEND=cloudrun|YTFACTORY_CLOUDRUN_JOB=ytfactory-render-worker-v2|YTFACTORY_CLOUDRUN_REGION=${REGION}|YTFACTORY_COOKIE_SECURE=1|YTFACTORY_ADMIN_DOMAINS=docx.co.in|YTFACTORY_ADMIN_EMAILS=sanimated219@gmail.com|YTFACTORY_PUBLIC_FRONTEND_URL=${WEB_NEXT_URL}|YTFACTORY_AUTH_REDIRECT_URI=${WEB_NEXT_URL}/api/auth/google/callback|CLOUDRUN_TTS_CHATTERBOX_URL=${TTS_CHATTERBOX_URL}|CLOUDRUN_TTS_INDICF5_URL=${TTS_INDICF5_URL}|CLOUDRUN_IMAGE_Z_IMAGE_TURBO_URL=${IMAGE_Z_IMAGE_TURBO_URL}|CLOUDRUN_ASR_URL=${ASR_URL}|AZURE_OPENAI_ENDPOINT=https://testshoffer.openai.azure.com|AZURE_OPENAI_API_VERSION=2025-04-01-preview|AZURE_OPENAI_MODEL=gpt-5.3-chat|AZURE_OPENAI_TOKEN_PARAM=max_completion_tokens"
# AZURE_OPENAI_ENDPOINT/API_VERSION/MODEL are non-secret triplet
# REQUIRED for chat_service.py and niche_specs_routes.py to talk to
# Azure (control/chat_service.py:96-105 + control/routes/niche_specs_routes.py:148-151
# both `return None` if AZURE_OPENAI_ENDPOINT is empty → chat falls back
# to "AI not configured" + niche-draft falls back to deterministic stub).
# Pre-2026-05-10 deploys had only AZURE_OPENAI_API_KEY (the secret),
# leaving the chat assistant + niche-draft auto-generate silently dead.
# Full post-mortem: docs/azure_openai_deploy_env.md.
#
# AZURE_OPENAI_TOKEN_PARAM=max_completion_tokens (added 2026-05-14):
# gpt-5.x / o1 / o3 reasoning deployments require this key and reject
# `max_tokens` outright (Azure 400). Without this env, every fresh web
# container instance burns one wasted Azure call to discover at runtime
# (TEL-OTEL-03 = 17 hits in 14d). Setting it skips the discovery.
# See docs/llm_max_tokens.md and pipeline/llm/cli.py:_azure_token_param.

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format="value(status.url)")
echo ""
echo "==> Deployed: ${URL}"
echo "    Open: ${URL}/renders"
