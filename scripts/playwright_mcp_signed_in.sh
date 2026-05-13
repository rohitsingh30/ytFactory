#!/usr/bin/env bash
# Wrapper: ensures a signed-in Chrome instance is running on a fixed CDP
# port, then launches @playwright/mcp configured to attach to it via
# --cdp-endpoint. The MCP server speaks stdio to the Copilot CLI; Chrome
# stays alive across MCP restarts because it's launched as an
# independent process group (`setsid` / `nohup`).
#
# Why this exists: MCP's default chrome launcher passes
# --use-mock-keychain --password-store=basic, which breaks cookie
# decryption for any cookies bridged in from the user's real Chrome
# (those cookies are encrypted with the macOS keychain Safe Storage
# key). By launching our own Chrome WITHOUT those flags, the real
# keychain is available and bridged cookies decrypt correctly.
#
# Reference: docs/chrome_signed_in_automation.md

set -euo pipefail

CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UDD="${CHROME_DEBUG_UDD:-$HOME/Library/Application Support/Google/Chrome-Debug}"
PROFILE="${CHROME_DEBUG_PROFILE:-Profile 12}"
CDP_PORT="${CHROME_DEBUG_CDP_PORT:-9222}"
SRC_ROOT="$HOME/Library/Application Support/Google/Chrome"
LOG_DIR="${CHROME_DEBUG_LOG_DIR:-$HOME/.copilot/logs/playwright-signed-in}"
mkdir -p "$LOG_DIR"
STDERR_LOG="$LOG_DIR/chrome.stderr"
PID_FILE="$LOG_DIR/chrome.pid"

log() { echo "[playwright-signed-in $(date +%H:%M:%S)] $*" >&2; }

cdp_alive() {
  curl -sf --max-time 1 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1
}

real_chrome_running() {
  ps -ax -o command | grep -E "^/Applications/Google Chrome\.app/Contents/MacOS/Google Chrome" \
    | grep -v -- "--user-data-dir=$UDD" \
    | grep -v -- "--user-data-dir=.*Chrome-PlaywrightRunner" \
    | grep -q .
}

bridge_cookies() {
  if real_chrome_running; then
    log "real Chrome is running — skipping bridge (using whatever cookies are already in $UDD)"
    return 0
  fi
  if [[ ! -d "$SRC_ROOT/$PROFILE" ]]; then
    log "WARN: source profile not found at $SRC_ROOT/$PROFILE — skipping bridge"
    return 0
  fi
  log "bridging cookies from '$PROFILE' → $UDD"
  mkdir -p "$UDD/$PROFILE/Network"
  cp -p "$SRC_ROOT/Local State" "$UDD/Local State" 2>/dev/null || true
  for f in "Cookies" "Cookies-journal" "Login Data" "Login Data-journal" "Web Data" "Web Data-journal" "Preferences"; do
    [[ -f "$SRC_ROOT/$PROFILE/$f" ]] && cp -p "$SRC_ROOT/$PROFILE/$f" "$UDD/$PROFILE/$f"
  done
  for f in "Cookies" "Cookies-journal"; do
    [[ -f "$SRC_ROOT/$PROFILE/Network/$f" ]] && cp -p "$SRC_ROOT/$PROFILE/Network/$f" "$UDD/$PROFILE/Network/$f"
  done
  return 0
}

launch_chrome() {
  rm -f "$UDD/SingletonLock" "$UDD/SingletonSocket" "$UDD/SingletonCookie"
  : > "$STDERR_LOG"
  # macOS doesn't ship setsid; nohup + & + disown gives us the same
  # "survives parent exit, no controlling terminal" effect.
  nohup "$CHROME_BIN" \
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
    --disable-features=PaintHolding,DialMediaRouteProvider \
    about:blank \
    >/dev/null 2>>"$STDERR_LOG" &
  local pid=$!
  disown $pid 2>/dev/null || true
  echo "$pid" > "$PID_FILE"
  log "Chrome launched pid=$pid, waiting for CDP on :$CDP_PORT"
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

ensure_chrome() {
  if cdp_alive; then
    log "CDP already alive on :$CDP_PORT — reusing existing Chrome"
    return 0
  fi
  bridge_cookies
  launch_chrome
}

ensure_chrome

# Hand off to @playwright/mcp in CDP-attach mode. stdio passes through.
exec npx -y @playwright/mcp \
  --cdp-endpoint "http://127.0.0.1:${CDP_PORT}" \
  --viewport-size 1366x900 \
  --caps vision \
  "$@"
