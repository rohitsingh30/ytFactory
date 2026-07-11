#!/usr/bin/env bash
# Wrapper: ensures a signed-in Microsoft Edge instance is running on a fixed
# CDP port, then launches @playwright/mcp configured to attach to it via
# --cdp-endpoint. Mirrors playwright_mcp_signed_in.sh but targets Edge so we
# can drive a real signed-in Edge work/school profile (e.g. User02@HCLTech).
#
# Why a separate debug user-data-dir + cookie bridge instead of attaching to
# the user's running Edge: the running Edge was not started with
# --remote-debugging-port (no CDP), and Chromium is a singleton per
# user-data-dir, so we cannot enable CDP on it without relaunching. We instead
# bridge the signed-in profile's cookies into an isolated Edge-Debug dir and
# launch our own Edge there. We launch Edge WITHOUT
# --use-mock-keychain/--password-store=basic so the "Microsoft Edge Safe
# Storage" keychain key is available and the bridged (encrypted) cookies
# decrypt correctly.

set -euo pipefail

EDGE_BIN="${EDGE_BIN:-/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge}"
SRC_ROOT="${EDGE_SRC_ROOT:-$HOME/Library/Application Support/Microsoft Edge}"
UDD="${EDGE_DEBUG_UDD:-$HOME/Library/Application Support/Microsoft Edge-Debug}"
PROFILE="${EDGE_DEBUG_PROFILE:-Profile 2}"
CDP_PORT="${EDGE_DEBUG_CDP_PORT:-9223}"
LOG_DIR="${EDGE_DEBUG_LOG_DIR:-$HOME/.copilot/logs/playwright-signed-in-edge}"
mkdir -p "$LOG_DIR"
STDERR_LOG="$LOG_DIR/edge.stderr"
PID_FILE="$LOG_DIR/edge.pid"

log() { echo "[playwright-signed-in-edge $(date +%H:%M:%S)] $*" >&2; }

cdp_alive() {
  curl -sf --max-time 1 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1
}

# Copy a single file if it exists, preserving metadata.
bridge_file() {
  local src="$1" dst="$2"
  [[ -f "$src" ]] && cp -p "$src" "$dst" 2>/dev/null || true
}

bridge_profile() {
  if [[ ! -d "$SRC_ROOT/$PROFILE" ]]; then
    log "WARN: source profile not found at $SRC_ROOT/$PROFILE — skipping bridge"
    return 0
  fi
  log "bridging signed-in data from '$PROFILE' -> $UDD"
  mkdir -p "$UDD/$PROFILE/Network"

  # Local State holds the os_crypt encrypted_key for cookie decryption.
  bridge_file "$SRC_ROOT/Local State" "$UDD/Local State"

  # Profile-scoped state. Edge here stores Cookies at the profile root
  # (DELETE-journal mode => the main DB is consistent for a live copy), but
  # newer builds use Network/Cookies, so bridge both locations if present.
  local f
  for f in "Cookies" "Cookies-journal" "Login Data" "Login Data-journal" \
           "Login Data For Account" "Login Data For Account-journal" \
           "Web Data" "Web Data-journal" "Preferences"; do
    bridge_file "$SRC_ROOT/$PROFILE/$f" "$UDD/$PROFILE/$f"
  done
  for f in "Cookies" "Cookies-journal"; do
    bridge_file "$SRC_ROOT/$PROFILE/Network/$f" "$UDD/$PROFILE/Network/$f"
  done
  return 0
}

launch_edge() {
  rm -f "$UDD/SingletonLock" "$UDD/SingletonSocket" "$UDD/SingletonCookie"
  : > "$STDERR_LOG"
  # nohup + & + disown: survives parent exit, no controlling terminal.
  nohup "$EDGE_BIN" \
    --remote-debugging-port="$CDP_PORT" \
    --remote-debugging-address=127.0.0.1 \
    --user-data-dir="$UDD" \
    --profile-directory="$PROFILE" \
    --no-first-run \
    --no-default-browser-check \
    --disable-blink-features=AutomationControlled \
    --noerrdialogs \
    --hide-crash-restore-bubble \
    --mute-audio \
    --disable-features=PaintHolding,DialMediaRouteProvider,msEdgeWelcomePage \
    about:blank \
    >/dev/null 2>>"$STDERR_LOG" &
  local pid=$!
  disown $pid 2>/dev/null || true
  echo "$pid" > "$PID_FILE"
  log "Edge launched pid=$pid, waiting for CDP on :$CDP_PORT"
  for _ in $(seq 1 50); do
    if cdp_alive; then
      log "CDP is alive on :$CDP_PORT"
      return 0
    fi
    sleep 0.3
  done
  log "ERROR: CDP did not come up; last 30 lines of stderr:"
  tail -30 "$STDERR_LOG" >&2 || true
  return 1
}

ensure_edge() {
  if cdp_alive; then
    log "CDP already alive on :$CDP_PORT — reusing existing Edge"
    return 0
  fi
  bridge_profile
  launch_edge
}

ensure_edge

# Hand off to @playwright/mcp in CDP-attach mode. stdio passes through.
exec npx -y @playwright/mcp \
  --cdp-endpoint "http://127.0.0.1:${CDP_PORT}" \
  --viewport-size 1366x900 \
  --caps vision \
  "$@"
