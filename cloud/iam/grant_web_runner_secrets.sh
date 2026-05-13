#!/usr/bin/env bash
# ============================================================================
# DEPRECATED — back-compat shim. Use cloud/iam/grant_web_runner.sh instead.
#
# 2026-05-13: this script used to grant ONLY roles/secretmanager.secretAccessor
# on 7 secrets. That partial coverage was the proximate cause of the 13-hour
# silent OAuth-callback outage on 2026-05-12 → 13:
#
#   - secretAccessor missing → deploy fails LOUDLY (caught + fixed)
#   - datastore.user / storage.objectAdmin / signBlob / run.invoker missing
#     → deploy SUCCEEDS, every Firestore/bucket/signed-URL/run.invoker
#     code path 500s on first user request.
#
# The canonical script now covers all 5 categories. This shim just calls
# into it so any old runbook / docs reference still works. Update your
# muscle memory: `bash cloud/iam/grant_web_runner.sh`.
#
# Memory: feedback_web_runner_iam_silent_post_deploy_500.md
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cat >&2 <<'EOF'
==> NOTE: grant_web_runner_secrets.sh is now a back-compat shim.
==>       Calling cloud/iam/grant_web_runner.sh — the canonical script
==>       that grants ALL roles web-runner needs (not just secrets).
==>
==>       Update your runbooks / muscle memory:
==>         bash cloud/iam/grant_web_runner.sh
EOF

exec "${SCRIPT_DIR}/grant_web_runner.sh" "$@"
