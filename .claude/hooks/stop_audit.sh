#!/usr/bin/env bash
# Stop master hook. Runs when the agent tries to end its turn.
#
# Performs all Stop-class audits in one process:
#
#   1. Grievance coverage (P02/P03/P09/P26) — every extracted ask
#      must be addressed in the response
#   2. Evidence audit (P01/P07/P19) — claim words need evidence anchors
#   3. Question-form (P13) — questions need 2-4 enumerated options
#   4. Deployment-audit (P06) — "shipped/deployed" needs commit + exit 0
#   5. Parallelization-audit (P25) — 3+ independent items must be parallel
#   6. Patch-shape-detector (P07) — one-line guards for the bug just hit
#   7. Pattern-claim evidence (P20) — "same shape as X" needs Read of X
#   8. Critique-quality (P27, if response is from critique-video skill)
#   9. Analysis-vs-facts (P14, LLM-backed)
#   10. Recurrence counter bump on any of the above firing
#
# Exit codes:
#   0 = allow turn to end
#   2 = block; force the agent to redo (stderr is shown back as a
#       continuation message)

set -uo pipefail

LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)"
export PROJECT_ROOT="$(cd "$LIB/../.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

. "$LIB/state.sh"

# Read hook stdin. For Stop hook, Claude Code provides:
#   {"hook_event_name": "Stop", "session_id": ..., "transcript_path": ..., "stop_hook_active": ...}
RAW="$(cat)"
SESSION_ID_NEW="$(printf '%s' "$RAW" | jq -r '.session_id // empty' 2>/dev/null)"
[ -n "$SESSION_ID_NEW" ] && export CLAUDE_SESSION_ID="$SESSION_ID_NEW"
TRANSCRIPT="$(printf '%s' "$RAW" | jq -r '.transcript_path // empty' 2>/dev/null)"
STOP_ACTIVE="$(printf '%s' "$RAW" | jq -r '.stop_hook_active // false' 2>/dev/null)"

# Anti-infinite-loop guard. Claude Code sets stop_hook_active=true on
# a re-invocation triggered by a previous block. If we keep blocking,
# the user is stuck. Bail out on the second pass.
if [ "$STOP_ACTIVE" = "true" ]; then
    exit 0
fi

# Read the most-recent assistant message from the transcript.
if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
    # Without a transcript we can't audit; allow.
    exit 0
fi

# Extract the last assistant text. Claude Code transcripts are JSONL
# with one line per message event. We want the most-recent line whose
# message.role == "assistant".
#
# 2026-05-24 stale-transcript fix (option A in the hook-fix triple):
# The transcript is sometimes flushed AFTER the Stop hook fires. The
# helper retries up to 3 times with a 250ms delay if the most-recent
# assistant text it sees doesn't match the size we'd expect from a
# just-completed turn (i.e. is empty or suspiciously short).
LAST_ASST_TEXT="$("$PYTHON" - << 'PY' "$TRANSCRIPT"
import json, sys, time
def extract_last(path):
    last = ""
    try:
        with open(path) as f:
            for line in f:
                try: d = json.loads(line)
                except: continue
                m = d.get("message")
                if not isinstance(m, dict): continue
                if m.get("role") != "assistant": continue
                content = m.get("content", "")
                if isinstance(content, list):
                    text = " ".join(
                        x.get("text", "") for x in content
                        if isinstance(x, dict) and x.get("type") == "text"
                    )
                else:
                    text = str(content)
                if text.strip():
                    last = text
    except Exception:
        pass
    return last

# Retry loop: read transcript up to 3 times with 250ms gap. If the
# previous read returned a SHORTER text than the current one, the
# transcript is still being written — wait and re-read. Stop when:
#   (a) text stabilises (two consecutive reads yield same length)
#   (b) max retries reached
prev_len = -1
text = ""
for _ in range(3):
    text = extract_last(sys.argv[1])
    if len(text) == prev_len and prev_len > 0:
        break
    prev_len = len(text)
    time.sleep(0.25)
print(text)
PY
)"

if [ -z "$LAST_ASST_TEXT" ]; then
    exit 0
fi

violations=()
recurrences_to_bump=()

# === 1. Grievance coverage ===
ASKS_JSON="$(state_get user_asks)"
N_ASKS=0
if [ -n "$ASKS_JSON" ] && [ "$ASKS_JSON" != "null" ]; then
    N_ASKS="$(printf '%s' "$ASKS_JSON" | jq 'length' 2>/dev/null || echo 0)"
fi

if [ "$N_ASKS" -gt 0 ]; then
    # For each ask, check whether the assistant's response addresses it.
    # Heuristic delegated to lib/check_grievance_coverage.py — the prior
    # inline `python - <<'PY'` + `<<<` here-string interaction left the
    # script with empty stdin, so every ask was always false-positive
    # flagged. The standalone helper takes ASKS_JSON as argv[1] and
    # the assistant text on stdin — clean wiring, no bash quoting
    # interactions.
    DROPPED="$(printf '%s' "$LAST_ASST_TEXT" | "$PYTHON" "$LIB/check_grievance_coverage.py" "$ASKS_JSON" 2>/dev/null || echo '[]')"
    N_DROPPED="$(printf '%s' "$DROPPED" | jq 'length' 2>/dev/null || echo 0)"
    if [ "$N_DROPPED" -gt 0 ]; then
        violations+=("P02/P03/P26 grievance coverage: $N_DROPPED ask(s) silently dropped:")
        violations+=("$(printf '%s' "$DROPPED" | jq -r '.[] | "  - (" + .kind + ") " + .ask' 2>/dev/null)")
        recurrences_to_bump+=("grievance_coverage")
    fi
fi

# === 2. Evidence audit ===
EVIDENCE_JSON="$(printf '%s' "$LAST_ASST_TEXT" | "$PYTHON" "$LIB/check_evidence.py" 2>/dev/null || echo '{"unsupported":[]}')"
N_UNSUPPORTED="$(printf '%s' "$EVIDENCE_JSON" | jq '.unsupported | length' 2>/dev/null || echo 0)"
if [ "$N_UNSUPPORTED" -gt 0 ]; then
    violations+=("P01 evidence audit: $N_UNSUPPORTED claim(s) without nearby evidence anchor:")
    violations+=("$(printf '%s' "$EVIDENCE_JSON" | jq -r '.unsupported[] | "  - " + .[:200]' 2>/dev/null | head -3)")
    recurrences_to_bump+=("evidence_audit")
fi

# === 3. Question-form ===
# Find every "?" in the response. For each, check that within 400 chars
# AFTER it there are 2-4 "- " bullet items or numbered options.
QUESTION_BAD="$("$PYTHON" - << 'PY'
import re, sys
text = sys.stdin.read()
violations = []
for m in re.finditer(r"\?(?:\s|$)", text):
    pos = m.start()
    window = text[pos:pos + 800]
    # Look for either AskUserQuestion tool call or markdown bullet options
    bullets = len(re.findall(r"(?:^|\n)\s*[-*\d]\s+\S+", window))
    if "AskUserQuestion" in window:
        continue
    if bullets >= 2 and bullets <= 8:
        continue
    snippet = text[max(0, pos-80):pos+1].replace("\n", " ")
    violations.append(snippet[-150:])
print("\n".join(violations[:3]))
PY
)" <<< "$LAST_ASST_TEXT" 2>/dev/null || QUESTION_BAD=""
if [ -n "$QUESTION_BAD" ]; then
    violations+=("P13 open-ended question: response contains question without 2-4 enumerated options:")
    violations+=("$(printf '%s\n' "$QUESTION_BAD" | head -3 | sed 's/^/  - /')")
    recurrences_to_bump+=("open_ended_question")
fi

# === 4. Deployment-audit ===
if printf '%s' "$LAST_ASST_TEXT" | grep -qiE '\b(shipped|deployed|in production|live now)\b'; then
    # If "shipped" mentioned, require: commit SHA (7+ hex) OR a deploy-log signal
    if ! printf '%s' "$LAST_ASST_TEXT" | grep -qE '\b[0-9a-f]{7,40}\b|deploy.*log|gcloud run|revision'; then
        violations+=("P06/P19 deployment audit: 'shipped/deployed' claim without commit SHA, deploy-log, or revision reference.")
        recurrences_to_bump+=("deployment_audit")
    fi
fi

# === 5. Parallelization-audit ===
# Count subagent dispatch hints. If the response describes 3+ independent
# tasks but uses sequential language, flag.
SEQ_COUNT="$(printf '%s' "$LAST_ASST_TEXT" | grep -ciE 'then I.ll|after that|next I.ll|once.*finishes I.ll' || true)"
PARA_COUNT="$(printf '%s' "$LAST_ASST_TEXT" | grep -ciE 'in parallel|simultaneously|dispatched.*subagent|spawn.*subagent|run_in_background' || true)"
if [ "${SEQ_COUNT:-0}" -ge 3 ] && [ "${PARA_COUNT:-0}" -eq 0 ]; then
    violations+=("P25 parallelization audit: 3+ sequential 'then I'll' phrases without any parallel/subagent dispatch.")
    recurrences_to_bump+=("parallelization_audit")
fi

# === 6. Patch-shape detector ===
# Very narrow heuristic: response describes a one-line guard against
# something just observed. Marker: "added a check for X" + no
# "root cause" / "why" mentioned.
if printf '%s' "$LAST_ASST_TEXT" | grep -qiE '\b(added|adding) (a |an |the )?(check|guard|assertion) for\b'; then
    if ! printf '%s' "$LAST_ASST_TEXT" | grep -qiE '\b(root cause|underlying|why this happened|class[- ]of[- ]bug)\b'; then
        violations+=("P07 patch-shape: response added a check for a specific bug without identifying root-cause / class-of-bug.")
        recurrences_to_bump+=("patch_shape")
    fi
fi

# === 7. Pattern-claim evidence ===
# "X is the same shape as Y" / "this is the same as Y" — when found,
# verify Y was Read or Grep'd in this turn.
if printf '%s' "$LAST_ASST_TEXT" | grep -qiE 'same (shape|pattern) as|exactly the (same|like) (failure|pattern|bug)'; then
    # Check if any Read/Grep tool result is in the recent transcript.
    if ! tail -200 "$TRANSCRIPT" 2>/dev/null | grep -qE '"name":\s*"(Read|Grep|Glob)"'; then
        violations+=("P20 pattern-claim evidence: response claims 'same shape as X' but no Read/Grep tool was invoked this turn.")
        recurrences_to_bump+=("pattern_claim")
    fi
fi

# === 8. Broad-scope enforcement (P16) ===
# UserPromptSubmit set scope_broad=true when the user asked for "e2e"/
# "all"/"everything". If true, the response must show 3+ distinct files
# Read (or Grep'd) in this turn.
SCOPE_BROAD="$(state_get scope_broad 2>/dev/null)"
if [ "$SCOPE_BROAD" = "true" ]; then
    READS="$(state_get reads_or_greps 2>/dev/null)"
    N_DISTINCT_READS="$(printf '%s' "$READS" | jq 'if . == null then 0 else (unique | length) end' 2>/dev/null || echo 0)"
    if [ "${N_DISTINCT_READS:-0}" -lt 3 ]; then
        violations+=("P16 broad-scope: user asked for e2e/all/everything but response shows only ${N_DISTINCT_READS} distinct Read/Grep targets in turn. Investigate more files before claiming coverage.")
        recurrences_to_bump+=("broad_scope_underread")
    fi
fi

# === 9. UI bug repro (P10) ===
# If the user reported a "when I click X, Y" or "when I hit X" pattern
# in their original message, the response must show evidence of repro
# (tool invocation that exercises the UI/runtime path) — not just a
# "I think this is the cause" hypothesis.
ASKS_RAW="$(state_get user_asks 2>/dev/null)"
if printf '%s' "$ASKS_RAW" | grep -qiE "when I (click|hit|press|open|publish|render)|button.*not work|UI.*broken|same topic again"; then
    # Did this turn invoke any reproduction tool — gcloud run/firestore
    # query/browser_snapshot/curl/.venv python script?
    if ! tail -300 "$TRANSCRIPT" 2>/dev/null | grep -qE '"name":\s*"(Bash|mcp__playwright)"'; then
        violations+=("P10 UI-bug repro: user reported a runtime/UI bug but no reproduction tool was invoked this turn (Bash / playwright / Firestore lookup). Don't hypothesize — reproduce or ask for the repro steps.")
        recurrences_to_bump+=("ui_bug_no_repro")
    fi
fi

# === 10. Analysis-vs-facts (P14, LLM-backed) ===
# If the user message contains a "tell me what / give me what / what
# is" pattern AND the response is long, classify whether the response
# is analysis vs raw facts. Block if it's analysis.
USER_PROMPT_LOWER="$(printf '%s' "$ASKS_RAW" | jq -r '.[].ask // empty' 2>/dev/null | tr '[:upper:]' '[:lower:]')"
RESP_LEN="$(printf '%s' "$LAST_ASST_TEXT" | wc -c | tr -d ' ')"
if printf '%s' "$USER_PROMPT_LOWER" | grep -qE "tell me what|give me what|just (tell|give) (me )?the|show me the|what is the missing|no.*your (knowledge|reasoning|bullshit)" && [ "${RESP_LEN:-0}" -gt 800 ]; then
    # Only run the classifier if API key is set (otherwise fail-open)
    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        CLASS_QUERY="The user asked for raw facts ('tell me what', 'just give me'). Does the assistant's response give raw facts/quotes (e.g. file contents, command output, direct quotes), or does it instead provide analysis / synthesis / interpretation in its own words?"
        CLASS_INPUT="$(printf '%s' "$LAST_ASST_TEXT" | jq -Rs --arg q "$CLASS_QUERY" '{question: $q, context: .}')"
        CLASS_RESULT="$(printf '%s' "$CLASS_INPUT" | "$PYTHON" "$LIB/llm_classify.py" 2>/dev/null || echo '{"answer":"unsure"}')"
        ANSWER="$(printf '%s' "$CLASS_RESULT" | jq -r '.answer // "unsure"' 2>/dev/null)"
        if [ "$ANSWER" = "no" ]; then
            REASON="$(printf '%s' "$CLASS_RESULT" | jq -r '.reason // ""' 2>/dev/null)"
            violations+=("P14 analysis-when-facts-wanted: user asked for raw facts but response is analysis. Haiku says: $REASON")
            recurrences_to_bump+=("analysis_when_facts")
        fi
    fi
fi

# === 11b. Multichoice-required (P28 — added 2026-05-24 per user) ===
# If the response contains 3+ option-shaped items (numbered or
# markdown-table or letter-labeled), the agent MUST also invoke
# AskUserQuestion so the user gets clickable selection rather than
# typing back a letter / number. User's exact directive: "give me as
# multichoice that I can select easily."
N_NUM_OPTIONS="$(printf '%s' "$LAST_ASST_TEXT" | grep -cE '^\s*([0-9]+|[A-D])[.)]\s+' 2>/dev/null || echo 0)"
N_NUM_OPTIONS="$(printf '%s' "$N_NUM_OPTIONS" | tr -dc '0-9' || echo 0)"
N_NUM_OPTIONS="${N_NUM_OPTIONS:-0}"
N_TABLE_OPTIONS="$(printf '%s' "$LAST_ASST_TEXT" | grep -cE '^\|\s*([0-9]+|[A-D])\s*\|' 2>/dev/null || echo 0)"
N_TABLE_OPTIONS="$(printf '%s' "$N_TABLE_OPTIONS" | tr -dc '0-9' || echo 0)"
N_TABLE_OPTIONS="${N_TABLE_OPTIONS:-0}"
TOTAL_OPTIONS=$((N_NUM_OPTIONS + N_TABLE_OPTIONS))
if [ "$TOTAL_OPTIONS" -ge 3 ]; then
    # Did the agent also call AskUserQuestion in this turn? Look at the
    # transcript for a recent AskUserQuestion tool invocation.
    if ! tail -300 "$TRANSCRIPT" 2>/dev/null | grep -qE '"name":\s*"AskUserQuestion"'; then
        violations+=("P28 multichoice-required: response contains $TOTAL_OPTIONS option-shaped items but no AskUserQuestion tool call. User asked for clickable multichoice not typed-back-letters — use the AskUserQuestion tool when presenting choices.")
        recurrences_to_bump+=("multichoice_required")
    fi
fi

# === 11. Critique-quality (P27) ===
# When the most-recent assistant turn looks like a critique output
# (matches the /critique-video skill output shape), scan for
# spewing patterns: vague hedge tokens, paragraphs without timestamp
# anchors, fewer than 3 concrete observations.
if printf '%s' "$LAST_ASST_TEXT" | grep -qE "^## Watching |^# Watching |## What this video feels|## Where the video weakened"; then
    SPEW_TOKENS="$(printf '%s' "$LAST_ASST_TEXT" | grep -ciE '\b(feels? (like)?|kind of|somewhat|sort of|maybe|perhaps|might be|seems? to|appears to|seems? like)\b' || echo 0)"
    SPEW_TOKENS="$(printf '%s' "$SPEW_TOKENS" | tr -dc '0-9' || echo 0)"
    SPEW_TOKENS="${SPEW_TOKENS:-0}"
    TIMESTAMP_REFS="$(printf '%s' "$LAST_ASST_TEXT" | grep -coE '\b[0-9]+:[0-9]{2}\b|\baround [0-9]+s\b|at [0-9]+\.[0-9]s\b' || echo 0)"
    TIMESTAMP_REFS="$(printf '%s' "$TIMESTAMP_REFS" | tr -dc '0-9' || echo 0)"
    TIMESTAMP_REFS="${TIMESTAMP_REFS:-0}"
    if [ "$SPEW_TOKENS" -gt 5 ] && [ "$TIMESTAMP_REFS" -lt 3 ]; then
        violations+=("P27 critique-quality: critique output has $SPEW_TOKENS hedge tokens (feels/kind of/somewhat) but only $TIMESTAMP_REFS timestamp anchors. Concrete observations need timestamps; hedge tokens are spewing.")
        recurrences_to_bump+=("critique_spewing")
    fi
fi

# === If any violations, block ===
if [ "${#violations[@]}" -gt 0 ]; then
    # Bump recurrence counters
    for axis in "${recurrences_to_bump[@]}"; do
        recurrence_increment "$axis"
    done
    {
        printf 'Stop hook BLOCK — %d violation(s):\n\n' "${#violations[@]}"
        for v in "${violations[@]}"; do
            printf '%s\n' "$v"
        done
        printf '\nRedo the turn addressing the above before ending.\n'
    } >&2
    exit 2
fi

exit 0
