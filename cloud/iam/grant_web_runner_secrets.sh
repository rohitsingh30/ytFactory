#!/usr/bin/env bash
# ============================================================================
# Grant `roles/secretmanager.secretAccessor` to web-runner@ on the 7 secrets
# that ytfactory-web mounts at runtime. Idempotent — safe to re-run.
#
# WHY: post-S1.21 (per-service runtime SAs) `web-runner@` was created
# 2026-05-13 but the per-secret accessor bindings were left ⚠️ pending in
# `docs/iam_per_service.md`. `bash cloud/web-server/deploy.sh` then fails
# at the `gcloud run deploy` step (after a successful build) with:
#
#   ERROR: ... Permission denied on secret:
#   projects/.../secrets/azure-openai-key/versions/latest for Revision
#   service account web-runner@.... The service account used must be
#   granted the 'Secret Manager Secret Accessor' role
#   (roles/secretmanager.secretAccessor) at the secret, project or
#   higher level.
#
# (...repeated 7 times, once per secret.)
#
# This script is the canonical idempotent fix. Run BEFORE the first
# deploy on a fresh project, OR whenever a redeploy fails with the
# "Permission denied on secret" error above for `web-runner@`.
#
# Required env (defaults match prod):
#   GCP_PROJECT       ytfactory-prod-v2
#   WEB_RUNTIME_SA    web-runner@${GCP_PROJECT}.iam.gserviceaccount.com
#
# Usage:
#   bash cloud/iam/grant_web_runner_secrets.sh
#   bash cloud/iam/grant_web_runner_secrets.sh --dry-run
#
# Verify after:
#   gcloud secrets get-iam-policy azure-openai-key \
#     --project ytfactory-prod-v2
# ============================================================================

set -euo pipefail

PROJECT="${GCP_PROJECT:-ytfactory-prod-v2}"
WEB_RUNTIME_SA="${WEB_RUNTIME_SA:-web-runner@${PROJECT}.iam.gserviceaccount.com}"
DRY_RUN="${1:-}"

# The exact 7 secrets `cloud/web-server/deploy.sh::--set-secrets` mounts.
# Keep this list in sync — when a new secret is added there, add it here.
SECRETS=(
  azure-openai-key             # AZURE_OPENAI_API_KEY  → chat assistant + niche-draft
  ytfactory-oauth-client       # YTFACTORY_CLIENT_SECRET → per-channel YouTube OAuth
  ytfactory-web-oauth-client   # YTFACTORY_WEB_OAUTH_CLIENT → browser sign-in flow
  ytfactory-session-secret     # YTFACTORY_SESSION_SECRET → yt_session cookie HMAC
  ytfactory-agent-token        # YTFACTORY_AGENT_TOKEN → M2M bearer (scheduler, agent)
  youtube-api-key              # YOUTUBE_API_KEY → dashboard cards (Data API v3)
  youtube-channel-ids          # mounted at /secrets/youtube-channel-ids/value
)

echo "==> Project:       ${PROJECT}"
echo "==> Runtime SA:    ${WEB_RUNTIME_SA}"
echo "==> Role:          roles/secretmanager.secretAccessor"
echo "==> Secrets:       ${#SECRETS[@]} mounted by cloud/web-server/deploy.sh"
echo

for secret in "${SECRETS[@]}"; do
  cmd=(
    gcloud secrets add-iam-policy-binding "${secret}"
      --project="${PROJECT}"
      --member="serviceAccount:${WEB_RUNTIME_SA}"
      --role="roles/secretmanager.secretAccessor"
      --condition=None
      --quiet
  )
  if [[ "${DRY_RUN}" == "--dry-run" ]]; then
    printf '%s\n' "${cmd[*]}"
  else
    echo "==> ${secret}"
    if ! "${cmd[@]}" >/dev/null; then
      # Don't abort the loop — log and continue so a missing secret
      # (e.g. mid-bootstrap state) doesn't block the others.
      echo "    SKIP: secret ${secret} not found OR binding failed"
    fi
  fi
done

if [[ "${DRY_RUN}" != "--dry-run" ]]; then
  echo
  echo "==> Done. Verify with:"
  echo "    gcloud secrets get-iam-policy azure-openai-key \\"
  echo "      --project ${PROJECT} --format='value(bindings.role)'"
  echo
  echo "==> Then redeploy:"
  echo "    bash cloud/web-server/deploy.sh"
fi
