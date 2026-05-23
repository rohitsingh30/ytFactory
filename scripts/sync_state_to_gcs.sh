#!/usr/bin/env bash
# Mirror local channel state to GCS so the cloud render-worker can read it.
#
# Triggered by ~/Library/LaunchAgents/com.ytfactory.state-sync.plist on
# WatchPaths events when any narration/cast/shotlist/upload/_holds.json
# under a tracked channel changes.
#
# One-way: laptop → GCS. Cloud writes (e.g. uploads/<slug>.json updates
# from the cloud upload path) flow back via Firestore + a separate
# pull-on-render mechanism in the render-worker.
#
# Manual run:
#   ./scripts/sync_state_to_gcs.sh
#
# Skip on a one-off:
#   YTFACTORY_STATE_SYNC_DISABLE=1 ./scripts/sync_state_to_gcs.sh
set -euo pipefail

if [[ "${YTFACTORY_STATE_SYNC_DISABLE:-}" == "1" ]]; then
  echo "state-sync: disabled via env"
  exit 0
fi

REPO="${YTFACTORY_REPO:-/Users/rohit/ytFactory}"
BUCKET="${YTFACTORY_STATE_BUCKET:-ytfactory-prod-v3-state}"
CHANNELS=(mystoriesanimated cosmosdecoded historyrecapped hindutavaanimated sportsrecapped scrollpulse rhymetimejunction)

cd "$REPO"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '%s state-sync: %s\n' "$(ts)" "$*"; }

# gsutil flags:
#   -m   parallel
#   -q   quiet (only errors)
#   -r   recursive
#   rsync — only uploads files that changed (mtime + checksum)
#   no -d so we never delete cloud-only files (e.g. cloud uploads/ writes)
GSUTIL_FLAGS="-m -q"

sync_dir() {
  local local_path="$1" gs_path="$2"
  if [[ ! -d "$local_path" ]]; then return 0; fi
  # Audit D3.24 — pre-fix this swallowed ALL errors via `|| true`
  # (auth expired / bucket missing / network down → silent forever).
  # Now: capture stderr+exit, log non-zero exits explicitly, and only
  # ignore the empty-output noise via grep — NOT the gsutil exit code.
  local out
  set +e
  out=$(gsutil $GSUTIL_FLAGS rsync -r "$local_path" "$gs_path" 2>&1)
  local rc=$?
  set -e
  echo "$out" | grep -vE '^$' || true
  if [[ $rc -ne 0 ]]; then
    log "WARN gsutil rsync $local_path → $gs_path failed (rc=$rc)"
    return $rc
  fi
}

sync_file() {
  local local_path="$1" gs_path="$2"
  if [[ ! -f "$local_path" ]]; then return 0; fi
  # Audit D3.24 — same as sync_dir; surface real failures.
  local out
  set +e
  out=$(gsutil -q cp "$local_path" "$gs_path" 2>&1)
  local rc=$?
  set -e
  echo "$out" | grep -vE '^$' || true
  if [[ $rc -ne 0 ]]; then
    log "WARN gsutil cp $local_path → $gs_path failed (rc=$rc)"
    return $rc
  fi
}

log "begin sync to gs://$BUCKET"

for ch in "${CHANNELS[@]}"; do
  if [[ ! -d "$ch" ]]; then continue; fi
  for sub in narrations cast shotlist uploads learnings; do
    sync_dir "$ch/$sub" "gs://$BUCKET/$ch/$sub"
  done
  sync_file "$ch/_holds.json" "gs://$BUCKET/$ch/_holds.json"
  # Niche-nested layouts (mystoriesanimated has variants/reddit_amitheasshole etc.)
  for nichedir in "$ch"/*/; do
    nicheslug="${nichedir%/}"; nicheslug="${nicheslug##*/}"
    case "$nicheslug" in
      narrations|cast|shotlist|uploads|learnings|cache|scratch|raw|footage|branding|music|scripts|variants|long_form|shorts|critiques) continue ;;
    esac
    if [[ -d "$ch/$nicheslug/narrations" ]]; then
      for sub in narrations cast shotlist uploads; do
        sync_dir "$ch/$nicheslug/$sub" "gs://$BUCKET/$ch/$nicheslug/$sub"
      done
    fi
  done
done

log "done"
