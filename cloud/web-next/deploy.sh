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

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
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
export NEXT_PUBLIC_FIREBASE_API_KEY="${NEXT_PUBLIC_FIREBASE_API_KEY:-AIzaSyCF7aODvy0_ZsY9GTuucfKPq-6MyCmz9YU}"
export NEXT_PUBLIC_FIREBASE_PROJECT_ID="${NEXT_PUBLIC_FIREBASE_PROJECT_ID:-ytfactory-prod-v2}"
export NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN="${NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN:-ytfactory-prod-v2.firebaseapp.com}"
(cd web-next && npm run build >/dev/null)

# Sanity check: the home page prerender must include <!DOCTYPE html>.
if ! head -c 64 web-next/.next/server/app/index.html | grep -q "<!DOCTYPE html>"; then
    echo "ERROR: Local .next/server/app/index.html is missing <!DOCTYPE html>."
    echo "       Refusing to deploy a broken build."
    exit 1
fi

echo "==> Building + pushing ${IMAGE}"
gcloud builds submit . \
  --config=cloud/web-next/cloudbuild.yaml \
  --substitutions="_IMAGE=${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=1800s

echo "==> Deploying ${SERVICE} to Cloud Run"
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --execution-environment=gen2 \
  --memory=512Mi --cpu=1 --cpu-boost \
  --concurrency=80 \
  --max-instances=3 \
  --min-instances=1 \
  --timeout=60 \
  --port=8080 \
  --allow-unauthenticated \
  --set-env-vars="^|^YTFACTORY_API_BASE=${API_BASE}|NEXT_TELEMETRY_DISABLED=1|YT_AUTH_ENABLED=1"

URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" --format='value(status.url)')
echo ""
echo "==> Deployed: ${URL}"
echo "    Open: ${URL}"
echo ""
echo "    Proxying /api/* + /agent/* + /healthz to: ${API_BASE}"
