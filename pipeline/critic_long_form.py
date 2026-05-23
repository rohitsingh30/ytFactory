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
import os
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


# 2026-05-14 audit add-on: stock essay-style openers that LLMs lean
# on when the source material is sparse and they have nothing
# specific to say. Two of the 5 long-forms in the audit window
# (mystoriesanimated/0c05c335 + 1b5002ec — both r/nosleep "If you
# can see this..." renders) opened with these meditation-essay
# patterns instead of the actual horror story. The rewriter learnt
# to fall back to a TED-Talk meditation when the source post is
# too short for a 30-min expansion.
#
# Soft-warn (not hard-fail) — there are legitimate philosophical
# topics that legitimately open this way; a soft signal lets the
# operator notice + retry without blocking content that's actually
# meditation by design.
ESSAY_DRIFT_OPENERS: tuple[tuple[str, str], ...] = (
    ("imagine a completely ordinary", "stock 'imagine an ordinary day' opener"),
    ("imagine an ordinary", "stock 'imagine an ordinary day' opener"),
    ("right now, wherever you are", "stock 'right now wherever you are' opener"),
    ("a person sits somewhere", "stock 'a person sits somewhere' framing"),
    ("what if i told you", "stock 'what if I told you' lecture opener"),
    ("scrolling through your phone right now", "stock 'scrolling through your phone' opener"),
    ("attention is the new currency", "stock 'attention is currency' essay framing"),
    ("the currency of attention", "stock 'currency of attention' essay framing"),
    ("we live in a world where", "stock TED-talk 'we live in a world' opener"),
    ("there's a moment in everyone's life", "stock 'moment in everyone's life' opener"),
)


# ---------- panel hold cap ------------------------------------------------

# Hard/soft caps on AUTHORED ``panel.hold_s`` — the LLM-emitted value
# before the renderer's :func:`pipeline.render.shared.long_form_lib.
# _adjust_panel_holds_to_dur` pads/scales holds to match narration
# duration. The post-stretch hold can legitimately exceed these caps
# (and the renderer does NOT re-validate); these are sanity gates on
# the LLM not emitting absurd authored values like ``hold_s=300``
# that suggest the model misunderstood the prompt.
#
# 2026-05-23: bumped from 12s/8s to 60s/45s. Rationale:
#   * The renderer produces static stills with hard cuts (see
#     pipeline/render/shared/long_form_lib.py::_assemble_panel_static).
#     With hard cuts between stills the empirical hard floor on hold
#     is ~45s (research: panel_pacing_research_2026-05.md, citing
#     YouTube retention dips + Bordwell ASL ranges).
#   * The pipeline-side compensation is denser panel cadence
#     (channel YAML's ``long_form.panel_seconds_target`` defaults to
#     25s); with that target hit, post-stretch holds land at ~20-30s
#     and stay well inside the new soft cap.
#   * The hard 60s cap still catches LLM outputs that ignore the
#     prompt and emit one hold_s=600 panel for the whole video.
PANEL_HOLD_HARD_MAX_S = 60.0
PANEL_HOLD_SOFT_MAX_S = 45.0


# ---------- length contract ----------------------------------------------

# Calm narrator at ~150 wpm. Per-section gate at ±10% of section
# target; total gate at ±15% of user's total target.
#
# 2026-05-22 (P3.2 / Q59) — tightened from the 2026-05-13 calibration
# (HARD_FLOOR_FRAC=0.50, HARD_SECTION_FLOOR_FRAC=0.40 — both
# symmetric-only on the LOW side, both too loose). The earlier
# rationale (single-shot output couldn't reliably hit higher floors)
# is obsolete now that the STORM-pattern fan-out gives each section
# call its own ~700-token budget. With the per-section gates working
# correctly, total length naturally lands inside ±15%; the gate is a
# repair trigger that the iterative-extend retry resolves.
#
# Gate philosophy (Q51/Q64): gates STAY. They are repair triggers,
# not termination signals. Gate fires → retry the failing piece
# (per-section iterative-extend in P3.6). The retry IS the fix.
#
# Override via env without a code edit:
#   YTFACTORY_LONG_FORM_TOTAL_TOL_FRAC      (default 0.15 = ±15%)
#   YTFACTORY_LONG_FORM_SECTION_TOL_FRAC    (default 0.10 = ±10%)
TOTAL_TOLERANCE_FRAC = float(
    os.environ.get("YTFACTORY_LONG_FORM_TOTAL_TOL_FRAC", "0.15")
)
SECTION_TOLERANCE_FRAC = float(
    os.environ.get("YTFACTORY_LONG_FORM_SECTION_TOL_FRAC", "0.10")
)
# Soft-warn floor for total length — lighter band than the hard gate
# so dashboards can surface borderline renders without blocking them.
# Soft band is total_tolerance_frac doubled (e.g. ±15% hard → ±30% soft).
SOFT_TOTAL_TOLERANCE_FRAC = float(
    os.environ.get("YTFACTORY_LONG_FORM_SOFT_TOTAL_TOL_FRAC",
                   str(TOTAL_TOLERANCE_FRAC * 2.0))
)
# Back-compat shims — old callers (tests, dashboards) may still
# reference these names. Derive from the new tolerance constants so
# there's one source of truth.
HARD_FLOOR_FRAC = max(0.0, 1.0 - TOTAL_TOLERANCE_FRAC)
SOFT_FLOOR_FRAC = max(0.0, 1.0 - SOFT_TOTAL_TOLERANCE_FRAC)
HARD_SECTION_FLOOR_FRAC = max(0.0, 1.0 - SECTION_TOLERANCE_FRAC)
SOFT_SECTION_FLOOR_FRAC = max(0.0, 1.0 - SECTION_TOLERANCE_FRAC * 1.5)
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
    """C2/C3/C4 — length disrespect + per-section length gate.

    Gates (P3.2 / Q59):
    * **Total** narration must be within ±TOTAL_TOLERANCE_FRAC (±15%
      default) of user's target. Outside that band → hard.
    * **Per-section** word count must be within
      ±SECTION_TOLERANCE_FRAC (±10% default) of THAT section's
      ``target_words`` (when the outline emitted one). Falls back to
      the mean-of-sections target when target_words is absent so
      pre-2026-05-22 envelopes still validate.

    Soft warnings fire at doubled band widths (±30% total, ±15%
    per-section) so dashboards can flag borderline renders without
    blocking them.

    Returns multiple per-section violations when several sections
    miss the band (each is a separate retry target).
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
    mean = total / len(counts) if counts else 0.0
    violations: list[Violation] = []

    # ---- total length gate (symmetric ±TOTAL_TOLERANCE_FRAC) -----------
    total_low = (1.0 - TOTAL_TOLERANCE_FRAC) * expected
    total_high = (1.0 + TOTAL_TOLERANCE_FRAC) * expected
    soft_low = (1.0 - SOFT_TOTAL_TOLERANCE_FRAC) * expected
    soft_high = (1.0 + SOFT_TOTAL_TOLERANCE_FRAC) * expected
    if total < total_low or total > total_high:
        # Distinguish under vs over so the retry layer can react
        # specifically (under → iterative-extend; over → tighten).
        code = ("length_under_delivered_hard" if total < total_low
                else "length_over_delivered_hard")
        violations.append(Violation(
            code=code,
            severity="hard",
            message=(
                f"narration delivered {total} words; target {expected} "
                f"({int(target_duration_s)}s @ 150wpm), allowed band "
                f"{int(total_low)}–{int(total_high)} (±{TOTAL_TOLERANCE_FRAC:.0%}). "
                f"Delivery: {total/expected:.0%}."
            ),
        ))
    elif total < soft_low or total > soft_high:
        code = ("length_under_delivered_soft" if total < soft_low
                else "length_over_delivered_soft")
        violations.append(Violation(
            code=code,
            severity="soft",
            message=(
                f"narration delivered {total} words "
                f"({total/expected:.0%} of {expected} target); "
                f"outside soft band ±{SOFT_TOTAL_TOLERANCE_FRAC:.0%}."
            ),
        ))

    # ---- per-section length gate (symmetric ±SECTION_TOLERANCE_FRAC) ---
    for idx, (section, actual) in enumerate(zip(sections, counts)):
        # Prefer section-authored target_words (outline-driven); fall
        # back to mean when absent (pre-P3.2 envelopes).
        sec_target_raw = _section_target_words(section)
        if sec_target_raw and sec_target_raw > 0:
            sec_target = float(sec_target_raw)
        elif mean > 0:
            sec_target = mean
        else:
            continue
        sec_low = (1.0 - SECTION_TOLERANCE_FRAC) * sec_target
        sec_high = (1.0 + SECTION_TOLERANCE_FRAC) * sec_target
        sec_soft_low = (1.0 - SECTION_TOLERANCE_FRAC * 1.5) * sec_target
        sec_soft_high = (1.0 + SECTION_TOLERANCE_FRAC * 1.5) * sec_target
        if actual < sec_low or actual > sec_high:
            violations.append(Violation(
                code="section_degradation_hard",
                severity="hard",
                message=(
                    f"section {idx} contains {actual} words; target "
                    f"{int(sec_target)}, allowed band "
                    f"{int(sec_low)}–{int(sec_high)} "
                    f"(±{SECTION_TOLERANCE_FRAC:.0%}). "
                    f"Re-prompt with iterative-extend to land in band."
                ),
            ))
        elif actual < sec_soft_low or actual > sec_soft_high:
            violations.append(Violation(
                code="section_degradation_soft",
                severity="soft",
                message=(
                    f"section {idx} is {actual} words vs target "
                    f"{int(sec_target)} — outside soft band "
                    f"±{SECTION_TOLERANCE_FRAC * 1.5:.0%}"
                ),
            ))
    return violations


def _section_target_words(section: Any) -> int | None:
    """Pull `target_words` from either a LongFormSection or a legacy dict.

    Defensive about every observed shape; returns None when absent so
    the caller can fall back to mean-of-sections (pre-P3.2 envelopes).
    """
    if section is None:
        return None
    tw = getattr(section, "target_words", None)
    if tw is None and isinstance(section, dict):
        tw = section.get("target_words")
    if tw is None:
        return None
    try:
        return int(tw)
    except (TypeError, ValueError):
        return None


def check_panel_holds(panels: Sequence[Any]) -> list[Violation]:
    """C5 — panel hold cap (AUTHORED holds only).

    Hard-fail if any panel.hold_s > ``PANEL_HOLD_HARD_MAX_S`` (60 s).
    Soft-warn if any panel.hold_s > ``PANEL_HOLD_SOFT_MAX_S`` (45 s).

    Note: this validates the AUTHORED ``hold_s`` value (LLM emission),
    not the post-stretch value the renderer uses. The renderer's
    ``_adjust_panel_holds_to_dur`` pads/scales holds proportionally to
    fill narration — by design the post-stretch hold can exceed these
    caps. The cap exists to catch model misunderstandings like one
    ``hold_s=600`` panel for the whole video; tighter empirical
    cadence shaping happens via channel YAML's
    ``long_form.panel_seconds_target``.
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
                f"Authored holds >{PANEL_HOLD_HARD_MAX_S}s suggest the LLM "
                f"misunderstood the prompt — emit more panels at shorter "
                f"holds (target {PANEL_HOLD_SOFT_MAX_S}s)."
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


def check_no_essay_drift(narration: str) -> list[Violation]:
    """C7 — meditation-essay opener drift (added 2026-05-14).

    Soft-warn if the narration's opening contains any phrase from
    ``ESSAY_DRIFT_OPENERS``. These are stock LLM crutches the
    rewriter falls back to when the source material is too sparse
    to fill the requested length — it pads with TED-Talk-style
    meditation instead of admitting the source can't carry the
    duration.

    Caught by the 2026-05-13 audit on the r/nosleep 'If you can see
    this' renders — both shipped 16-23 minute meditation essays
    completely unrelated to the actual horror post they were
    nominally based on.

    Soft (not hard) because a topic that GENUINELY warrants a
    philosophical opener (e.g. 'the philosophy of free will' on a
    cosmosdecoded long-form) shouldn't be blocked. Operator sees
    the warning and decides whether to re-prompt with a stricter
    source-fidelity instruction.
    """
    if not narration:
        return []
    # Check the FIRST 300 characters — opener-only signal. A later
    # mention of "imagine an ordinary day" mid-narration is fine
    # rhetorical scaffolding.
    opener = narration[:300].lower()
    hits: list[str] = []
    for phrase, label in ESSAY_DRIFT_OPENERS:
        if phrase in opener:
            hits.append(label)
    if hits:
        return [Violation(
            code="essay_drift_opener",
            severity="soft",
            message=(
                f"narration opens with stock essay-drift phrase(s): "
                f"{'; '.join(hits)}. The rewriter may have padded a "
                f"sparse source with TED-Talk meditation. Verify "
                f"narration is actually grounded in the source."
            ),
        )]
    return []


def _extract_anchor_words(text: str, *, min_len: int = 4) -> set[str]:
    """Pull lowercase content-words from ``text`` that are likely
    entity/topic anchors.

    Heuristic: split on whitespace + punctuation, lowercase, keep
    tokens of length ≥ ``min_len`` that are NOT stop-words. Numbers
    are kept (years, counts, proper-noun-like digits).

    Used by :func:`check_source_fidelity` as a poor-man's NER. Avoids
    bringing in spaCy / NLTK as a hard dep.
    """
    if not text:
        return set()
    # Strip basic punctuation + lowercase + split.
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    tokens = cleaned.split()
    return {
        t for t in tokens
        if len(t) >= min_len and t not in _STOP_WORDS
    }


# Stop-words to exclude from the anchor set — top-frequency English
# function words that would dominate any overlap calculation. Not
# exhaustive (no NLP lib) — enough to filter the worst noise.
_STOP_WORDS: frozenset[str] = frozenset({
    "about", "after", "again", "against", "around", "because",
    "before", "being", "between", "could", "doing", "down",
    "during", "every", "from", "have", "having", "into",
    "more", "most", "much", "never", "other", "should", "since",
    "some", "still", "such", "than", "that", "their", "them",
    "then", "there", "these", "they", "this", "those", "through",
    "under", "until", "very", "what", "when", "where", "which",
    "while", "with", "would", "your", "just", "like", "into",
    "only", "over", "even", "also", "back", "down", "your",
    "yourself", "ourselves", "themselves",
})


def check_source_fidelity(
    narration: str, raw_body: str | None,
    *, min_overlap_frac: float = 0.30,
    min_source_words: int = 30,
) -> list[Violation]:
    """C8 — source-fidelity check (added 2026-05-14).

    Soft-warn when fewer than ``min_overlap_frac`` of the anchor
    words from ``raw_body`` appear in ``narration``. Indicates the
    rewriter has wandered off the source material.

    Skipped when:
      * ``raw_body`` is None or blank (LLM-generated topic, no
        source to compare against).
      * Source body is shorter than ``min_source_words`` content
        words (too sparse to compute meaningful overlap — would
        produce false positives).

    The 30% threshold is conservative; legitimate creative
    expansion typically retains ≥ 50% of the source's anchor
    words even when paraphrased heavily. The audit case
    (r/nosleep 'If you can see this' rendered as 'attention is
    currency' essay) had ZERO overlap with the source post's
    actual entities (the chain message, the recipient, the
    implied warning).

    Soft severity — operator judgment. A creative-fiction long-form
    might intentionally diverge from a sparse prompt; we don't want
    a hard block on every render.
    """
    if not narration or not raw_body:
        return []
    source_anchors = _extract_anchor_words(raw_body)
    if len(source_anchors) < min_source_words:
        # Source too sparse to compute meaningful overlap.
        return []
    narration_anchors = _extract_anchor_words(narration)
    overlap = source_anchors & narration_anchors
    overlap_frac = len(overlap) / len(source_anchors)
    if overlap_frac < min_overlap_frac:
        sample_missing = sorted(source_anchors - narration_anchors)[:8]
        return [Violation(
            code="source_fidelity_low",
            severity="soft",
            message=(
                f"narration overlaps only {len(overlap)}/{len(source_anchors)} "
                f"({overlap_frac:.0%}) of source anchor words; rewriter may "
                f"have wandered off-source. Min expected: "
                f"{min_overlap_frac:.0%}. Sample missing terms: "
                f"{', '.join(sample_missing) if sample_missing else '(none)'}."
            ),
        )]
    return []


# ---------- top-level validator ------------------------------------------


def validate_long_form_envelope(
    envelope: Any,
    *,
    target_duration_s: int | float,
    niche: str | None = None,
    raw_body: str | None = None,
) -> list[Violation]:
    """Run every check against a long-form ``ScriptEnvelope``.

    Returns a flat list of all violations found (both hard and soft).
    Caller decides how to act: surface to retry prompt, raise
    LongFormContractError, log + proceed, etc.

    Tolerant of multiple envelope shapes:
    * ``ScriptEnvelope`` with ``.long_form`` populated (new path)
    * dict with ``narration`` + ``sections`` + ``panels`` keys (legacy
      narration JSON shape on disk)

    Args:
        raw_body: Source post body text (e.g. ``raw_story["body"]``).
            When provided, enables ``check_source_fidelity``. Pass
            None for LLM-only sources where there's no body to
            compare against.
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
    out.extend(check_no_essay_drift(narration))
    out.extend(check_source_fidelity(narration, raw_body))
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
    "SECTION_TOLERANCE_FRAC",
    "SOFT_FLOOR_FRAC",
    "SOFT_SECTION_FLOOR_FRAC",
    "SOFT_TOTAL_TOLERANCE_FRAC",
    "TOTAL_TOLERANCE_FRAC",
    "Violation",
    "WORDS_PER_MINUTE",
    "check_niche_contract",
    "check_no_stock_anecdotes",
    "check_panel_holds",
    "check_word_count",
    "render_violations_for_retry_prompt",
    "validate_long_form_envelope",
]
