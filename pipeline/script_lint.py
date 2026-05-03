"""Stage 3.5 — narration linter + auto-fix.

The rewrite prompt (rewrite.py) tells claude exactly what sentence
shape to produce ("5–12 word avg, hard cap 14, never two consecutive
≤3-word fragments, vary length"). Claude follows it most of the time
but not reliably — observed failures on a recent batch:

  AITA craft-night Short: [9, 6, 6, 4, 3, 5, 7, 1, 1, 2, ...]

The "1, 1, 2" run is exactly what the prompt forbids ("staccato list,
not a punch — TTS chops them like a robot reading inventory"). Adding
yet more rules to the already-200-line prompt isn't the fix; the prompt
is at the limit of what claude reliably tracks.

Instead: a deterministic post-pass. We DETECT violations and AUTO-FIX
the safe class (consecutive shorts → joined with commas, which is the
"GOOD (one-breath list)" form the prompt itself prescribes). Anything
unsafe to auto-fix gets logged for the operator.

This is a class-of-bug fix per the project memory: every future
narration is now constrained at the SCHEMA level, not by hoping the
LLM follows the prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Mirrors the rule constants stated in the rewrite prompt. Kept here so
# both prompt and linter share one source of truth — change the rule
# in one place and both update.
SENTENCE_HARD_CAP_WORDS = 14
SENTENCE_AVG_TARGET_LO = 5
SENTENCE_AVG_TARGET_HI = 12
PUNCH_MAX_WORDS = 3        # what counts as a "short fragment"
MAX_CONSECUTIVE_PUNCH = 1  # 2+ in a row = the staccato regression we're fixing
MIN_SENTENCES = 10
TARGET_WORDS_LO = 110
TARGET_WORDS_HI = 160


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


@dataclass
class LintResult:
    fixed: bool                 # did we change the narration?
    narration: str              # post-fix narration (== input if no fix)
    violations: list[str]       # human-readable issues (post-fix; "" if all clean)
    fixes_applied: list[str]    # what auto-fixes ran (empty if no fix)


def _word_count(s: str) -> int:
    return len([w for w in s.split() if w.strip()])


def _split_paragraphs(narration: str) -> list[str]:
    return [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(narration) if p.strip()]


def _split_sentences(paragraph: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(paragraph) if s.strip()]


def _strip_terminator(s: str) -> str:
    """Drop trailing .!? so a sentence can be merged into a comma-joined list."""
    return re.sub(r"[.!?]+$", "", s).rstrip()


def _merge_consecutive_shorts(paragraph: str) -> tuple[str, int]:
    """Inside one paragraph, collapse any run of 2+ ≤PUNCH_MAX_WORDS
    sentences into a single comma-joined sentence.

    Example:
        "Wine. Crafts. Just us." → "Wine, crafts, just us."

    The rewrite prompt explicitly prescribes this form for list-style
    enumerations ("GOOD (one-breath list): Wine, crafts, just us."), so
    we're applying the prompt's own preferred shape — not inventing new
    style. Returns ``(new_paragraph, n_runs_merged)``.
    """
    sents = _split_sentences(paragraph)
    if len(sents) < 2:
        return paragraph, 0

    out: list[str] = []
    runs_merged = 0
    i = 0
    while i < len(sents):
        if _word_count(sents[i]) <= PUNCH_MAX_WORDS:
            # Find the extent of this short-run.
            j = i
            while j < len(sents) and _word_count(sents[j]) <= PUNCH_MAX_WORDS:
                j += 1
            run = sents[i:j]
            if len(run) >= 2:
                # Merge: strip terminators on all but the last, lowercase
                # the joins so the comma-list reads naturally. We KEEP
                # the last sentence's terminator so the run still ends
                # like a sentence ("Wine, crafts, just us." not "...us").
                head = [_strip_terminator(s) for s in run[:-1]]
                # Lowercase first letter of run[1:] when the original had
                # a leading capital — they're now mid-clause so a capital
                # in the middle reads jarring. Skip if the token looks
                # like an acronym (all-caps multi-letter) so AITA stays
                # AITA, NTA stays NTA, etc.
                def _decap(s: str) -> str:
                    """Lowercase a sentence's first letter UNLESS the first
                    word is the pronoun "I" (single-letter capital alphabetic)
                    or an acronym (all-caps multi-letter alphabetic), both
                    of which are case-meaningful and must stay as-is.
                    """
                    if not s:
                        return s
                    first_word = s.split(" ", 1)[0]
                    # Strip trailing punctuation off the first word for the
                    # case check (e.g. "AITA?" → "AITA"). Without this an
                    # all-caps acronym followed by a "?" reads as not-isupper
                    # because of the punctuation.
                    bare = re.sub(r"[^A-Za-z]+$", "", first_word)
                    if bare.isupper() and bare.isalpha():
                        return s  # "I", "AITA", "NTA", "WIBTA" etc.
                    return s[:1].lower() + s[1:] if s[:1].isupper() else s

                joined_parts = [head[0]]
                for h in head[1:]:
                    joined_parts.append(_decap(h))
                tail = _decap(run[-1])
                merged = ", ".join(joined_parts + [tail])
                # Capitalise the first character of the merged sentence.
                merged = merged[:1].upper() + merged[1:] if merged else merged
                out.append(merged)
                runs_merged += 1
                i = j
                continue
            # Run of exactly one short sentence is FINE — that's the
            # intended punch. Pass through untouched.
            out.append(sents[i])
            i += 1
            continue
        out.append(sents[i])
        i += 1

    return " ".join(out), runs_merged


def _check(narration: str) -> list[str]:
    """Return a list of violation strings (empty if clean)."""
    issues: list[str] = []
    paras = _split_paragraphs(narration)
    if not paras:
        return ["narration is empty"]

    all_sents: list[tuple[int, str]] = []
    for p_idx, para in enumerate(paras):
        for s in _split_sentences(para):
            all_sents.append((p_idx, s))

    if len(all_sents) < MIN_SENTENCES:
        issues.append(
            f"only {len(all_sents)} sentences (target ≥{MIN_SENTENCES})"
        )

    word_counts = [_word_count(s) for _, s in all_sents]
    total_words = sum(word_counts)
    if total_words < TARGET_WORDS_LO:
        issues.append(
            f"only {total_words} total words (target ≥{TARGET_WORDS_LO})"
        )
    if total_words > TARGET_WORDS_HI + 20:  # mild overflow tolerated
        issues.append(
            f"{total_words} total words exceeds soft cap "
            f"({TARGET_WORDS_HI}+20)"
        )

    # Hard cap on sentence length.
    long_sents = [
        (i, c, s) for i, (c, (_, s)) in enumerate(zip(word_counts, all_sents))
        if c > SENTENCE_HARD_CAP_WORDS
    ]
    for i, c, s in long_sents:
        issues.append(
            f"sentence {i} is {c} words (cap {SENTENCE_HARD_CAP_WORDS}): {s!r}"
        )

    # Avg sentence length sanity check.
    avg = total_words / max(1, len(all_sents))
    if avg < SENTENCE_AVG_TARGET_LO:
        issues.append(
            f"avg sentence length {avg:.1f} below target "
            f"[{SENTENCE_AVG_TARGET_LO}, {SENTENCE_AVG_TARGET_HI}] — "
            f"narration may read as choppy fragments"
        )
    if avg > SENTENCE_AVG_TARGET_HI:
        issues.append(
            f"avg sentence length {avg:.1f} above target "
            f"[{SENTENCE_AVG_TARGET_LO}, {SENTENCE_AVG_TARGET_HI}] — "
            f"sentences too long for caption density"
        )

    # Consecutive short fragments — the bug we wrote this for.
    run = 0
    for c in word_counts:
        if c <= PUNCH_MAX_WORDS:
            run += 1
            if run > MAX_CONSECUTIVE_PUNCH:
                issues.append(
                    f"≥{run} consecutive ≤{PUNCH_MAX_WORDS}-word "
                    f"sentences (staccato regression — should be a "
                    f"comma-joined one-breath list)"
                )
                # Don't keep flagging the same run.
                break
        else:
            run = 0

    return issues


def lint_and_fix(narration: str) -> LintResult:
    """Validate narration; auto-fix the safely-fixable issues.

    Currently auto-fixes:
      - Consecutive ≤PUNCH_MAX_WORDS sentences → comma-joined list
        within the same paragraph.

    Other violations (>14-word sentences, total-word counts, average
    length) are reported but NOT auto-fixed — silently rewriting them
    would change the story content, which only the LLM should do.
    """
    fixed_paras: list[str] = []
    runs_merged_total = 0
    for para in _split_paragraphs(narration):
        new_para, runs_merged = _merge_consecutive_shorts(para)
        runs_merged_total += runs_merged
        fixed_paras.append(new_para)

    new_narration = "\n\n".join(fixed_paras)
    fixes_applied: list[str] = []
    fixed = new_narration != narration
    if fixed:
        fixes_applied.append(
            f"merged {runs_merged_total} run(s) of consecutive "
            f"≤{PUNCH_MAX_WORDS}-word sentences into comma-joined lists"
        )
    return LintResult(
        fixed=fixed,
        narration=new_narration,
        violations=_check(new_narration),
        fixes_applied=fixes_applied,
    )
