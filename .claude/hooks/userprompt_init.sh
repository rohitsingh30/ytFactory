#!/usr/bin/env bash
# UserPromptSubmit master hook. Runs as the first thing on every turn.
#
# Reads the user's message from the Claude Code hook stdin payload.
# Performs all UserPromptSubmit-class checks in one process to keep
# wire latency low:
#
#   1. Extracts distinct asks (P02/P03/P26 — used by Stop hook later)
#   2. Resolves named entities (P05 — flags wrong-premise / wrong-id)
#   3. Detects ambiguity / "the thing" references (P12)
#   4. Detects scope cues "e2e"/"all"/"everything" (P16)
#   5. Injects state digest (P17/P21/P24)
#
# Writes results to .claude/state/turn-<session>.json for Stop hook
# to verify.
#
# Exit codes:
#   0 = allow, with stdout printed as injected context
#   2 = block, force the agent to ASK before acting (used by entity-
#       resolution + ambiguity check when needed)

set -uo pipefail

LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)"
export PROJECT_ROOT="$(cd "$LIB/../.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

# Source state helpers
. "$LIB/state.sh"

# Read hook stdin (Claude Code provides JSON with the user's message).
RAW="$(cat)"

# Extract the prompt text. Claude Code's hook payload shape:
#   {"hook_event_name": "UserPromptSubmit", "session_id": ..., "prompt": "..."}
PROMPT="$(printf '%s' "$RAW" | jq -r '.prompt // .user_message // empty' 2>/dev/null)"
SESSION_ID_NEW="$(printf '%s' "$RAW" | jq -r '.session_id // empty' 2>/dev/null)"
if [ -n "$SESSION_ID_NEW" ]; then
    export CLAUDE_SESSION_ID="$SESSION_ID_NEW"
fi

if [ -z "$PROMPT" ]; then
    # No prompt parsed — let it through, nothing to do.
    exit 0
fi

# 1. Extract asks → turn checklist
ASKS_JSON="$(printf '%s' "$PROMPT" | "$PYTHON" "$LIB/extract_user_asks.py" 2>/dev/null || echo '[]')"
state_set "user_asks" "$ASKS_JSON"
state_set "user_prompt_chars" "$(printf '%s' "$PROMPT" | wc -c | tr -d ' ')"

# 2. Resolve entities — find wrong premise / mistyped names BEFORE acting
ENT_JSON="$(printf '%s' "$PROMPT" | "$PYTHON" "$LIB/resolve_entity.py" 2>/dev/null || echo '{"resolved":[],"unresolved":[]}')"
state_set "entities" "$ENT_JSON"

UNRESOLVED_COUNT="$(printf '%s' "$ENT_JSON" | jq '.unresolved | length' 2>/dev/null || echo 0)"

# 3. Ambiguity check — fire only on common ambiguity markers
PROMPT_LC="$(printf '%s' "$PROMPT" | tr '[:upper:]' '[:lower:]')"
AMBIGUOUS=0
# These tokens are common in short follow-up messages and rarely
# unambiguous when the message itself is short.
for pat in "the thing" " that\b" "fix it" "do it" "this is wrong" "research it" "build it"; do
    if printf '%s' "$PROMPT_LC" | grep -qE "$pat"; then
        AMBIGUOUS=1
        break
    fi
done

# Only block on ambiguity if message is short AND ambiguous AND no
# entity resolved that anchors what "it" refers to.
PROMPT_LEN="$(printf '%s' "$PROMPT" | wc -c | tr -d ' ')"
RESOLVED_COUNT="$(printf '%s' "$ENT_JSON" | jq '.resolved | length' 2>/dev/null || echo 0)"
state_set "ambiguous" "$([ "$AMBIGUOUS" = 1 ] && echo true || echo false)"

# 4. Scope detector
SCOPE_BROAD=0
if printf '%s' "$PROMPT_LC" | grep -qE "\b(e2e|end[ -]to[ -]end|all (the )?services|every(thing|file|piece)|across (the )?codebase|sweep)\b"; then
    SCOPE_BROAD=1
fi
state_set "scope_broad" "$([ "$SCOPE_BROAD" = 1 ] && echo true || echo false)"

# 5. Inject state digest. This is the stdout that Claude Code prepends
# to the agent's context.
DIGEST="$("$PYTHON" "$LIB/state_digest.py" 2>/dev/null || true)"
if [ -n "$DIGEST" ]; then
    printf '%s\n' "$DIGEST"
fi

# Also emit the parsed checklist so the agent can see it in context.
N_ASKS="$(printf '%s' "$ASKS_JSON" | jq 'length' 2>/dev/null || echo 0)"
if [ "$N_ASKS" -gt 0 ]; then
    printf '%s\n' "## Turn checklist (auto-extracted from your message)"
    printf '%s\n' ""
    printf '%s' "$ASKS_JSON" | jq -r '.[] | "- [ ] (" + .kind + ") " + .ask' 2>/dev/null
    printf '%s\n' ""
    printf '%s\n' "Stop hook will block end-of-turn if any item is silently dropped."
    printf '%s\n' "---"
fi

# Block decision: if there are unresolved entities AND the prompt is
# short (< 200 chars), force the agent to ASK first.
if [ "$UNRESOLVED_COUNT" -gt 0 ] && [ "$PROMPT_LEN" -lt 200 ]; then
    UNRESOLVED_LIST="$(printf '%s' "$ENT_JSON" | jq -r '.unresolved[] | "- \(.kind): \(.value)"' 2>/dev/null)"
    {
        printf 'P05-block: %d named entit%s in your message did not resolve to a real channel/file/model:\n' \
            "$UNRESOLVED_COUNT" "$([ "$UNRESOLVED_COUNT" -gt 1 ] && echo "ies" || echo "y")"
        printf '%s\n' "$UNRESOLVED_LIST"
        printf '\nAsk the user which one they meant before acting.\n'
    } >&2
    exit 2
fi

# Block decision: ambiguous-reference + short prompt + no anchor entity.
#
# 2026-05-24 softening (option B in the hook-fix triple):
# If the recent assistant message contains a multi-choice block (numbered
# / lettered / markdown-table options) OR contains pick-prompt language,
# treat this short prompt as a RESPONSE to that choice, not a fresh
# ambiguous request. Don't block — let the agent handle it.
#
# 2026-05-24 surfacing (option C in the hook-fix triple):
# When the hook DOES block, run the same helper to extract the recent
# options and include them in the stderr message so the user (and agent)
# see the actual candidates instead of just "Ask which specific thing."
TRANSCRIPT_PATH="$(printf '%s' "$RAW" | jq -r '.transcript_path // empty' 2>/dev/null)"
RECENT_OPTIONS_JSON="{}"
HAS_RECENT_OPTIONS="false"
if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
    RECENT_OPTIONS_JSON="$("$PYTHON" "$LIB/recent_context_options.py" "$TRANSCRIPT_PATH" 2>/dev/null || echo '{}')"
    HAS_RECENT_OPTIONS="$(printf '%s' "$RECENT_OPTIONS_JSON" | jq -r '.has_options // false' 2>/dev/null)"
fi
state_set "had_recent_options" "$([ "$HAS_RECENT_OPTIONS" = "true" ] && echo true || echo false)"

if [ "$AMBIGUOUS" = 1 ] && [ "$PROMPT_LEN" -lt 80 ] && [ "$RESOLVED_COUNT" = 0 ]; then
    # Softening: if the recent assistant message had options, this is a
    # response to that choice — allow.
    if [ "$HAS_RECENT_OPTIONS" = "true" ]; then
        exit 0
    fi

    # Otherwise block, but surface the options helper's output (which
    # will be "no options detected" — letting the user see why we
    # think context wasn't enough).
    OPTION_LINES="$("$PYTHON" "$LIB/recent_context_options.py" "$TRANSCRIPT_PATH" text 2>/dev/null || echo '')"
    {
        printf 'P12-block: short ambiguous prompt (no resolved anchor entity, no recent option-list to anchor against).\n'
        printf 'Recent assistant context for reference:\n'
        printf '%s\n' "$OPTION_LINES"
        printf '\nAsk which specific thing the user means before acting.\n'
    } >&2
    exit 2
fi

exit 0
