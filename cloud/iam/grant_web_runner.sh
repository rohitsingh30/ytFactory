#!/usr/bin/env bash
# ============================================================================
# CANONICAL grant script for the `web-runner@` per-service runtime SA.
#
# Single source of truth — replaces the older, secrets-only
# `cloud/iam/grant_web_runner_secrets.sh` (which is now a thin
# back-compat shim that just calls into here). Keep
# `docs/iam_per_service.md § "Roles per SA → web-runner"` in sync with
# the role list at the bottom of this script — the regression test
# `tests/test_cloud_deploy_hardening.py::TestWebRunnerGrantsAreComplete`
# parses both files and asserts they line up.
#
# WHY THIS EXISTS
# ---------------
# 2026-05-12 21:00 UTC: audit S1.21 ("per-service Cloud Run runtime
# SAs") flipped `cloud/web-server/deploy.sh` from the legacy global
# `tts-runner@` SA (which was overprivileged with `datastore.user`,
# `storage.objectAdmin`, `run.invoker`, etc.) to a fresh `web-runner@`
# SA that only had the 3 telemetry roles + per-secret accessor binds.
#
# The deploy succeeded:
#   - Cloud Build: green
#   - Container boot: green (telemetry roles cover OTel exporters)
#   - /healthz: green (no Firestore in the probe)
#
# 13 hours later, the first user clicking "Sign in with Google" hit
# `upsert_user(...)` → Firestore `auth_users/<email>.set(...)` →
# `google.api_core.exceptions.PermissionDenied: 403` → FastAPI
# returned 500 → Next.js proxy showed "Internal Server Error" →
# user (correctly) said "why do we keep regressing?"
#
# Class-of-bug: deploy-time validation only catches roles needed at
# container boot. Roles needed at first-user-request — Firestore
# reads, signed-URL generation, run.invoker for the render JOB — go
# undetected until a real user exercises the path.
#
# The fix lives in three places:
#   1. THIS SCRIPT  — grants every role web-runner needs, idempotent.
#   2. cloud/iam/verify_web_runner.sh — preflight checker, fails LOUD
#      with the exact missing grants. Wired into deploy.sh.
#   3. tests/test_cloud_deploy_hardening.py::TestWebRunnerGrantsAreComplete
#      — parses docs + this script and asserts they're in sync.
#
# Memory: feedback_web_runner_iam_silent_post_deploy_500.md
# Doc:    docs/iam_per_service.md § "Roles per SA → web-runner"
#
# USAGE
# -----
#   bash cloud/iam/grant_web_runner.sh              # apply everything
#   bash cloud/iam/grant_web_runner.sh --dry-run    # print only
#
# Re-run safely after editing the role list. Skips bindings that
# already exist (gcloud add-iam-policy-binding is idempotent for
# project + bucket grants; per-secret grants exit 0 too).
#
# Required env (defaults match prod):
#   GCP_PROJECT       ytfactory-prod-v2
#   WEB_RUNTIME_SA    web-runner@${GCP_PROJECT}.iam.gserviceaccount.com
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../_shared/auth_setup.sh"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v3}"
WEB_RUNTIME_SA="${WEB_RUNTIME_SA:-web-runner@${PROJECT}.iam.gserviceaccount.com}"
MEMBER="serviceAccount:${WEB_RUNTIME_SA}"

DRY_RUN="${1:-}"
if [[ -n "${DRY_RUN}" && "${DRY_RUN}" != "--dry-run" ]]; then
  echo "ERROR: unknown argument: ${DRY_RUN}" >&2
  echo "usage: $(basename "$0") [--dry-run]" >&2
  exit 2
fi

# ----------------------------------------------------------------------------
# Role list — KEEP IN SYNC WITH docs/iam_per_service.md § "web-runner".
# The regression test parses both files and fails if they drift.
# ----------------------------------------------------------------------------

# (a) Project-level roles (one binding for the whole project).
PROJECT_ROLES=(
  roles/datastore.user                  # Firestore: auth_users, oauth_tokens, jobs/*, scheduler
  roles/run.invoker                     # invoke render-worker-v2 Cloud Run JOB + sibling cloud services
  roles/run.developer                   # run.jobs.runWithOverrides for SDK trigger_render_job() — invoker alone is NOT enough (2026-05-18 v3 e2e smoke caught this)
  roles/monitoring.metricWriter         # OTel exporter creates custom metric descriptors (workload.googleapis.com/http.server.*) on first request; without this, every request logs a 403 from monitoring.metricDescriptors.create
)

# (b) Self-binding (web-runner@ acts on itself, for IAM signBlob path
#     used by control/core/storage.py::_signed_url_via_iam_signblob).
SELF_BINDING_ROLES=(
  roles/iam.serviceAccountTokenCreator
)

# (c) Bucket-level roles (storage.objectAdmin scoped to specific buckets,
#     not project-wide — least-privilege, matches the rule from the
#     S1.21 motivation).
declare -a BUCKET_BINDINGS=(
  "${PROJECT}-state|roles/storage.objectAdmin"      # YTFACTORY_STATE_BUCKET
  "${PROJECT}-artifacts|roles/storage.objectAdmin"  # YTFACTORY_BUCKET (job artifacts, signed URLs)
)

# (d) Per-secret accessor (the 7 secrets cloud/web-server/deploy.sh
#     mounts via --set-secrets). Keep in sync with that script.
ACCESSOR_SECRETS=(
  azure-openai-key             # AZURE_OPENAI_API_KEY  → chat assistant + niche-draft
  ytfactory-oauth-client       # YTFACTORY_CLIENT_SECRET → per-channel YouTube OAuth
  ytfactory-web-oauth-client   # YTFACTORY_WEB_OAUTH_CLIENT → browser sign-in flow
  ytfactory-session-secret     # YTFACTORY_SESSION_SECRET → yt_session cookie HMAC
  ytfactory-agent-token        # YTFACTORY_AGENT_TOKEN → M2M bearer (scheduler, agent)
  youtube-api-key              # YOUTUBE_API_KEY → dashboard cards (Data API v3)
  youtube-channel-ids          # mounted at /secrets/youtube-channel-ids/value
)

# (e) Per-secret writeback (rotated OAuth refresh tokens). One per
#     youtube-token-<account> secret. Reuses the canonical account
#     list from cloud/iam/grant_token_writeback.sh — see ACCOUNTS array
#     there. Keep this list in sync with that one.
WRITEBACK_ACCOUNTS=(
  # v3 (2026-05-18) — per-channel YouTube upload intentionally skipped.
  # No youtube-token-<channel> secrets exist; loop no-ops.
  # Re-populate this list once upload is enabled per channel.
)

# ----------------------------------------------------------------------------
# Apply
# ----------------------------------------------------------------------------

echo "==> Project:    ${PROJECT}"
echo "==> Runtime SA: ${WEB_RUNTIME_SA}"
echo "==> Roles:"
echo "    (a) project:     ${#PROJECT_ROLES[@]}"
echo "    (b) self:        ${#SELF_BINDING_ROLES[@]}"
echo "    (c) bucket:      ${#BUCKET_BINDINGS[@]}"
echo "    (d) sec-accessor:${#ACCESSOR_SECRETS[@]}"
echo "    (e) sec-version: ${#WRITEBACK_ACCOUNTS[@]}"
echo

_run() {
  if [[ "${DRY_RUN}" == "--dry-run" ]]; then
    printf '  %s\n' "$*"
    return 0
  fi
  if ! "$@" >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

# (a) project
for role in "${PROJECT_ROLES[@]}"; do
  echo "==> project ${PROJECT} <- ${role}"
  if ! _run gcloud projects add-iam-policy-binding "${PROJECT}" \
       --member="${MEMBER}" --role="${role}" --condition=None --quiet; then
    echo "    grant FAILED — check you have roles/resourcemanager.projectIamAdmin or roles/iam.securityAdmin on ${PROJECT}." >&2
    exit 1
  fi
done

# (b) self
for role in "${SELF_BINDING_ROLES[@]}"; do
  echo "==> sa-self ${WEB_RUNTIME_SA} <- ${role}"
  if ! _run gcloud iam service-accounts add-iam-policy-binding "${WEB_RUNTIME_SA}" \
       --project="${PROJECT}" --member="${MEMBER}" --role="${role}" --condition=None --quiet; then
    echo "    grant FAILED — check you have roles/iam.serviceAccountAdmin on ${WEB_RUNTIME_SA}." >&2
    exit 1
  fi
done

# (c) bucket
for entry in "${BUCKET_BINDINGS[@]}"; do
  bucket="${entry%%|*}"
  role="${entry#*|}"
  echo "==> bucket gs://${bucket} <- ${role}"
  if ! _run gcloud storage buckets add-iam-policy-binding "gs://${bucket}" \
       --member="${MEMBER}" --role="${role}" --condition=None --quiet; then
    echo "    grant FAILED — bucket missing OR caller lacks roles/storage.admin." >&2
    exit 1
  fi
done

# (d) per-secret accessor
for secret in "${ACCESSOR_SECRETS[@]}"; do
  echo "==> secret ${secret} <- roles/secretmanager.secretAccessor"
  if ! _run gcloud secrets add-iam-policy-binding "${secret}" \
       --project="${PROJECT}" --member="${MEMBER}" \
       --role="roles/secretmanager.secretAccessor" --condition=None --quiet; then
    # Don't abort: a missing secret (mid-bootstrap state) shouldn't block
    # the others. Loud SKIP so the operator can investigate.
    echo "    SKIP: secret ${secret} not found OR binding failed"
  fi
done

# (e) per-secret writeback
for acct in "${WRITEBACK_ACCOUNTS[@]+"${WRITEBACK_ACCOUNTS[@]}"}"; do
  secret="youtube-token-${acct}"
  echo "==> secret ${secret} <- roles/secretmanager.secretVersionAdder"
  if ! _run gcloud secrets add-iam-policy-binding "${secret}" \
       --project="${PROJECT}" --member="${MEMBER}" \
       --role="roles/secretmanager.secretVersionAdder" --condition=None --quiet; then
    # Same as above — some accounts may not have a secret yet (e.g.
    # afddfdf / zgsbhqszdheo on certain envs).
    echo "    SKIP: secret ${secret} not found OR binding failed"
  fi
done

if [[ "${DRY_RUN}" != "--dry-run" ]]; then
  echo
  echo "==> Done. Verify with:"
  echo "    bash cloud/iam/verify_web_runner.sh"
fi
