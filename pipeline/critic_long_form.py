"""Post-rewrite contract validator for long-form scripts.

Created 2026-05-13 after the first end-to-end successful long-form
render (job 0c05c335…) shipped a script with FOUR independent contract
violations that all should have been caught at the rewrite gate:

1. **Niche tonal mismatch** — wizard picked r/nosleep niche, rewriter
   produced a 16-min self-help meditation about scrolling habits. Zero
   dread tokens in the first 200 words. The rewriter LLM's natural
   prior pulls toward brand-safe / TED-Talk content; without a hard
   contract gate the genre never lands.
2. **Length under-delivery** — wizard asked 30 min (1800s), rewriter
   labelled each section ``target_s=180`` but actually delivered ~106s
   of words per section. Final mp4 was 53% of requested length.
3. **Panel hold deadness** — every panel emitted with ``hold_s=30.0``
   (the prompt's default), so first 30 s of every video is one
   pixel-identical static frame. Verified on the rendered mp4.
4. **Stock-anecdote drift** — section 5 of the rendered video was
   literally the British Cycling marginal-gains story (a stock TED
   talking point), not the source narrative. The rewriter pads with
   familiar anecdotes when the source story is too short.

Each of these is a CLASS-OF-BUG (will keep happening on every render
that doesn't meet the contract). This module pins them as
post-rewrite assertions:

  >>> from pipeline.critic_long_form import validate_long_form_envelope
  >>> violations = validate_long_form_envelope(env, target_duration_s=1800,
  ...                                          niche="r/nosleep")
  >>> if violations:
  ...     for v in violations:
  ...         print(v.severity, v.code, v.message)

Usage in the rewriter (``pipeline/llm/rewrite_long_form.py``):

  >>> env = rewrite_long_form(...)
  >>> violations = validate_long_form_envelope(
  ...     env, target_duration_s=target_duration_s, niche=niche
  ... )
  >>> hard = [v for v in violations if v.severity == "hard"]
  >>> if hard:
  ...     # surface in retry prompt or raise to fail the render
  ...     raise LongFormContractError(hard)

Severities:

* ``hard`` — block: render MUST not proceed (would ship a bad video).
* ``soft`` — warn: log and continue (would ship a watchable video
  with a notable defect, but not catastrophic).

Severity choice is conservative — we'd rather refuse to spend $1 of
TTS + image budget than ship a video the user has to throw away.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

_logger = logging.getLogger(__name__)


# ---------- niche-tonal contract -----------------------------------------

# Per-niche "must contain at least one of" lexicon, checked against the
# first ``_NICHE_HOOK_WORDS`` words of the narration. Fail if zero
# matches found. Lowercased before comparison; regex word-boundary so
# we don't false-match "cared" → "care".
#
# Add new niches as the channel surface grows. Unknown niches → no
# tonal check (returns []), so this is opt-in per niche.
_NICHE_HOOK_WORDS = 200

NICHE_TONAL_LEXICON: dict[str, tuple[str, ...]] = {
    "r/nosleep": (
        # dread anchors
        "dread", "fear", "afraid", "terror", "scream", "shadow", "shadows",
        "darkness", "blood", "bleeding", "corpse", "dead", "death",
        "dying", "killed", "kill", "killing", "murder", "murdered",
        # presence / entity
        "thing", "creature", "entity", "figure", "voice", "voices",
        "watching", "watched", "follows", "followed", "stalking",
        "presence", "lurking", "appeared", "appears", "vanished",
        # location-anchored unease (nosleep stays grounded in spaces)
        "basement", "attic", "hallway", "doorway", "closet", "window",
        "mirror", "behind", "underneath", "inside",
        # body / physical reactions
        "trembling", "shaking", "froze", "frozen", "screamed", "shivered",
        "whispered", "cold", "chill", "wrong", "off",
        # source-anchored — these are the words a real r/nosleep post uses
        "happened", "remember", "tell", "story", "swear", "real",
        "house", "room", "night", "midnight", "alone",
    ),
    "r/amitheasshole": (
        "aita", "asshole", "wrong", "wife", "husband", "boyfriend",
        "girlfriend", "mom", "dad", "sister", "brother", "family",
        "friend", "told", "said", "called", "feel", "feeling",
        "argument", "fight", "refused", "yelled",
    ),
    "r/tifu": (
        "tifu", "fucked", "screwed", "messed", "ruined", "embarrass",
        "embarrassed", "horrible", "awful", "stupid", "idiot",
    ),
}


def _niche_lexicon_for(niche: str | None) -> tuple[str, ...]:
    """Resolve a niche string to its tonal lexicon.

    Tolerant of common surface variants ("r/nosleep", "nosleep",
    "RedditNoSleep", "reddit_nosleep"). Returns empty tuple when no
    contract is registered (=> no tonal check).
    """
    if not niche:
        return ()
    key = niche.strip().lower()
    if key in NICHE_TONAL_LEXICON:
        return NICHE_TONAL_LEXICON[key]
    # Tolerant lookups — most callers pass "nosleep" not "r/nosleep".
    bare = key.removeprefix("r/").removeprefix("reddit_").removeprefix("reddit/")
    bare = bare.replace("reddit", "").strip("_/ ")
    if f"r/{bare}" in NICHE_TONAL_LEXICON:
        return NICHE_TONAL_LEXICON[f"r/{bare}"]
    return ()


# ---------- stock-anecdote ban list --------------------------------------

# Phrases the long-form rewriter LLM tends to drop in as "filler"
# when the source story is too thin. Hit a banned phrase → reject the
# rewrite. Each pattern is checked case-insensitively against the
# concatenated narration text.
#
# Hot-list seeded by the 2026-05-13 critique (British Cycling
# marginal-gains story landed in a r/nosleep video). Add more as new
# drift patterns surface.
BANNED_STOCK_ANECDOTES: tuple[tuple[str, str], ...] = (
    ("british cycling", "British Cycling marginal-gains anecdote"),
    ("marginal gains", "marginal-gains framing"),
    ("dave brailsford", "Dave Brailsford story"),
    ("steve jobs", "Steve Jobs anecdote"),
    ("stanford commencement", "Steve Jobs Stanford speech"),
    ("roger bannister", "Roger Bannister 4-minute mile"),
    ("four-minute mile", "Roger Bannister 4-minute mile"),
    ("marshmallow test", "Stanford marshmallow test"),
    ("marshmallow experiment", "Stanford marshmallow experiment"),
    ("10,000 hour", "10,000-hour rule"),
    ("10000 hour", "10,000-hour rule"),
    ("ten thousand hour", "10,000-hour rule"),
    ("sara blakely", "Sara Blakely / Spanx anecdote"),
    ("kobe bryant", "Kobe Bryant work-ethic anecdote"),
    ("michael jordan", "Michael Jordan cut-from-team anecdote"),
    ("boiling frog", "boiling-frog metaphor"),
    ("james clear", "Atomic Habits / James Clear citation"),
    ("atomic habits", "Atomic Habits citation"),
    ("malcolm gladwell", "Malcolm Gladwell citation"),
    ("simon sinek", "Simon Sinek / Start With Why"),
    ("compound interest", "compound-interest framing"),
)


# ---------- panel hold cap ------------------------------------------------

# A long-form panel held for >12 s without animation reads as dead.
# Channel default before 2026-05-13 was 30 s — every render had 30 s
# of pixel-identical frames. Fix: cap hard at 12 s, soft-warn at 8 s.
PANEL_HOLD_HARD_MAX_S = 12.0
PANEL_HOLD_SOFT_MAX_S = 8.0


# ---------- length contract ----------------------------------------------

# Calm narrator at ~150 wpm. Targets:
#  - integrated word count must be ≥ HARD_FLOOR_FRAC of expected
#    (e.g. 0.85 * expected = "must deliver at least 85% of requested length")
#  - no individual section may be < HARD_SECTION_FLOOR_FRAC of mean
#    (catches the tired-by-the-end LLM degradation pattern observed
#    on job 0c05c335 — sections 0-3 hit 65%, sections 6-9 hit 48%)
#
# Soft floors below trigger warnings but allow the render to proceed.
HARD_FLOOR_FRAC = 0.85
SOFT_FLOOR_FRAC = 0.92
HARD_SECTION_FLOOR_FRAC = 0.55
SOFT_SECTION_FLOOR_FRAC = 0.70
WORDS_PER_MINUTE = 150


def _expected_words(target_duration_s: int | float) -> int:
    return int(float(target_duration_s) * WORDS_PER_MINUTE / 60.0)


def _word_count(text: str | None) -> int:
    if not text:
        return 0
    return len(re.findall(r"\S+", text))


# ---------- violation type ------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """One contract violation surfaced by the validator.

    code:     stable identifier the retry prompt or alert can match on
    severity: "hard" (block) or "soft" (warn)
    message:  human-readable explanation, with concrete numbers
    """
    code: str
    severity: str  # "hard" | "soft"
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.code}: {self.message}"


class LongFormContractError(Exception):
    """Raised by callers that want to hard-fail on any violation.

    Carries the list of violations on the .violations attribute so the
    worker / cron / dashboard can surface them.
    """

    def __init__(self, violations: Sequence[Violation]) -> None:
        self.violations = list(violations)
        super().__init__(
            f"long-form contract failed: "
            + "; ".join(v.message for v in violations)
        )


# ---------- individual checks ---------------------------------------------


def check_niche_contract(narration: str, niche: str | None) -> list[Violation]:
    """C1 — niche-tonal contract.

    Hard-fail if the first ``_NICHE_HOOK_WORDS`` words of the narration
    contain ZERO tokens from the niche lexicon. Soft-warn if fewer
    than 3 tokens land (means the LLM is grazing the contract, not
    honoring it).
    """
    lexicon = _niche_lexicon_for(niche)
    if not lexicon:
        return []
    words = re.findall(r"\b\w+\b", (narration or "").lower())
    head = " ".join(words[:_NICHE_HOOK_WORDS])
    if not head:
        return [Violation(
            code="niche_empty_hook",
            severity="hard",
            message=f"narration is empty; niche={niche!r} requires "
                    f"opening tonal anchor",
        )]
    pattern = r"\b(" + "|".join(re.escape(t) for t in lexicon) + r")\b"
    matches = re.findall(pattern, head)
    if not matches:
        sample = ", ".join(lexicon[:8])
        return [Violation(
            code="niche_tonal_violation",
            severity="hard",
            message=(
                f"first {_NICHE_HOOK_WORDS} words of narration contain ZERO "
                f"tokens from {niche!r} lexicon — the genre contract is "
                f"violated. Need at least one of: {sample}, … "
                f"(see pipeline.critic_long_form.NICHE_TONAL_LEXICON)"
            ),
        )]
    if len(set(matches)) < 3:
        return [Violation(
            code="niche_tonal_thin",
            severity="soft",
            message=(
                f"first {_NICHE_HOOK_WORDS} words of narration contain only "
                f"{len(set(matches))} unique tokens from {niche!r} lexicon "
                f"({list(set(matches))}). LLM is grazing the contract, not "
                f"honoring it — consider tightening hook"
            ),
        )]
    return []


def check_word_count(
    sections: Sequence[Any],
    target_duration_s: int | float,
) -> list[Violation]:
    """C2/C3/C4 — length disrespect + per-section degradation.

    Hard-fail if total word count is < HARD_FLOOR_FRAC * expected_words.
    Hard-fail if any section's word count is < HARD_SECTION_FLOOR_FRAC
    of the mean (catches the tired-by-the-end LLM pattern).
    Soft-warn at the SOFT thresholds.
    """
    if not sections:
        return [Violation(
            code="length_no_sections",
            severity="hard",
            message="rewrite produced zero sections; cannot validate length",
        )]
    expected = _expected_words(target_duration_s)
    if expected <= 0:
        return []
    counts = [_word_count(_section_text(s)) for s in sections]
    total = sum(counts)
    mean = total / len(counts)
    violations: list[Violation] = []
    if total < HARD_FLOOR_FRAC * expected:
        violations.append(Violation(
            code="length_under_delivered_hard",
            severity="hard",
            message=(
                f"narration delivered {total} words, expected ≥{int(HARD_FLOOR_FRAC * expected)} "
                f"({int(target_duration_s)}s @ 150wpm = {expected} words target). "
                f"Delivery: {total/expected:.0%}. The rewrite under-delivered "
                f"badly enough to ship a much shorter video than requested."
            ),
        ))
    elif total < SOFT_FLOOR_FRAC * expected:
        violations.append(Violation(
            code="length_under_delivered_soft",
            severity="soft",
            message=(
                f"narration delivered {total} words ({total/expected:.0%} of "
                f"{expected} target). Watchable but shorter than promised."
            ),
        ))
    if mean > 0:
        worst_idx, worst_words = min(enumerate(counts), key=lambda x: x[1])
        worst_frac = worst_words / mean
        if worst_frac < HARD_SECTION_FLOOR_FRAC:
            violations.append(Violation(
                code="section_degradation_hard",
                severity="hard",
                message=(
                    f"section {worst_idx} contains {worst_words} words; mean "
                    f"section is {mean:.0f} words — that's {worst_frac:.0%} "
                    f"of mean. The LLM degraded mid-rewrite (tired-by-the-end "
                    f"pattern). Re-rewrite with explicit per-section minimum."
                ),
            ))
        elif worst_frac < SOFT_SECTION_FLOOR_FRAC:
            violations.append(Violation(
                code="section_degradation_soft",
                severity="soft",
                message=(
                    f"section {worst_idx} is {worst_frac:.0%} of mean — "
                    f"some pacing imbalance"
                ),
            ))
    return violations


def check_panel_holds(panels: Sequence[Any]) -> list[Violation]:
    """C5 — panel hold cap.

    Hard-fail if any panel.hold_s > PANEL_HOLD_HARD_MAX_S (12 s).
    Soft-warn if any panel.hold_s > PANEL_HOLD_SOFT_MAX_S (8 s).

    Note: this validates the PANEL emission. The renderer also caps
    per-panel hold at compose time as a defense-in-depth gate — but
    we want the rewriter to learn to emit short holds, not paper over
    long ones.
    """
    if not panels:
        return []
    holds = [_panel_hold(p) for p in panels]
    over_hard = [(i, h) for i, h in enumerate(holds) if h > PANEL_HOLD_HARD_MAX_S]
    if over_hard:
        return [Violation(
            code="panel_hold_too_long",
            severity="hard",
            message=(
                f"{len(over_hard)} panel(s) have hold_s > {PANEL_HOLD_HARD_MAX_S}s "
                f"(worst: panel {over_hard[0][0]} = {over_hard[0][1]:.1f}s). "
                f"Long-form panels held >12s without movement read as dead. "
                f"Cap at {PANEL_HOLD_SOFT_MAX_S}s and emit more panels."
            ),
        )]
    over_soft = [(i, h) for i, h in enumerate(holds) if h > PANEL_HOLD_SOFT_MAX_S]
    if over_soft:
        return [Violation(
            code="panel_hold_borderline",
            severity="soft",
            message=(
                f"{len(over_soft)} panel(s) hold longer than {PANEL_HOLD_SOFT_MAX_S}s; "
                f"emit more density for tighter pacing"
            ),
        )]
    return []


def check_no_stock_anecdotes(narration: str) -> list[Violation]:
    """C6 — stock-anecdote drift ban.

    Hard-fail if any banned phrase from BANNED_STOCK_ANECDOTES appears
    in the narration. Each phrase is checked case-insensitively. Reject
    rather than soft-warn because these almost always indicate the
    rewriter wandered off the source.
    """
    if not narration:
        return []
    text = narration.lower()
    hits: list[str] = []
    for phrase, label in BANNED_STOCK_ANECDOTES:
        if phrase in text:
            hits.append(label)
    if hits:
        return [Violation(
            code="stock_anecdote_drift",
            severity="hard",
            message=(
                f"narration contains banned stock anecdote(s): "
                f"{'; '.join(hits)}. The rewriter has drifted off-source — "
                f"reject and re-rewrite from raw_story.body only."
            ),
        )]
    return []


# ---------- top-level validator ------------------------------------------


def validate_long_form_envelope(
    envelope: Any,
    *,
    target_duration_s: int | float,
    niche: str | None = None,
) -> list[Violation]:
    """Run every check against a long-form ``ScriptEnvelope``.

    Returns a flat list of all violations found (both hard and soft).
    Caller decides how to act: surface to retry prompt, raise
    LongFormContractError, log + proceed, etc.

    Tolerant of multiple envelope shapes:
    * ``ScriptEnvelope`` with ``.long_form`` populated (new path)
    * dict with ``narration`` + ``sections`` + ``panels`` keys (legacy
      narration JSON shape on disk)
    """
    long_form = _resolve_long_form(envelope)
    if long_form is None:
        return [Violation(
            code="missing_long_form_payload",
            severity="hard",
            message=f"envelope of type {type(envelope).__name__} has no long-form payload",
        )]
    sections = _resolve_sections(long_form)
    panels = _resolve_panels(long_form)
    narration = _flat_narration(long_form, sections)
    out: list[Violation] = []
    out.extend(check_niche_contract(narration, niche))
    out.extend(check_word_count(sections, target_duration_s))
    out.extend(check_panel_holds(panels))
    out.extend(check_no_stock_anecdotes(narration))
    if out:
        _logger.warning(
            "validate_long_form_envelope: %d violations (%d hard, %d soft)",
            len(out),
            sum(1 for v in out if v.severity == "hard"),
            sum(1 for v in out if v.severity == "soft"),
        )
    return out


def render_violations_for_retry_prompt(violations: Iterable[Violation]) -> str:
    """Format violations as a block to inject into the rewriter retry prompt.

    Each line is prefixed with `- ` so it can be appended as a bullet
    list to the existing CRAFT RULES.
    """
    items = list(violations)
    if not items:
        return ""
    lines = ["The previous rewrite violated these contract rules — fix all of them:"]
    for v in items:
        lines.append(f"- ({v.code}) {v.message}")
    return "\n".join(lines)


# ---------- shape helpers (private) --------------------------------------


def _resolve_long_form(envelope: Any) -> Any | None:
    """Pull the long-form payload from either a ScriptEnvelope or a
    legacy narration dict."""
    if envelope is None:
        return None
    lf = getattr(envelope, "long_form", None)
    if lf is not None:
        return lf
    if isinstance(envelope, dict):
        if "long_form" in envelope and isinstance(envelope["long_form"], dict):
            return envelope["long_form"]
        # Legacy narration JSON: top-level has narration + sections + panels
        if "narration" in envelope or "sections" in envelope or "panels" in envelope:
            return envelope
    return None


def _resolve_sections(long_form: Any) -> list[Any]:
    secs = getattr(long_form, "sections", None)
    if secs is not None:
        return list(secs)
    if isinstance(long_form, dict):
        return list(long_form.get("sections") or [])
    return []


def _resolve_panels(long_form: Any) -> list[Any]:
    panels = getattr(long_form, "panels", None)
    if panels is not None:
        return list(panels)
    if isinstance(long_form, dict):
        return list(long_form.get("panels") or [])
    return []


def _section_text(section: Any) -> str:
    if section is None:
        return ""
    text = getattr(section, "narration", None)
    if text is not None:
        return str(text)
    if isinstance(section, dict):
        return str(section.get("narration") or section.get("text") or "")
    return ""


def _panel_hold(panel: Any) -> float:
    h = getattr(panel, "hold_s", None)
    if h is not None:
        try:
            return float(h)
        except (TypeError, ValueError):
            return 0.0
    if isinstance(panel, dict):
        try:
            return float(panel.get("hold_s") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _flat_narration(long_form: Any, sections: Sequence[Any]) -> str:
    """Best-effort concatenation of narration text across the envelope.

    Prefers an explicit ``narration_flat`` field if present (some
    legacy paths cache one), else joins per-section narration."""
    flat = getattr(long_form, "narration_flat", None)
    if flat is None and isinstance(long_form, dict):
        flat = long_form.get("narration_flat") or long_form.get("narration")
    if isinstance(flat, str) and flat.strip():
        return flat
    pieces = [_section_text(s) for s in sections]
    return "\n\n".join(p for p in pieces if p)


__all__ = [
    "BANNED_STOCK_ANECDOTES",
    "HARD_FLOOR_FRAC",
    "HARD_SECTION_FLOOR_FRAC",
    "LongFormContractError",
    "NICHE_TONAL_LEXICON",
    "PANEL_HOLD_HARD_MAX_S",
    "PANEL_HOLD_SOFT_MAX_S",
    "SOFT_FLOOR_FRAC",
    "SOFT_SECTION_FLOOR_FRAC",
    "Violation",
    "WORDS_PER_MINUTE",
    "check_niche_contract",
    "check_no_stock_anecdotes",
    "check_panel_holds",
    "check_word_count",
    "render_violations_for_retry_prompt",
    "validate_long_form_envelope",
]
