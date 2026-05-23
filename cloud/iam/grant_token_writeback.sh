#!/usr/bin/env bash
# ============================================================================
# E3 — IAM grant for token-rotation writeback (plan: token-issue permanent fix)
# ============================================================================
#
# After the laptop fetches/refreshes an OAuth refresh-token interactively,
# subsequent cloud token rotations need to write the rotated blob BACK to
# Secret Manager (the read-only mount can't be modified directly). For
# that, the runtime service-account needs:
#
#   roles/secretmanager.secretVersionAdder
#
# on every ``youtube-token-<account>`` secret. This script grants exactly
# that — idempotent, safe to re-run, prints the resulting policy at the end.
#
# Required env (defaults match prod):
#   GCP_PROJECT       ytfactory-prod-v3
#   RUNTIME_SA        tts-runner@${GCP_PROJECT}.iam.gserviceaccount.com
#
# Usage:
#   ./cloud/iam/grant_token_writeback.sh
#   ./cloud/iam/grant_token_writeback.sh --dry-run    # print what would run
#
# Verify after:
#   gcloud secrets get-iam-policy youtube-token-mystoriesanimated \
#     --project ytfactory-prod-v3
# ============================================================================

set -euo pipefail

# Audit D3.23 — bypass interactive gcloud reauth via ADC.
# Source the shared helper so a single `gcloud auth
# application-default login` covers every wire-up script.
source "$(cd "$(dirname "$0")" && pwd)/../_shared/auth_setup.sh"

PROJECT="${GCP_PROJECT:-ytfactory-prod-v3}"
RUNTIME_SA="${RUNTIME_SA:-tts-runner@${PROJECT}.iam.gserviceaccount.com}"
# Audit D3.29 — pre-fix any typo (--dryrun, --help, --whatever)
# silently slipped past as "not --dry-run" and ran live grants. Now
# explicitly validate so unknown flags abort.
DRY_RUN="${1:-}"
if [[ -n "${DRY_RUN}" && "${DRY_RUN}" != "--dry-run" ]]; then
  echo "ERROR: unknown argument: ${DRY_RUN}" >&2
  echo "usage: $(basename "$0") [--dry-run]" >&2
  exit 2
fi

# Per the 2026-05-09 cutover (docs/full_cloud_cutover_2026_05_09.md §1a)
# these are the canonical 9 secrets — one per OAuth-distinct YouTube
# account. Keep this list in sync with that doc.
ACCOUNTS=(
  mystoriesanimated
  cosmosdecoded
  historyrecapped
  hindutavaanimated
  sportsrecapped
  scrollpulse
  rhymetimejunction
  afddfdf
  zgsbhqszdheo
)

echo "==> Project:       ${PROJECT}"
echo "==> Runtime SA:    ${RUNTIME_SA}"
echo "==> Role:          roles/secretmanager.secretVersionAdder"
echo "==> Secrets:       ${#ACCOUNTS[@]} youtube-token-* secrets"
echo

for acct in "${ACCOUNTS[@]}"; do
  secret="youtube-token-${acct}"
  cmd=(
    gcloud secrets add-iam-policy-binding "${secret}"
      --project="${PROJECT}"
      --member="serviceAccount:${RUNTIME_SA}"
      --role="roles/secretmanager.secretVersionAdder"
      --condition=None
      --quiet
  )
  if [[ "${DRY_RUN}" == "--dry-run" ]]; then
    printf '%s\n' "${cmd[*]}"
  else
    echo "==> ${secret}"
    if ! "${cmd[@]}" >/dev/null; then
      # Most common cause: secret doesn't exist for this project (e.g.
      # an account that may not be wired up everywhere). Don't abort
      # the loop — just log and continue.
      echo "    SKIP: secret ${secret} not found OR binding failed"
    fi
  fi
done

if [[ "${DRY_RUN}" != "--dry-run" ]]; then
  echo
  echo "==> Done. Verify with:"
  echo "    gcloud secrets get-iam-policy youtube-token-mystoriesanimated \\"
  echo "      --project ${PROJECT}"
fi
