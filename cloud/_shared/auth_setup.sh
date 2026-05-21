# Source this file from any cloud/<service>/deploy.sh BEFORE the first
# `gcloud …` invocation. Sets CLOUDSDK_AUTH_ACCESS_TOKEN so deploys stop
# dying mid-flow with:
#
#   ERROR: (gcloud.builds.submit) There was a problem refreshing your
#   current auth tokens: Reauthentication failed. cannot prompt during
#   non-interactive execution.
#
# Token-mint cascade (first one that succeeds wins):
#
#   1. SA impersonation (PREFERRED, expiry-resistant).
#      Runs `gcloud auth print-access-token --impersonate-service-account=<sa>`
#      against ${YTFACTORY_DEPLOY_SA:-ytfactory-deployer@<project>.iam.gserviceaccount.com}.
#      The user-account creds need roles/iam.serviceAccountTokenCreator
#      on the SA (one-time grant; see docs/deploy.md). Once granted, every
#      deploy mints a SA access token via STS — bypasses the org's
#      user-account reauth policy entirely (until the user-account ADC
#      itself goes stale, typically 8-12h on enterprise Workspaces).
#
#   2. Plain ADC token. Falls back to
#      `gcloud auth application-default print-access-token`. Used when
#      impersonation is unconfigured (e.g. fresh laptop, SA not yet
#      created) OR when the SA grant hasn't propagated yet.
#
#   3. Hard error. If both fail, prints a single clear instruction:
#      `gcloud auth application-default login` — then exits non-zero so
#      the user gets ONE prompt instead of a noisy mid-flow re-auth
#      failure halfway through a 5-minute Cloud Build push.
#
# Caller override:
#
#   - CLOUDSDK_AUTH_ACCESS_TOKEN already set → no-op (caller's choice wins).
#   - YTFACTORY_DEPLOY_SA empty → skip impersonation, go straight to ADC
#     fallback (useful for one-off deploys against a different project).
#   - YTFACTORY_DEPLOY_SA_PROJECT overrides the SA project (defaults to
#     GCP_PROJECT, which deploy.sh sets via its own default chain).
#
# Why impersonation, not a SA key file: the org has
# `constraints/iam.disableServiceAccountKeyCreation` enforced (Workspace
# security baseline) — we can't create a downloadable JSON key. Impersonation
# is the policy-compliant alternative; the user authenticates ONCE per ADC
# refresh window (~8-12h) and every gcloud invocation in between routes
# through the SA's identity.
#
# Why this exists: see
# ~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_gcloud_reauth_use_adc_bypass.md
# (2026-05-12 user correction) +
# feedback_deploy_sa_impersonation.md (2026-05-13 follow-up — the SA-key
# blocker + impersonation workaround). The SHELL surface fix lives here;
# the Python equivalent for in-process gcloud invocations is
# pipeline/cloudrun_auth.py (mints ID tokens via google-auth directly).

if [[ -n "${CLOUDSDK_AUTH_ACCESS_TOKEN:-}" ]]; then
  # Caller already provided one — respect it.
  return 0 2>/dev/null || exit 0
fi

# Audit D3.39 — if this file is sourced from an interactive shell by
# accident (e.g. `source cloud/_shared/auth_setup.sh` from a terminal
# rather than from a deploy.sh subprocess), we'd dump a short-lived
# (~1 hr) access token into the user's interactive env, breaking
# every subsequent `gcloud auth login` / `gcloud auth list` /
# automatic refresh in that shell. Detect interactive mode (PS1 set
# OR shell launched with -i) and abort with a clear message.
if [[ $- == *i* ]] || [[ -n "${PS1:-}" ]]; then
  echo "==> ERROR: cloud/_shared/auth_setup.sh sourced from an" >&2
  echo "    INTERACTIVE shell. This file is meant to be sourced from" >&2
  echo "    a deploy.sh subprocess — sourcing it interactively dumps a" >&2
  echo "    short-lived access token into your shell environment that" >&2
  echo "    will break gcloud refresh until you 'unset CLOUDSDK_AUTH_ACCESS_TOKEN'." >&2
  echo "    If you really want to do this, set CLOUDSDK_AUTH_ACCESS_TOKEN_INTERACTIVE_OVERRIDE=1" >&2
  echo "    and re-source." >&2
  if [[ "${CLOUDSDK_AUTH_ACCESS_TOKEN_INTERACTIVE_OVERRIDE:-}" != "1" ]]; then
    return 1 2>/dev/null || exit 1
  fi
fi

# Audit 2026-05-16 — opt-out for the cost-optimization re-bootstrap.
# Setting CLOUDSDK_AUTH_ACCESS_TOKEN strips the ADC quota_project_id
# context, which Cloud Build needs to resolve the source-upload bucket
# `<project>_cloudbuild`. On a fresh project (where impersonation may
# not have propagated yet) this manifests as:
#   "The user is forbidden from accessing the bucket [<project>_cloudbuild]"
# Set YTFACTORY_SKIP_AUTH_SETUP=1 to skip the token mint and let gcloud
# use plain ADC with its quota_project_id intact.
if [[ "${YTFACTORY_SKIP_AUTH_SETUP:-}" == "1" ]]; then
  echo "==> auth: skipped (YTFACTORY_SKIP_AUTH_SETUP=1; using plain ADC)" >&2
  return 0 2>/dev/null || exit 0
fi

# Resolve SA email for impersonation. Default project comes from the caller's
# GCP_PROJECT (every cloud/<svc>/deploy.sh sets that). Caller can override
# with YTFACTORY_DEPLOY_SA / YTFACTORY_DEPLOY_SA_PROJECT.
_sa_project="${YTFACTORY_DEPLOY_SA_PROJECT:-${GCP_PROJECT:-ytfactory-prod-v2}}"
_sa_email="${YTFACTORY_DEPLOY_SA:-ytfactory-deployer@${_sa_project}.iam.gserviceaccount.com}"

# --- Tier 1: SA impersonation via IAM Credentials REST ---
#
# Mint a SA access token directly via the IAM Credentials API instead of
# `gcloud auth print-access-token --impersonate-service-account=...`. The
# gcloud variant bootstraps from user-account creds (NOT ADC), so it
# fails the moment the user-account access token hits the org's reauth
# wall — defeating the entire point of this script. The REST call
# bootstraps from ADC (passed in the Authorization header), which has
# the same long-lived refresh-token resilience that drove this whole
# fix in the first place.
#
# Both `curl` and `python3` are present on every macOS / Linux dev
# machine we deploy from; no extra deps. Token is parsed inline with
# python3 (`jq` not assumed).
_imp_token=""
if [[ -n "${_sa_email}" ]]; then
  _adc_for_imp="$(gcloud auth application-default print-access-token 2>/dev/null || true)"
  if [[ -n "${_adc_for_imp}" ]]; then
    _imp_token="$(curl -sS -X POST \
        -H "Authorization: Bearer ${_adc_for_imp}" \
        -H "Content-Type: application/json" \
        --data '{"scope":["https://www.googleapis.com/auth/cloud-platform"],"lifetime":"3600s"}' \
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/${_sa_email}:generateAccessToken" \
        2>/dev/null \
      | python3 -c "import json,sys
try:
    d = json.load(sys.stdin)
    t = d.get('accessToken', '')
    if t and not d.get('error'):
        print(t)
except Exception:
    pass
" 2>/dev/null || true)"
  fi
  unset _adc_for_imp
fi
if [[ -n "${_imp_token}" ]]; then
  export CLOUDSDK_AUTH_ACCESS_TOKEN="${_imp_token}"
  unset _imp_token _adc_token _sa_project _sa_email
  echo "==> auth: impersonating SA (bypasses user-account reauth policy)"
  return 0 2>/dev/null || exit 0
fi

# --- Tier 2: plain ADC token ---
_adc_token="$(gcloud auth application-default print-access-token 2>/dev/null || true)"
if [[ -n "${_adc_token}" ]]; then
  export CLOUDSDK_AUTH_ACCESS_TOKEN="${_adc_token}"
  echo "==> auth: using ADC access token (impersonation unavailable — fallback)" >&2
  echo "    To enable impersonation (recommended, fewer reauth prompts):" >&2
  echo "      gcloud iam service-accounts add-iam-policy-binding \\" >&2
  echo "        ${_sa_email} \\" >&2
  echo "        --member=user:\$(gcloud config get-value account) \\" >&2
  echo "        --role=roles/iam.serviceAccountTokenCreator \\" >&2
  echo "        --project=${_sa_project}" >&2
  unset _imp_token _adc_token _sa_project _sa_email
  return 0 2>/dev/null || exit 0
fi

# --- Tier 3: both failed → loud error ---
echo "==> ERROR: no Application Default Credentials available." >&2
echo "    Run ONCE per day to refresh BOTH gcloud + ADC creds:" >&2
echo "      gcloud auth application-default login" >&2
echo "    Then re-run this deploy. The deployer SA impersonation in tier 1" >&2
echo "    needs SOME user-account ADC to bootstrap from; it won't work if" >&2
echo "    the laptop has zero gcloud creds at all." >&2
unset _imp_token _adc_token _sa_project _sa_email
exit 1
