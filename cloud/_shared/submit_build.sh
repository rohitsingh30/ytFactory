#!/usr/bin/env bash
# Shared helper for "gcloud builds submit" that survives the VPC-SC log-
# streaming error.
#
# Background:
#   gcloud builds submit reads logs from the default GCS logs bucket. Our
#   project enforces a VPC-SC perimeter that blocks the default bucket,
#   so gcloud emits:
#     ERROR: (gcloud.builds.submit)
#     The build is running, and logs are being written to the default logs
#     bucket. This tool can only stream logs if you are Viewer/Owner of
#     the project and, if applicable, allowed by your VPC-SC security policy.
#   AND exits non-zero — even though the underlying Cloud Build is happily
#   building and pushing the image. Under `set -euo pipefail` this aborts
#   the deploy script before the `gcloud run deploy` step runs.
#
#   Caught us 3 times on 2026-05-15. Permanent fix:
#
# Usage:
#   source "$(cd "$(dirname "$0")" && pwd)/../_shared/submit_build.sh"
#   submit_build "${IMAGE}" "${PROJECT}" 5400
#   # On success, $BUILD_ID and $BUILD_STATUS are exported.
#
# What this does:
#   1. Runs `gcloud builds submit --async` so the submission returns
#      immediately with the build ID and we never hit the log streaming
#      path.
#   2. Polls `gcloud builds describe` for the actual build status until
#      it reaches a terminal state (SUCCESS / FAILURE / CANCELLED / TIMEOUT).
#   3. Returns non-zero only when the build *actually* failed, not when
#      log streaming flickered.

submit_build() {
  local image="$1"
  local project="$2"
  local timeout="${3:-5400}"
  local extra_args="${4:-}"  # e.g. "--machine-type=e2-highcpu-32"
  # CLOUDBUILD_REGION is the cost-audit guardrail (2026-05-17): pin the
  # build to the same region as Artifact Registry so image pushes don't
  # generate intercontinental egress. Defaults to GCP_REGION (which
  # every deploy.sh sets to asia-southeast1) and falls back to the
  # global pool only if both are unset (preserves old behavior for any
  # caller that didn't source GCP_REGION).
  local region="${CLOUDBUILD_REGION:-${GCP_REGION:-}}"

  if [ -z "$image" ] || [ -z "$project" ]; then
    echo "submit_build: usage: submit_build <image> <project> [timeout] [extra-gcloud-args]" >&2
    return 2
  fi

  local region_arg=""
  if [ -n "$region" ]; then
    region_arg="--region=${region}"
    echo "==> Submitting build (async, region=${region}) for ${image}"
  else
    echo "==> Submitting build (async, GLOBAL region — egress risk!) for ${image}"
  fi
  # --async returns immediately with the build ID; no log streaming.
  local out
  # --async with --format prints status lines to STDERR and the value to STDOUT.
  # Redirect stderr to /dev/null so we capture ONLY the build id.
  out=$(gcloud builds submit . \
    ${region_arg} \
    --tag="${image}" \
    --project="${project}" \
    --timeout="${timeout}s" \
    --async \
    ${extra_args} \
    --format="value(id)" 2>/dev/null)
  local rc=$?
  if [ $rc -ne 0 ]; then
    echo "submit_build: gcloud builds submit --async failed (rc=$rc)" >&2
    return $rc
  fi
  # Validate that we captured a UUID-shaped build id, not noise.
  BUILD_ID=$(echo "$out" | tr -d '[:space:]')
  if ! echo "$BUILD_ID" | grep -qE '^[a-f0-9-]{20,}$'; then
    echo "submit_build: bad BUILD_ID captured: $BUILD_ID" >&2
    return 1
  fi
  echo "==> Build ID: ${BUILD_ID}"
  if [ -n "$region" ]; then
    echo "==> Poll URL: https://console.cloud.google.com/cloud-build/builds/${BUILD_ID}?project=${project}&region=${region}"
  else
    echo "==> Poll URL: https://console.cloud.google.com/cloud-build/builds/${BUILD_ID}?project=${project}"
  fi

  # Poll until terminal state. Build timeout flag above caps the build's
  # max duration; we poll up to a hard wall of timeout+300s.
  local deadline=$((SECONDS + timeout + 300))
  local poll_interval=10
  while [ $SECONDS -lt $deadline ]; do
    local status
    status=$(gcloud builds describe "${BUILD_ID}" \
      ${region_arg} \
      --project="${project}" \
      --format="value(status)" 2>/dev/null || echo "?")
    case "$status" in
      SUCCESS)
        BUILD_STATUS="SUCCESS"
        echo "==> Build ${BUILD_ID} SUCCESS"
        return 0
        ;;
      FAILURE|INTERNAL_ERROR|TIMEOUT|CANCELLED|EXPIRED)
        BUILD_STATUS="$status"
        echo "==> Build ${BUILD_ID} ${status}" >&2
        return 1
        ;;
      QUEUED|WORKING|PENDING|"")
        # Still in progress; print a heartbeat every minute.
        if [ $((SECONDS % 60)) -lt $poll_interval ]; then
          echo "    ...status=${status:-pending} (${SECONDS}s elapsed)"
        fi
        ;;
      *)
        echo "    ...unknown status: ${status} — continuing to poll"
        ;;
    esac
    sleep $poll_interval
  done

  BUILD_STATUS="WALL_DEADLINE"
  echo "==> Build ${BUILD_ID} did not reach terminal state within wall deadline" >&2
  return 1
}

export -f submit_build
