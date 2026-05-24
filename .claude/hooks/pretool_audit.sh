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

# === Track WebSearch invocations in turn state ===
# Used by P04 research-before-creative (below) to know if research
# happened in this turn. Also used by Stop hook for fabrication audit.
if [ "$TOOL_NAME" = "WebSearch" ] || [ "$TOOL_NAME" = "WebFetch" ]; then
    Q="$(printf '%s' "$TOOL_INPUT" | jq -r '.query // .url // empty' 2>/dev/null)"
    state_append "web_searches" "$(printf '%s' "$Q" | jq -Rs '.')"
fi

# === Track Read/Grep invocations in turn state ===
# Used by Stop hook's P20 pattern-claim audit + P16 broad-scope check.
if [ "$TOOL_NAME" = "Read" ] || [ "$TOOL_NAME" = "Grep" ] || [ "$TOOL_NAME" = "Glob" ]; then
    TARGET="$(printf '%s' "$TOOL_INPUT" | jq -r '.file_path // .pattern // .path // empty' 2>/dev/null)"
    state_append "reads_or_greps" "$(printf '%s' "$TARGET" | jq -Rs '.')"
fi

# === P32 sweep-evidence tracking ===
# A "sweep" is a tool call that walks an entire class of files/configs/
# events — the evidence required before claiming "all X are fixed" /
# "every Y is done". Without a sweep in the turn state, the Stop hook
# rejects overclaim quantifiers.
#
# Counts as a sweep:
#   - Glob (always — that's literally a sweep)
#   - Grep with a path argument that's a directory (not a single file)
#   - Bash command containing: grep -r / grep --include / find / rg /
#     pytest <dir> / pytest -k <pattern> / gsutil ls -r / gcloud
#     logging read with no severity narrowing
#
# Recorded as state.sweeps_done = [{"kind": "...", "target": "..."}, ...]
if [ "$TOOL_NAME" = "Glob" ]; then
    G_PATTERN="$(printf '%s' "$TOOL_INPUT" | jq -r '.pattern // empty' 2>/dev/null)"
    G_PATH="$(printf '%s' "$TOOL_INPUT" | jq -r '.path // empty' 2>/dev/null)"
    state_append "sweeps_done" "$(jq -nc --arg k "glob" --arg t "${G_PATH}/${G_PATTERN}" '{kind:$k,target:$t}')"
fi
if [ "$TOOL_NAME" = "Grep" ]; then
    GR_PATH="$(printf '%s' "$TOOL_INPUT" | jq -r '.path // empty' 2>/dev/null)"
    GR_PATTERN="$(printf '%s' "$TOOL_INPUT" | jq -r '.pattern // empty' 2>/dev/null)"
    # Treat as sweep if path is a directory OR is empty (Grep defaults to cwd recursive)
    if [ -z "$GR_PATH" ] || [ -d "$GR_PATH" ]; then
        state_append "sweeps_done" "$(jq -nc --arg k "grep" --arg t "${GR_PATH:-.}::${GR_PATTERN}" '{kind:$k,target:$t}')"
    fi
fi
if [ "$TOOL_NAME" = "Bash" ]; then
    B_CMD="$(printf '%s' "$TOOL_INPUT" | jq -r '.command // empty' 2>/dev/null)"
    # Recognize multi-file sweep commands.
    SWEEP_REGEX='(grep -[rR]|grep --include|^find |\srg [-A-Za-z]+ |\srg \"|pytest tests/|pytest [a-z]+/|gsutil ls -r|gcloud logging read)'
    if printf '%s' "$B_CMD" | grep -qE "$SWEEP_REGEX" 2>/dev/null; then
        # Truncate the command for storage
        B_CMD_SHORT="$(printf '%s' "$B_CMD" | head -c 200)"
        state_append "sweeps_done" "$(jq -nc --arg k "bash" --arg t "$B_CMD_SHORT" '{kind:$k,target:$t}')"
    fi
fi

# === P04 research-before-creative ===
# Block Write on data/critiques/, docs/, and PIL imaging tool calls
# unless WebSearch was invoked earlier in this turn — catches the
# "produced a CTA mockup from priors" failure mode.
if [ "$TOOL_NAME" = "Write" ]; then
    PATH_WRITE="$(printf '%s' "$TOOL_INPUT" | jq -r '.file_path // empty' 2>/dev/null)"
    case "$PATH_WRITE" in
        */data/critiques/*.png|*/data/critiques/*.jpg|*/data/critiques/*-mockup*|*/docs/*-mockup*)
            # Creative image output — require prior research
            N_SEARCHES="$(state_get "web_searches" | jq 'if . == null then 0 else length end' 2>/dev/null || echo 0)"
            if [ "$N_SEARCHES" = "0" ] || [ "$N_SEARCHES" = "null" ]; then
                violations+=("P04 fabricated-output: about to Write a creative artifact ($PATH_WRITE) but no WebSearch / WebFetch was invoked this turn. Modern conventions for this output type must be researched first, not produced from priors.")
            fi
            ;;
    esac
fi

# Same gate on PIL imaging via Python — catch the .venv/bin/python ... PIL pattern
# Tightened to require ACTUAL python execution context AND an
# image-write call. Pure word-mentions (in commit messages, in shell
# heredocs that aren't Python) must NOT trigger; that was the
# false-positive that fired on this hook's own commit.
if [ "$TOOL_NAME" = "Bash" ]; then
    CMD="$(printf '%s' "$TOOL_INPUT" | jq -r '.command // empty' 2>/dev/null)"
    # Skip if the command is clearly data-piping (echo/printf/cat
    # heredoc to another tool). Those embed source code as data
    # strings — the trigger words mentioned inside aren't actual
    # python execution.
    CMD_TRIMMED="$(printf '%s' "$CMD" | sed 's/^[[:space:]]*//')"
    IS_DATA_PIPE=0
    case "$CMD_TRIMMED" in
        echo\ *|printf\ *|cat\ *|"git commit"*|"git add"*) IS_DATA_PIPE=1 ;;
    esac
    # Also skip if the command is invoking a hook directly (testing infra).
    if printf '%s' "$CMD" | grep -qE '\.claude/hooks/' 2>/dev/null; then
        IS_DATA_PIPE=1
    fi

    if [ "$IS_DATA_PIPE" = "0" ]; then
        # Heuristic: require python-execution invocation AND PIL import
        # AND an image .save() call within close proximity, AND output
        # path matches mockup/preview/sample/cta.
        if printf '%s' "$CMD" | grep -qE '(\.venv/bin/python|python3?\s)' 2>/dev/null \
           && printf '%s' "$CMD" | grep -qE 'from PIL' 2>/dev/null \
           && printf '%s' "$CMD" | grep -qE '\.save\(' 2>/dev/null \
           && printf '%s' "$CMD" | grep -qiE '\.save\([^)]*(mockup|preview|sample|cta)' 2>/dev/null; then
            N_SEARCHES="$(state_get "web_searches" | jq 'if . == null then 0 else length end' 2>/dev/null || echo 0)"
            if [ "$N_SEARCHES" = "0" ] || [ "$N_SEARCHES" = "null" ]; then
                violations+=("P04 fabricated-output: Python+PIL command writes a mockup/preview/sample image without prior WebSearch this turn. Research the visual conventions first.")
            fi
        fi
    fi
fi

# === Dependency exists (P11) ===
# Check that named asset paths referenced in Bash commands actually
# resolve. Targets the music_bed / voice_ref / weights class of bug.
if [ "$TOOL_NAME" = "Bash" ]; then
    CMD="$(printf '%s' "$TOOL_INPUT" | jq -r '.command // empty' 2>/dev/null)"
    # Pattern: --update-env-vars=KEY=path or YTFACTORY_X=path or
    # similar. Extract every path-shaped argument and verify.
    # Conservative: only check paths that look like channel music/
    # or voice_refs/ — these are the documented F24 drift surface.
    MISSING_PATHS="$(printf '%s' "$CMD" | "$PROJECT_ROOT/.venv/bin/python" - << 'PY' 2>/dev/null
import re, sys, os
cmd = sys.stdin.read()
paths = re.findall(
    r"((?:[a-z]+/){1,5}(?:music|voice_refs|weights)/[a-zA-Z0-9_.-]+\.(?:mp3|wav|safetensors|bin|pt))",
    cmd,
)
repo = os.environ.get("PROJECT_ROOT", "/Users/rohit/ytFactory")
for p in paths:
    full = os.path.join(repo, p)
    if not os.path.exists(full):
        print(p)
PY
)"
    if [ -n "$MISSING_PATHS" ]; then
        violations+=("P11 dependency-asset-missing: Bash references asset paths that don't exist on disk:")
        violations+=("$(printf '%s\n' "$MISSING_PATHS" | head -3 | sed 's/^/  - /')")
    fi
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
