# Source this file from any cloud/<service>/deploy.sh BEFORE the first
# `gcloud …` invocation. It transparently sets CLOUDSDK_AUTH_ACCESS_TOKEN
# from Application Default Credentials (which survive the org's reauth
# policy hours-to-days longer than user-account creds), so deploys
# stop dying mid-flow with:
#
#   ERROR: (gcloud.builds.submit) There was a problem refreshing your
#   current auth tokens: Reauthentication failed. cannot prompt during
#   non-interactive execution.
#
# whenever the user-account access token has expired but their fresh
# `gcloud auth application-default login` from earlier in the day is
# still good.
#
# Usage (at the top of every cloud/<svc>/deploy.sh):
#
#   set -euo pipefail
#   SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
#   source "${SCRIPT_DIR}/../_shared/auth_setup.sh"
#   # ... rest of the script ...
#
# Behaviour matrix:
#   - CLOUDSDK_AUTH_ACCESS_TOKEN already set → no-op (caller's choice wins).
#   - ADC token mints in <2s → exported, gcloud uses it verbatim.
#   - ADC token also fails → loud message + non-zero exit so the user
#     gets ONE clear "run gcloud auth application-default login" prompt
#     instead of a noisy mid-flow re-auth failure halfway through a
#     5-minute Cloud Build push.
#
# Why this exists: see
# ~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_gcloud_reauth_use_adc_bypass.md
# (2026-05-12 user correction). The SHELL surface fix lives here; the
# Python equivalent for in-process gcloud invocations is
# pipeline/cloudrun_auth.py (mints ID tokens via google-auth directly).

if [[ -n "${CLOUDSDK_AUTH_ACCESS_TOKEN:-}" ]]; then
  # Caller already provided one — respect it.
  return 0 2>/dev/null || exit 0
fi

# Try to mint an ADC token. Suppress noisy stderr; we surface our own
# message on failure.
_adc_token="$(gcloud auth application-default print-access-token 2>/dev/null || true)"
if [[ -z "${_adc_token}" ]]; then
  echo "==> ERROR: no Application Default Credentials available." >&2
  echo "    Run ONCE per day to refresh both gcloud + ADC creds:" >&2
  echo "      gcloud auth application-default login" >&2
  echo "    Then re-run this deploy. No need to also run \`gcloud auth login\` —" >&2
  echo "    the ADC bypass below covers every gcloud command this script invokes." >&2
  unset _adc_token
  exit 1
fi
export CLOUDSDK_AUTH_ACCESS_TOKEN="${_adc_token}"
unset _adc_token

# Brief one-liner so the user knows the bypass kicked in. Verbose output
# would just be noise; if you want to debug, set CLOUDSDK_CORE_VERBOSITY=debug.
echo "==> auth: using ADC access token (bypasses user-account reauth policy)"
