#!/usr/bin/env bash
# Build + deploy ytfactory-web-next (Next.js frontend) to Cloud Run.
#
# Architecture (2026-05-10):
#   - ytfactory-web      → FastAPI backend (web/server.py)
#   - ytfactory-web-next → Next.js frontend (this) — public face
# The Next.js service proxies /api/* + /agent/* + /healthz to the
# FastAPI backend via app/api/[...path]/route.ts and rewrites in
# next.config.mjs. YTFACTORY_API_BASE points at the FastAPI URL.
#
# IMPORTANT: this script pre-builds .next/ on the host (macOS) and ships
# the prebuilt artifacts. We do NOT run `next build` inside the
# container — see cloud/web-next/Dockerfile for the rationale.

set -euo pipefail

# Bake in the ADC-token auth bypass so deploys don't die mid-build with
# "Reauthentication failed" when the user-account access token has expired
# but ADC is still fresh. See cloud/_shared/auth_setup.sh + the memory
# file feedback_gcloud_reauth_use_adc_bypass.md for the full why.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v3}"
REGION="${GCP_REGION:-asia-southeast1}"
REPO="ytfactory-tts"
SERVICE="ytfactory-web-next"
TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# Discover the FastAPI backend URL so /api/* proxies land correctly.
API_BASE=$(gcloud run services describe ytfactory-web \
    --project="${PROJECT}" --region="${REGION}" \
    --format='value(status.url)' 2>/dev/null || true)

if [[ -z "${API_BASE}" ]]; then
    echo "ERROR: ytfactory-web (FastAPI backend) not deployed."
    echo "       Run cloud/web-server/deploy.sh first."
    exit 1
fi

# Discover the canonical host (the one Google's OAuth client has
# registered as redirect_uri). Cloud Run hands every service two URLs
# (project-id-hash form + project-number form); we must pin all
# requests to ONE of them so cookies + the OAuth callback line up.
# `status.url` returns the project-id-hash form, which is what's
# registered with Google.
#
# First-deploy bootstrap: if the service does not yet exist, the
# describe call returns empty and we deploy without the env var. The
# very next deploy will pick the URL up and lock the host. (Google
# OAuth registration happens manually after the first deploy anyway.)
CANONICAL_HOST=$(gcloud run services describe "${SERVICE}" \
    --project="${PROJECT}" --region="${REGION}" \
    --format='value(status.url)' 2>/dev/null \
    | sed -E 's|^https?://||' \
    || true)

CANONICAL_ENV=""
if [[ -n "${CANONICAL_HOST}" ]]; then
    echo "==> Canonical host (will pin all requests via middleware): ${CANONICAL_HOST}"
    CANONICAL_ENV="|YTFACTORY_CANONICAL_HOST=${CANONICAL_HOST}"
else
    echo "==> WARNING: ${SERVICE} not yet deployed; skipping YTFACTORY_CANONICAL_HOST."
    echo "             Re-run this script once for the env to populate."
fi

# ---- 1. Pre-build .next/ on the host (macOS) ---------------------------
# Linux builds of this app produce broken HTML (missing doctype/html/body
# opening tags). The host build is the source of truth.
echo "==> Pre-building .next/ on host"
# Firebase config — public values, baked into the JS bundle so the
# critique-chat panel can sign in via Firebase Auth + subscribe to
# Firestore directly. Public Firebase apiKeys are NOT secrets; real
# auth runs via the custom token minted by /api/jobs/<id>/critique/token
# on the FastAPI backend + Firestore security rules. Provisioned
# 2026-05-11 via `firebase apps:create WEB ytfactory-web-next` after
# `firebase projects:addfirebase ytfactory-prod-v2`. Override via env
# if you ever rotate the keys.
export NEXT_PUBLIC_FIREBASE_API_KEY="${NEXT_PUBLIC_FIREBASE_API_KEY:-PLACEHOLDER}"
export NEXT_PUBLIC_FIREBASE_PROJECT_ID="${NEXT_PUBLIC_FIREBASE_PROJECT_ID:-ytfactory-prod-v3}"
export NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN="${NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN:-ytfactory-prod-v3.firebaseapp.com}"
(cd web-next && npm run build >/dev/null)

# Sanity check: the home page prerender must include <!DOCTYPE html>.
if ! head -c 64 web-next/.next/server/app/index.html | grep -q "<!DOCTYPE html>"; then
    echo "ERROR: Local .next/server/app/index.html is missing <!DOCTYPE html>."
    echo "       Refusing to deploy a broken build."
    exit 1
fi

echo "==> Building + pushing ${IMAGE}"
# --region pins build to asia-southeast1 (2026-05-17 cost-audit rule 3).
gcloud builds submit . \
  --region="${REGION}" \
  --config=cloud/web-next/cloudbuild.yaml \
  --substitutions="_IMAGE=${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=1800s

echo "==> Deploying ${SERVICE} to Cloud Run"
# Audit S1.22 — pin to the dedicated web-next SA so a compromised
# Next.js container can't inherit the default Compute Engine SA's
# broad project privileges. Operator: create the SA with
#   gcloud iam service-accounts create web-next-runner --project="${PROJECT}"
# and grant it ONLY what the Next.js server actually needs (run.invoker
# on the upstream control-plane API service for the /api/* proxy).
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --service-account="web-next-runner@${PROJECT}.iam.gserviceaccount.com" \
  --execution-environment=gen2 \
  --memory=512Mi --cpu=1 --cpu-boost \
  --concurrency=80 \
  --max-instances=3 \
  --min-instances=1 \
  --timeout=60 \
  --port=8080 \
  --allow-unauthenticated \
  --set-secrets="YTFACTORY_SESSION_SECRET=ytfactory-session-secret:latest" \
  --set-env-vars="^|^YTFACTORY_API_BASE=${API_BASE}|NEXT_TELEMETRY_DISABLED=1|YT_AUTH_ENABLED=1${CANONICAL_ENV}"

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format='value(status.url)')
echo ""
echo "==> Deployed: ${URL}"
echo "    Open: ${URL}"
echo ""
echo "    Proxying /api/* + /agent/* + /healthz to: ${API_BASE}"
