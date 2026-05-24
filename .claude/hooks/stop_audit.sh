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
LAST_ASST_TEXT="$("$PYTHON" - << 'PY' "$TRANSCRIPT"
import json, sys
last_text = ""
try:
    with open(sys.argv[1]) as f:
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
                last_text = text
except Exception:
    pass
print(last_text)
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
    # Heuristic: if 3+ alphanumeric tokens (length>=4) from the ask appear
    # in the response, consider it addressed. Cheap, conservative — false
    # positives over false negatives so users aren't forever blocked.
    DROPPED="$("$PYTHON" - "$ASKS_JSON" << 'PY'
import json, re, sys
asks = json.loads(sys.argv[1])
text = sys.stdin.read().lower()
def tokens(s):
    return [t for t in re.findall(r"[a-z0-9_]{4,}", s.lower())
            if t not in {"this","that","with","from","into","does","need","also","just","like","what","when","want"}]
dropped = []
for a in asks:
    ask_text = a.get("ask", "")
    toks = tokens(ask_text)
    if not toks:
        continue
    matches = sum(1 for t in set(toks) if t in text)
    coverage = matches / max(len(set(toks)), 1)
    if coverage < 0.25 and matches < 3:
        dropped.append(a)
print(json.dumps(dropped))
PY
)" <<< "$LAST_ASST_TEXT" 2>/dev/null || DROPPED="[]"
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
