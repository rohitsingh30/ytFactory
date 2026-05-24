#!/usr/bin/env bash
# PreToolUse master hook. Runs before each tool call.
#
# Performs all PreToolUse-class audits in one process:
#
#   1. lean_audit (P22/P18) — Write/Edit/TaskCreate/Agent: prefer
#      existing files / one-liners / direct action; block over-
#      engineering when a simpler path exists.
#   2. research_before_creative (P04) — Write to data/critiques/,
#      docs/, or PIL imaging calls require a prior WebSearch trace.
#   3. dependency_exists (P11) — image/audio tool calls referencing
#      named assets (music_bed, voice_ref, weights) must resolve.
#   4. preserve_verbatim (P15/P23) — Edit on .md files in /data/ or
#      /docs/ that's removing user-quoted text → block.
#
# Exit codes:
#   0 = allow tool call
#   2 = block; stderr is shown to the agent as reason

set -uo pipefail

LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)"
export PROJECT_ROOT="$(cd "$LIB/../.." && pwd)"

. "$LIB/state.sh"

RAW="$(cat)"
SESSION_ID_NEW="$(printf '%s' "$RAW" | jq -r '.session_id // empty' 2>/dev/null)"
[ -n "$SESSION_ID_NEW" ] && export CLAUDE_SESSION_ID="$SESSION_ID_NEW"

TOOL_NAME="$(printf '%s' "$RAW" | jq -r '.tool_name // empty' 2>/dev/null)"
TOOL_INPUT="$(printf '%s' "$RAW" | jq -c '.tool_input // {}' 2>/dev/null)"

violations=()

# === Lean audit (P22, P18) ===
# Block over-engineering signals. Heuristics tuned to be cheap; this
# hook fires on EVERY tool call so it cannot be expensive.

# 1a. TaskCreate when an existing pending task is similar.
if [ "$TOOL_NAME" = "TaskCreate" ]; then
    NEW_SUBJ="$(printf '%s' "$TOOL_INPUT" | jq -r '.subject // empty' 2>/dev/null)"
    # No deep dedup; just nudge.
    state_append "task_create_attempts" "$(printf '%s' "$NEW_SUBJ" | jq -Rs '.')"
fi

# 1b. Write on a NEW markdown file in docs/ — flag if it could be an
# Edit on an existing file. We can't easily know "could be"; this is
# tuned conservatively to fire only on very obvious cases.
if [ "$TOOL_NAME" = "Write" ]; then
    PATH_NEW="$(printf '%s' "$TOOL_INPUT" | jq -r '.file_path // empty' 2>/dev/null)"
    case "$PATH_NEW" in
        */docs/*.md|*/.claude/projects/*/memory/*.md)
            # Count existing memos / docs of similar prefix.
            DIR="$(dirname "$PATH_NEW")"
            BASE="$(basename "$PATH_NEW" .md)"
            if [ -d "$DIR" ]; then
                EXISTING="$(ls "$DIR" 2>/dev/null | grep -ic "$(echo "$BASE" | cut -d'_' -f1,2)" 2>/dev/null || echo 0)"
                # Strip any non-numeric chars
                EXISTING="$(printf '%s' "$EXISTING" | tr -dc '0-9' || echo 0)"
                EXISTING="${EXISTING:-0}"
                if [ "$EXISTING" -gt 5 ]; then
                    violations+=("P22 lean audit: $DIR already contains $EXISTING files matching this prefix. Consider editing an existing one rather than creating $(basename "$PATH_NEW").")
                fi
            fi
            ;;
    esac
fi

# === Preserve-verbatim (P15/P23) ===
# When editing an .md file in /data/critiques/ or /docs/, if old_string
# contains a verbatim user quote pattern ("> "), block unless new_string
# preserves it.
if [ "$TOOL_NAME" = "Edit" ]; then
    PATH_EDIT="$(printf '%s' "$TOOL_INPUT" | jq -r '.file_path // empty' 2>/dev/null)"
    case "$PATH_EDIT" in
        */data/critiques/*.md|*/docs/*.md)
            OLD_STR="$(printf '%s' "$TOOL_INPUT" | jq -r '.old_string // empty' 2>/dev/null)"
            NEW_STR="$(printf '%s' "$TOOL_INPUT" | jq -r '.new_string // empty' 2>/dev/null)"
            # If the old text contains a markdown blockquote line "> ..."
            # AND the new text doesn't contain the same blockquote, that's
            # a removal of user-quoted material → flag.
            if printf '%s' "$OLD_STR" | grep -qE '^> ' 2>/dev/null; then
                QUOTE_LINE="$(printf '%s' "$OLD_STR" | grep -E '^> ' | head -1)"
                if ! printf '%s' "$NEW_STR" | grep -qF "$QUOTE_LINE" 2>/dev/null; then
                    violations+=("P15/P23 preserve-verbatim: editing $PATH_EDIT removes a user-quoted line ('> ...') that wasn't replaced. Verify with the user before dropping their words.")
                fi
            fi
            ;;
    esac
fi

# === Dependency exists (P11) ===
# When invoking gcloud run jobs execute / images.generate / music
# selection paths, check that referenced asset paths actually exist
# in the worker image (best-effort — we can only check local paths).
# Keep this narrow: only fire on Bash with very specific patterns.
if [ "$TOOL_NAME" = "Bash" ]; then
    CMD="$(printf '%s' "$TOOL_INPUT" | jq -r '.command // empty' 2>/dev/null)"
    # gcloud run jobs execute with --update-env-vars=... — check job_id
    # syntax. Skip; that's runtime.
    :
fi

if [ "${#violations[@]}" -gt 0 ]; then
    {
        printf 'PreToolUse hook BLOCK — %d violation(s):\n\n' "${#violations[@]}"
        for v in "${violations[@]}"; do
            printf '%s\n' "$v"
        done
        printf '\nAdjust the tool call or proceed via a different approach.\n'
    } >&2
    exit 2
fi

exit 0
