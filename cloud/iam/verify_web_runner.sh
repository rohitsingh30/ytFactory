#!/usr/bin/env bash
# ============================================================================
# READ-ONLY preflight verifier for the `web-runner@` runtime SA.
#
# Companion to `cloud/iam/grant_web_runner.sh` (which mutates IAM).
# This script never changes anything — it just checks every binding
# the doc + grant script say should exist, and exits non-zero with a
# concrete fix command if any are missing.
#
# Wired into `cloud/web-server/deploy.sh` and
# `cloud/clone-video-worker/deploy.sh` as a preflight step BEFORE the
# `gcloud run deploy`. The deploy aborts if web-runner is missing
# any role — preventing a repeat of the 2026-05-12 → 2026-05-13
# silent-13h-window where the SA flip succeeded at deploy time but
# the OAuth callback 500'd on first user request.
#
# Why verify-by-default rather than auto-grant: the operator running
# `deploy.sh` may not have project-IAM-admin permissions (deploys are
# happy with `roles/run.developer` + `roles/iam.serviceAccountUser`).
# Loud failure with the exact one-liner is a much better DX than a
# silent permission-denied during auto-grant.
#
# USAGE
# -----
#   bash cloud/iam/verify_web_runner.sh
#   bash cloud/iam/verify_web_runner.sh --quiet   # only print on failure
#
# Exit codes:
#   0 = all required bindings present (or skipped because gcloud is
#       unavailable on this host — e.g. CI runner without auth).
#   1 = one or more bindings missing. Stdout lists them + the fix.
#   2 = unexpected env / arg error.
#
# Required env (defaults match prod):
#   GCP_PROJECT       ytfactory-prod-v3
#   WEB_RUNTIME_SA    web-runner@${GCP_PROJECT}.iam.gserviceaccount.com
#
# Memory: feedback_web_runner_iam_silent_post_deploy_500.md
# Doc:    docs/iam_per_service.md § "Roles per SA → web-runner"
# ============================================================================

set -euo pipefail

PROJECT="${GCP_PROJECT:-ytfactory-prod-v3}"
WEB_RUNTIME_SA="${WEB_RUNTIME_SA:-web-runner@${PROJECT}.iam.gserviceaccount.com}"
MEMBER="serviceAccount:${WEB_RUNTIME_SA}"

QUIET="${1:-}"
if [[ -n "${QUIET}" && "${QUIET}" != "--quiet" ]]; then
  echo "ERROR: unknown argument: ${QUIET}" >&2
  echo "usage: $(basename "$0") [--quiet]" >&2
  exit 2
fi

# If gcloud isn't installed (CI / dev container without it), exit 0 —
# the preflight is best-effort. The grant script will fail loudly when
# a real deploy is attempted from a host that DOES have gcloud.
if ! command -v gcloud >/dev/null 2>&1; then
  [[ "${QUIET}" == "--quiet" ]] || echo "==> gcloud not installed; skipping IAM verification."
  exit 0
fi

# Try to source the ADC bypass. If it fails (no ADC creds), continue
# anyway and let individual gcloud calls error — verifier should never
# hard-block a deploy on a transient auth glitch.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/../_shared/auth_setup.sh" 2>/dev/null || true

# ----------------------------------------------------------------------------
# Expected bindings — KEEP IN SYNC with cloud/iam/grant_web_runner.sh.
# The regression test asserts both files agree.
# ----------------------------------------------------------------------------

PROJECT_ROLES=(
  roles/datastore.user
  roles/run.invoker
  roles/run.developer
  roles/monitoring.metricWriter
)

SELF_BINDING_ROLES=(
  roles/iam.serviceAccountTokenCreator
)

declare -a BUCKET_BINDINGS=(
  "${PROJECT}-state|roles/storage.objectAdmin"
  "${PROJECT}-artifacts|roles/storage.objectAdmin"
)

# Minimum-set per-secret accessor probe — checking every secret would
# be slow + flaky (some accounts may be mid-bootstrap). Sample three
# critical ones; if these are missing, run the full grant.
ACCESSOR_SECRETS_PROBE=(
  azure-openai-key
  ytfactory-web-oauth-client
  ytfactory-session-secret
)

# Sample one writeback secret — same flakiness rationale. If missing,
# the full `bash cloud/iam/grant_web_runner.sh` re-applies
# roles/secretmanager.secretVersionAdder on every youtube-token-*.
# In v3 (2026-05-18) per-channel YouTube upload is intentionally skipped,
# so no youtube-token-* secrets exist; probe is empty and the loop no-ops.
WRITEBACK_SECRETS_PROBE=()

# ----------------------------------------------------------------------------
MISSING=()

_log() {
  [[ "${QUIET}" == "--quiet" ]] || echo "$@"
}

_check_project_role() {
  local role="$1"
  local found
  found=$(gcloud projects get-iam-policy "${PROJECT}" \
    --flatten='bindings[].members' \
    --filter="bindings.members=${MEMBER} AND bindings.role=${role}" \
    --format='value(bindings.role)' 2>/dev/null || true)
  if [[ -z "${found}" ]]; then
    MISSING+=("project:${role}")
    _log "  MISSING project ${role}"
  else
    _log "  OK      project ${role}"
  fi
}

_check_self_binding() {
  local role="$1"
  local found
  found=$(gcloud iam service-accounts get-iam-policy "${WEB_RUNTIME_SA}" \
    --project="${PROJECT}" \
    --flatten='bindings[].members' \
    --filter="bindings.members=${MEMBER} AND bindings.role=${role}" \
    --format='value(bindings.role)' 2>/dev/null || true)
  if [[ -z "${found}" ]]; then
    MISSING+=("sa-self:${role}")
    _log "  MISSING sa-self ${role}"
  else
    _log "  OK      sa-self ${role}"
  fi
}

_check_bucket_role() {
  local bucket="$1" role="$2"
  local found
  found=$(gcloud storage buckets get-iam-policy "gs://${bucket}" --format=json 2>/dev/null \
    | python3 -c "
import json, sys
try:
    p = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for b in p.get('bindings', []):
    if b.get('role') == '${role}' and '${MEMBER}' in b.get('members', []):
        print('yes')
        break
" 2>/dev/null || true)
  if [[ -z "${found}" ]]; then
    MISSING+=("bucket:${bucket}:${role}")
    _log "  MISSING bucket gs://${bucket} ${role}"
  else
    _log "  OK      bucket gs://${bucket} ${role}"
  fi
}

_check_secret_accessor() {
  local secret="$1"
  local found
  found=$(gcloud secrets get-iam-policy "${secret}" --project="${PROJECT}" \
    --flatten='bindings[].members' \
    --filter="bindings.members=${MEMBER} AND bindings.role=roles/secretmanager.secretAccessor" \
    --format='value(bindings.role)' 2>/dev/null || true)
  if [[ -z "${found}" ]]; then
    MISSING+=("secret:${secret}:roles/secretmanager.secretAccessor")
    _log "  MISSING secret ${secret} roles/secretmanager.secretAccessor"
  else
    _log "  OK      secret ${secret} roles/secretmanager.secretAccessor"
  fi
}

_check_secret_writeback() {
  local secret="$1"
  local found
  found=$(gcloud secrets get-iam-policy "${secret}" --project="${PROJECT}" \
    --flatten='bindings[].members' \
    --filter="bindings.members=${MEMBER} AND bindings.role=roles/secretmanager.secretVersionAdder" \
    --format='value(bindings.role)' 2>/dev/null || true)
  if [[ -z "${found}" ]]; then
    MISSING+=("secret:${secret}:roles/secretmanager.secretVersionAdder")
    _log "  MISSING secret ${secret} roles/secretmanager.secretVersionAdder"
  else
    _log "  OK      secret ${secret} roles/secretmanager.secretVersionAdder"
  fi
}

_log "==> Verifying ${WEB_RUNTIME_SA} bindings on ${PROJECT}"
for role in "${PROJECT_ROLES[@]}";       do _check_project_role  "${role}";  done
for role in "${SELF_BINDING_ROLES[@]}";  do _check_self_binding   "${role}";  done
for entry in "${BUCKET_BINDINGS[@]}";    do _check_bucket_role    "${entry%%|*}" "${entry#*|}"; done
for secret in "${ACCESSOR_SECRETS_PROBE[@]}"; do _check_secret_accessor "${secret}"; done
for secret in "${WRITEBACK_SECRETS_PROBE[@]+"${WRITEBACK_SECRETS_PROBE[@]}"}"; do _check_secret_writeback "${secret}"; done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  cat >&2 <<EOF

==> ERROR: ${#MISSING[@]} required IAM binding(s) missing on ${WEB_RUNTIME_SA}.
==> Missing:
$(printf '      - %s\n' "${MISSING[@]}")

==> To fix (idempotent — safe to re-run):
      bash cloud/iam/grant_web_runner.sh

==> Then re-run this deploy. See docs/iam_per_service.md and
==> ~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_web_runner_iam_silent_post_deploy_500.md
==> for the full why.
EOF
  exit 1
fi

_log "==> All required web-runner bindings present."
exit 0
