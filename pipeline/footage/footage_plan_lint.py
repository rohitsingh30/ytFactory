"""Footage-plan preflight lint — gives the existing anchor-no-respelling rule teeth.

Background: ``sportsrecapped/learnings/long_form_doc_anchor_no_respellings.md``
documents that ``narration_anchor`` strings used by the long-form sports-doc
renderer must be **whisper-transcript-friendly** — no respellings, apostrophes,
em-dashes, digits, hyphenated number-words. When violated, the renderer
silently drops the anchor and the footage entry never lands; the curator only
finds out by watching the output.

This module turns the rule from a markdown note into a hard preflight check.
``lint_footage_plan(plan_dict)`` returns a list of violations; ``raise_if_any()``
turns the list into a ``FootagePlanLintError`` so ``render_long_form_doc.py``
can fail fast.

Author the project doc at:
``sportsrecapped/learnings/long_form_doc_anchor_no_respellings.md``
(already exists — this module is the enforcement mirror).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


# Hyphenated lowercase-UPPERCASE token = pronunciation respelling
# Examples: ``zheh-NEH-zee-oh``, ``bare-na-BAY-oo``, ``ben-zeh-MAH``
_RESPELLING_RE = re.compile(r"\b[a-z]+(?:-[A-Z]+)+(?:-[a-z]+)*\b")
# Or all-lowercase hyphenated trains of 3+ syllables — ``em-bah-pay``,
# ``ron-al-deen-yo`` — same risk class.
_LOWER_TRAIN_RE = re.compile(r"\b(?:[a-z]{2,4}-){2,}[a-z]{2,4}\b")
# Hyphenated number-words — whisper writes these as digits.
# ``twenty-five``, ``thirty-three``, ``ninety-eight``…
_NUMBER_WORD_RE = re.compile(
    r"\b(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)-"
    r"(?:one|two|three|four|five|six|seven|eight|nine)\b",
    re.IGNORECASE,
)
# Apostrophes — whisper drops them inconsistently.
# ``That's``, ``you'll``, ``Newell's``
_APOSTROPHE_RE = re.compile(r"[A-Za-z]['’][a-z]")
# Em / en dashes — whisper transcribes as space.
_DASH_RE = re.compile(r"[—–]")
# Diacritics — whisper de-accents.
_DIACRITIC_NAMES = {"é", "ã", "ç", "ñ", "ü", "ö", "ä", "í", "á", "ó", "ú"}


_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to",
    "for", "with", "by", "from", "as", "is", "was", "were", "are", "be",
    "been", "being", "this", "that", "these", "those", "his", "her",
    "their", "its", "it", "i", "you", "he", "she", "we", "they",
})


@dataclass
class AnchorViolation:
    section: str           # 'match_footage', 'talking_heads', 'b_roll', 'archival_footage', 'motion_graphics'
    entry_id: str | None
    anchor: str
    rule: str              # short rule name, e.g. 'respelling'
    detail: str            # the offending substring or count

    def __str__(self) -> str:  # pragma: no cover — formatting helper
        anchor_short = self.anchor if len(self.anchor) < 80 else self.anchor[:77] + "…"
        return (
            f"[{self.section}/{self.entry_id or '?'}] {self.rule}: "
            f"{self.detail!r} in anchor: {anchor_short!r}"
        )


class FootagePlanLintError(RuntimeError):
    """Raised when a footage_plan has anchor violations that would silently
    drop entries at render time."""


_SECTIONS = (
    "match_footage",
    "talking_heads",
    "b_roll",
    "archival_footage",
    "motion_graphics",
)


def _has_diacritic(text: str) -> str | None:
    for ch in text:
        if ch.lower() in _DIACRITIC_NAMES:
            return ch
        # Belt-and-braces: any combining mark.
        if unicodedata.combining(ch):
            return ch
    return None


def _content_word_count(text: str) -> int:
    tokens = re.findall(r"\b[a-zA-Z][a-zA-Z'’-]*\b", text)
    return sum(1 for t in tokens if t.lower() not in _STOPWORDS)


def _check_anchor(text: str, *, section: str, entry_id: str | None) -> list[AnchorViolation]:
    violations: list[AnchorViolation] = []

    if not text or not text.strip():
        violations.append(AnchorViolation(section, entry_id, text or "", "empty", "anchor is empty"))
        return violations

    if m := _RESPELLING_RE.search(text):
        violations.append(AnchorViolation(section, entry_id, text, "respelling", m.group(0)))

    if m := _LOWER_TRAIN_RE.search(text):
        # Skip common compound nouns like ``high-value``. Heuristic: 3+ pieces.
        pieces = m.group(0).split("-")
        if len(pieces) >= 3:
            violations.append(AnchorViolation(section, entry_id, text, "lowercase-train", m.group(0)))

    if m := _NUMBER_WORD_RE.search(text):
        violations.append(AnchorViolation(section, entry_id, text, "number-word", m.group(0)))

    if m := _APOSTROPHE_RE.search(text):
        violations.append(AnchorViolation(section, entry_id, text, "apostrophe", m.group(0)))

    if m := _DASH_RE.search(text):
        violations.append(AnchorViolation(section, entry_id, text, "dash", m.group(0)))

    if re.search(r"\b\d+\b", text):
        violations.append(AnchorViolation(section, entry_id, text, "digits", "digit token"))

    if d := _has_diacritic(text):
        violations.append(AnchorViolation(section, entry_id, text, "diacritic", d))

    cw = _content_word_count(text)
    if cw < 4:
        violations.append(
            AnchorViolation(section, entry_id, text, "too-short", f"{cw} content words (min 4)")
        )

    return violations


def lint_footage_plan(plan: dict) -> list[AnchorViolation]:
    """Run all anchor checks across every section of a footage_plan dict.

    Empty sections / missing keys are fine — only entries that exist are
    checked.

    Background ``b_roll`` (no ``narration_anchor`` field, or empty) is
    intentional: per ``pipeline/render/sports_doc.py:_build_filler_video``,
    b_roll entries WITHOUT an anchor become background filler that loops
    end-to-end to fill gaps between pinned overlays. Skip those.
    Same exemption for ``motion_graphics`` entries marked ``deferred:
    true`` (Phase-2 stubs that don't render in v1).
    """
    violations: list[AnchorViolation] = []
    for section in _SECTIONS:
        for entry in plan.get(section) or []:
            anchor = (entry.get("narration_anchor") or "").strip()
            entry_id = entry.get("id")
            if section == "b_roll" and not anchor:
                # Background filler — anchor intentionally absent.
                continue
            if section == "motion_graphics" and entry.get("deferred"):
                continue
            violations.extend(_check_anchor(anchor, section=section, entry_id=entry_id))
    return violations


def raise_if_any(plan: dict, *, slug: str | None = None) -> None:
    """Raise FootagePlanLintError if any anchor violations exist.

    Bypass for dev iteration: set env ``FOOTAGE_PLAN_LINT_DISABLE=1``.
    """
    import os

    if os.environ.get("FOOTAGE_PLAN_LINT_DISABLE") == "1":
        return

    violations = lint_footage_plan(plan)
    if not violations:
        return

    msg_lines = [
        f"footage_plan lint failed for slug={slug!r} — {len(violations)} violation(s):",
        "",
    ]
    msg_lines.extend(f"  - {v}" for v in violations)
    msg_lines.extend([
        "",
        "Why this matters: at render time, every narration_anchor is fuzzy-matched",
        "against a whisper transcript of the synthesised TTS. Whisper drops",
        "diacritics, drops apostrophes, transcribes respellings as different",
        "English words, and writes numbers as digits. An anchor with any of these",
        "WILL silently miss alignment and the footage entry WILL be dropped.",
        "",
        "Rule (project doc):",
        "  sportsrecapped/learnings/long_form_doc_anchor_no_respellings.md",
        "",
        "Fix: rewrite each anchor using only the surrounding English-only context,",
        "no respellings, no apostrophes, no digits, no number-words, no em-dashes.",
        "",
        "Override (dev only): FOOTAGE_PLAN_LINT_DISABLE=1",
    ])
    raise FootagePlanLintError("\n".join(msg_lines))
