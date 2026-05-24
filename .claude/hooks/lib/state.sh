#!/usr/bin/env bash
# Shared turn-scoped state for hook scripts.
#
# Each turn writes to .claude/state/turn-<session>-<turn_num>.json.
# Hooks read/write this through the helpers below.
#
# session = $CLAUDE_SESSION_ID (set by the harness on every hook invocation)
# turn_num = monotonic counter inside the session, stored in the file itself

set -uo pipefail
# NOTE: do NOT enable -e (errexit). Helpers sourced by other scripts
# inherit -e and then any pipeline that returns non-zero (grep with no
# match, etc.) kills the parent hook silently. Hooks rely on pipelines
# that legitimately may return non-zero (grep -q / -c on absent
# patterns) to make boolean decisions. Letting -e kill the script
# turned the doc-citation P29 check into a silent no-op for cases
# where LAST_ASST_TEXT had no docs/ mentions — caught 2026-05-24
# while testing P30 forced-skill-invocation.

# Paths — absolute so hooks work regardless of cwd at invocation time.
HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ROOT="$(cd "$HOOKS_DIR/.." && pwd)"
PROJECT_ROOT="$(dirname "$PROJECT_ROOT")"
STATE_DIR="$PROJECT_ROOT/.claude/state"
mkdir -p "$STATE_DIR"

# Resolve the current turn's state file. The harness gives us a session
# id; we partition state files per-session so concurrent sessions don't
# collide. Turn id within a session is stored in the file itself, so
# UserPromptSubmit increments it and subsequent hooks read it.
_session_id() {
    # Try multiple env vars Claude Code may set; fall back to a static
    # "default" if running outside a session (manual testing).
    printf '%s' "${CLAUDE_SESSION_ID:-${SESSION_ID:-default}}"
}

state_file() {
    printf '%s/turn-%s.json' "$STATE_DIR" "$(_session_id)"
}

# Read the current state JSON. Returns "{}" if absent or unreadable.
state_read() {
    local f
    f="$(state_file)"
    if [ -f "$f" ]; then
        cat "$f"
    else
        printf '{}'
    fi
}

# Replace the state JSON. Atomic via mktemp.
state_write() {
    local f tmp
    f="$(state_file)"
    tmp="$(mktemp)"
    cat > "$tmp"
    mv "$tmp" "$f"
}

# Set a top-level key in the state JSON. Usage: state_set KEY VALUE-AS-JSON
state_set() {
    local key="$1"
    local value="$2"
    state_read | jq --arg k "$key" --argjson v "$value" '.[$k] = $v' | state_write
}

# Append to an array at key. Creates array if missing.
state_append() {
    local key="$1"
    local value="$2"
    state_read | jq --arg k "$key" --argjson v "$value" \
        '.[$k] = ((.[$k] // []) + [$v])' | state_write
}

# Read a top-level key, returning JSON.
state_get() {
    local key="$1"
    state_read | jq --arg k "$key" '.[$k] // null'
}

# Cross-session counter — increment a named recurrence axis.
recurrence_file() {
    printf '%s/recurrences.json' "$STATE_DIR"
}

recurrence_increment() {
    local axis="$1"
    local f
    f="$(recurrence_file)"
    [ -f "$f" ] || echo "{}" > "$f"
    local tmp
    tmp="$(mktemp)"
    jq --arg a "$axis" '.[$a] = ((.[$a] // 0) + 1)' < "$f" > "$tmp"
    mv "$tmp" "$f"
}

recurrence_count() {
    local axis="$1"
    local f
    f="$(recurrence_file)"
    [ -f "$f" ] || { echo 0; return; }
    jq -r --arg a "$axis" '.[$a] // 0' < "$f"
}
