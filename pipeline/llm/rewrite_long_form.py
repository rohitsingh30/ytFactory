"""Long-form rewriter — STORM-pattern two-phase generation.

Inspired by Stanford STORM (NAACL 2024;
https://github.com/stanford-oval/storm). Replaces the pre-2026-05-13
single-shot 12-15k-token rewrite call (which kept hitting the model's
real-world reliable-output cliff at ~10-12k tokens, producing
truncated JSON / silent stops / KeyError-on-partial-section thrash)
with a two-phase generation:

  Phase 1 — OUTLINE (one ~1k-token call).
    Returns the spine: hook, thesis, section stubs (id+title+brief+
    target_words), panel briefs (scene+hold_s).

  Phase 2 — SECTION BODIES (N parallel calls, each ~700 tokens).
    For each section stub, a separate LLM call generates ONLY that
    section's narration body. Run via ThreadPoolExecutor(max_workers=5)
    so a 10-section render takes ~30s instead of 5-7 min.

  Phase 3 — AGGREGATE.
    Stitch outline + section bodies into a LongFormScript, run the
    full ``pipeline.critic_long_form.validate_long_form_envelope``
    contract, return the ScriptEnvelope.

This is literally STORM's "writing stage" applied to our LongFormScript
shape — see ``stanford-oval/storm/knowledge_storm/storm_wiki/modules/
article_generation.py`` for the canonical reference. STORM's
"pre-writing" stage (perspectives + retrieval over rich source
material) is deferred to Phase 1 of the analysis doc
(``docs/long_form_pipeline_analysis_2026-05-13.md``) — it lands with
the RawDoc migration. For now the outline call uses the existing thin
``raw_story.body`` as context (same source as today, just sliced
differently).

Why this works where the single-shot pattern doesn't:

* Each call's output budget is well within the model's reliable
  range (~700 tokens vs ~12-15k). No truncation.
* Failures are per-section, not per-render. One bad section call
  retries cheaply (~2k tokens) while the others succeed.
* Validator fires once at the end on a complete envelope, not
  mid-stream on partial output.
* Eliminates the ``_salvage_truncated_json`` partial-dict path
  entirely (Fix B retires it from the dispatcher too).

Output shape: :class:`pipeline.llm.script_schema.ScriptEnvelope` with
``kind="long_form"`` and a fully-populated
:class:`pipeline.llm.script_schema.LongFormScript` payload.

Routes through the same backend dispatcher every other rewriter uses
(``pipeline.llm.cli.call_claude_cli`` → Azure OpenAI in cloud,
``claude`` CLI on laptop).
"""
from __future__ import annotations

import concurrent.futures
import logging
import math
import re
from typing import Any

from . import cli as _llm
from .script_schema import (
    LongFormPanel,
    LongFormScript,
    LongFormSection,
    ScriptEnvelope,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 1 — outline prompt + schema
# ---------------------------------------------------------------------------

_OUTLINE_PROMPT_TEMPLATE = """\
You are the OUTLINE agent for a long-form YouTube narration. Your job
is the spine — NOT the body. The body comes in a separate call per
section, so keep this output focused on STRUCTURE.

Channel context (the visual aesthetic + narrator persona this video
will be produced in):
{channel_context}

Topic: {topic}

User's notes / raw source material (use this as the source of truth;
expand only with reasonable creative inferences if sparse):
{notes}

Target duration: {duration_min} minutes ({duration_s} seconds total).

OUTPUT — return ONE JSON object with EXACTLY these keys:

  hook            — string (30-60 s of opening narration). The
                    listener should feel "I have to know how this
                    ends" by the end of the hook. Write the actual
                    spoken words, not a description.

  thesis          — string, ONE sentence (≤ 25 words). The single
                    claim or question the whole video proves /
                    explores. Surfaced in the description; NOT
                    narrated verbatim.

  sections        — array of {section_count_target} section STUBS
                    (one per ~3 minutes of narration). Each stub is
                    {{id, title, brief, target_words, visual_brief,
                    quality_goal}}:
                      id           — kebab-case slug, unique
                      title        — short chapter label (≤ 80 chars)
                      brief        — 2-3 sentence summary of WHAT
                                     this section establishes /
                                     reveals / pivots on. Used by
                                     the per-section body call as
                                     the directorial intent for that
                                     chapter. Not narrated.
                      target_words — int, soft target word count for
                                     the body call (typical
                                     ~{section_words_target}).
                      visual_brief — ONE sentence describing what the
                                     camera / illustration should
                                     show for THIS section.
                      quality_goal — ONE short phrase capturing what
                                     GOOD looks like for THIS section
                                     specifically — what emotional /
                                     narrative beat it must land
                                     ("escalate tension", "reveal
                                     the twist", "humanise the
                                     victim", "deliver the punchline",
                                     "set the stakes concretely").
                                     Authored per-section, not
                                     templated — this becomes a
                                     POSITIVE specification handed
                                     to the body-writer LLM so it
                                     knows what to AIM for, not what
                                     to avoid.

  panel_briefs    — array of {{scene, hold_s, after_section_id}}
                    panel cues. Produce EXACTLY {panel_count_target}
                    panel_briefs TOTAL across the whole video
                    (one image per ~{panel_seconds_target} seconds of
                    narration — empirically tuned for Ken-Burns
                    animated-stills retention; see
                    docs/panel_pacing_research_2026-05.md). Distribute
                    them roughly evenly across sections — aim for
                    {panel_min_per_section}+ panels per section so no
                    section sits on one image.

                    scene is a detailed image-gen prompt (the
                    channel's image_style_prefix is prepended at
                    render time — DON'T repeat aesthetic
                    instructions like "flat 2D crayon". Just describe
                    WHAT is in the frame: subject, action, props,
                    mood, composition).

                    hold_s ≤ {panel_hold_max_s} (panels held longer
                    read as DEAD on screen — but the renderer will
                    stretch holds uniformly to fill narration; aim
                    for hold_s in the {panel_seconds_target}±5s
                    range so post-stretch holds stay tolerable).

                    after_section_id pins which section this panel
                    accompanies — must match one of the section ids
                    above. Panels are visualised in their order
                    within the section.

  sources         — array of strings. Optional — URLs / refs the
                    video draws from (cited in the description).
                    Empty array if no specific sources.

  title_options   — array of 3-5 candidate YouTube titles. The first
                    is used; the rest are A/B alternates.
{niche_title_rules}

CRAFT RULES (NON-NEGOTIABLE — applied to BOTH outline and body):

- Each section's BRIEF must end on a small open loop (a question, a
  tension, a "what they didn't know was…") so the body call can
  carry that thread. Long-form viewers ABANDON at section
  boundaries; the open loop is what holds them.

- VARY pacing across sections in the brief (which sections lean
  long-and-explanatory vs. short-and-punchy). The body calls will
  honor this.

- Concrete > abstract. EVERY section brief should anchor in at
  least one specific detail (a number, a date, a place, a named
  person, a named object, a specific quote).

- DON'T repeat across sections. If section 3 establishes a fact,
  don't re-establish it in section 6 — the body calls trust prior
  context.

- SOURCE FIDELITY (HARD). The outline and the bodies MUST be
  traceable to the user's raw source material above. Do NOT
  introduce stock anecdotes — the post-rewrite validator hard-rejects
  any narration containing the following BANNED phrases:
    * British Cycling / "marginal gains" / Dave Brailsford
    * Steve Jobs / Stanford commencement speech
    * Roger Bannister / four-minute mile
    * Stanford marshmallow test / experiment
    * 10,000-hour rule
    * Sara Blakely / Spanx
    * Kobe Bryant / Michael Jordan cut-from-team
    * Boiling frog metaphor
    * Atomic Habits / James Clear / Malcolm Gladwell / Simon Sinek
    * Compound-interest framing

- PANEL SCENE RULES (CLASS-OF-BUG fix from 2026-05-13):
  * Each panel scene CONCRETE and STAGED — name subject, action,
    props, environment, lighting. Avoid abstract / metaphor scenes.
  * NO READABLE TEXT IN PANELS. If a panel features a phone screen,
    sign, book, poster, chalkboard, label, or document, append
    "AVOID all readable text, captions, words, or letters in the
    image — use abstract shapes or icons only" to the scene. The
    image model otherwise hallucinates garbled fake text.

- CHARACTER CONSISTENCY (the #1 long-form quality bug):
  * Decide ONE character spec for the protagonist (and named
    supporting cast) BEFORE writing any panel. Spec = age range,
    gender presentation, hair (length + colour), skin tone, build
    AND BODY PROPORTIONS, clothing palette, signature prop.
    Channel context above may already define a "Recurring
    character" — when present, USE THAT EXACTLY.
  * In every panel scene that features that character, repeat the
    WHOLE character spec verbatim. The image generator has no
    memory between panels — describe the protagonist as "a young
    child" in panel 2 and "an elderly man" in panel 5 → you WILL
    get a young child and an elderly man, not the same person at
    two ages.

OUTPUT: ONE JSON object matching the structure above. No prose
preface, no markdown fence. Just the object.
"""


_OUTLINE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "hook", "thesis", "sections", "panel_briefs", "sources",
        "title_options",
    ],
    "properties": {
        "hook":   {"type": "string", "minLength": 100, "maxLength": 3000},
        "thesis": {"type": "string", "minLength": 5,   "maxLength": 250},
        "sections": {
            "type": "array",
            "minItems": 3,
            "maxItems": 30,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "brief", "quality_goal"],
                "properties": {
                    "id":           {"type": "string", "minLength": 1, "maxLength": 80},
                    "title":        {"type": "string", "minLength": 1, "maxLength": 120},
                    "brief":        {"type": "string", "minLength": 30, "maxLength": 2000},
                    "target_words": {"type": ["integer", "null"]},
                    "visual_brief": {"type": ["string", "null"]},
                    # Outline-authored per-section positive spec for the
                    # body call. Replaces the old negative "Do NOT pad
                    # with filler" framing (Q61). Short phrase / clause.
                    "quality_goal": {"type": "string", "minLength": 3, "maxLength": 200},
                },
            },
        },
        "panel_briefs": {
            "type": "array",
            "minItems": 0,
            "maxItems": 80,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["scene"],
                "properties": {
                    "scene":             {"type": "string", "minLength": 20, "maxLength": 1500},
                    "hold_s":            {"type": ["number", "null"]},
                    "after_section_id":  {"type": ["string", "null"]},
                },
            },
        },
        "sources": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        },
        "title_options": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {"type": "string", "minLength": 5, "maxLength": 120},
        },
    },
}


# ---------------------------------------------------------------------------
# Phase 2 — section-body prompt + schema
# ---------------------------------------------------------------------------

_SECTION_BODY_PROMPT_TEMPLATE = """\
You are the SECTION-BODY agent for a long-form YouTube narration.

The OUTLINE agent has already authored the spine. Your job is to
write the FULL NARRATION TEXT for ONE section only, given its
position in the outline and the directorial brief.

Channel context:
{channel_context}

Overall topic: {topic}

Overall thesis (NOT narrated — for context only):
{thesis}

Outline of all sections (so you can position THIS section's body in
the broader arc — do NOT repeat material from other sections):
{outline_summary}

Source material (the user's raw notes — your narration MUST stay
traceable to this):
{notes}

YOUR SECTION ({section_id}):
  Title:        {section_title}
  Visual brief: {visual_brief}

  Quality goal for THIS section: {quality_goal}
  ★ This is the POSITIVE specification — what success looks like
    for this section specifically. Aim for it; build every sentence
    in service of it.

  Target word count: {target_words} words. Acceptable range:
  {min_words}-{max_words} words.
  ★ Length comes from substance. Anchor in concrete source material:
    specific names, dates, numbers, locations, direct quotes, sensory
    detail, cause-and-effect chains. The more grounded the section,
    the more naturally it reaches the target.
{emphasis_block}
  Directorial brief from the outline:
  {section_brief}

OUTPUT — return ONE JSON object with EXACTLY these keys:

  narration  — string. The actual prose the narrator reads. NO
               chapter prefixes ("Section 3:" / "Chapter:"). Plain
               prose. Vary sentence length naturally. End on a small
               open loop pulling viewers into the next section
               (unless this is the final section — then close with
               the arc payoff plus a binary-opinion CTA the closer
               can attach to).

  sentences  — array of strings. The same narration, split into
               individual sentences (for caption alignment). Each
               array element should be ONE complete sentence,
               roughly 5-25 words. Keep punctuation; the renderer
               uses these as caption cue boundaries.

  word_count — integer. The number of words in `narration` (split
               on whitespace). EMIT this AFTER writing the
               narration: literally count the words you just wrote
               and put the integer here. This anchors length —
               counting forces the model to attend to length while
               writing. The validator uses len(narration.split())
               as the source of truth; if your emitted count
               disagrees, that's logged as telemetry but is not
               a failure mode. What matters is that you counted.

CRAFT RULES (same as outline — applied to body):

- Open loop at end of section (unless final).
- Concrete > abstract. Anchor in source-material specifics (numbers,
  dates, places, names, quotes).
- DON'T repeat established facts from earlier sections.
- SOURCE FIDELITY: do not import stock anecdotes (British Cycling,
  Steve Jobs Stanford, Roger Bannister, marshmallow test, 10,000-hour
  rule, Sara Blakely, Kobe/Jordan, boiling frog, James Clear,
  Malcolm Gladwell, Simon Sinek, compound interest, etc).
- VARY sentence length within the section.

OUTPUT: ONE JSON object matching the structure above. No prose
preface, no markdown fence. Just the object.
"""


_SECTION_BODY_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["narration", "sentences", "word_count"],
    "properties": {
        "narration": {"type": "string", "minLength": 50},
        "sentences": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        # P3.1 / Q57.1 / Q60: LLM self-emits its own count of the
        # narration body. Anchoring mechanism — counting forces the
        # model to attend to length while writing. Validator uses the
        # ACTUAL count (len(narration.split())) as truth; emitted-vs-
        # actual delta is logged as telemetry only (no hard fail on
        # mismatch — the act of emitting IS the fix).
        "word_count": {"type": "integer", "minimum": 0},
    },
}


# ---------------------------------------------------------------------------
# Helpers (preserved from pre-2026-05-13)
# ---------------------------------------------------------------------------


def _channel_context(channel_cfg: dict[str, Any]) -> str:
    """One-paragraph description of the channel + its long-form aesthetic."""
    parts: list[str] = []
    name = channel_cfg.get("name") or channel_cfg.get("channel") or "this channel"
    parts.append(f"Channel: {name}.")

    lf = channel_cfg.get("long_form") or {}
    style = (lf.get("image_style_prefix") or
             channel_cfg.get("image_style_prefix") or "").strip()
    if style:
        parts.append("Visual aesthetic: " + " ".join(style.split()))

    char = (channel_cfg.get("character_description") or "").strip()
    if char:
        parts.append("Recurring character: " + " ".join(char.split()))

    render_mode = lf.get("render_mode") or "image_panels"
    parts.append(
        f"Long-form render mode is {render_mode!r} — "
        + ("the visualize stage will generate per-panel illustrations from "
           "the panels[] you author, so each scene must be concretely "
           "describable as a still image."
           if render_mode == "image_panels"
           else "the visualize stage will match archival footage to "
                "sections, so panels[] can be empty / sparse — focus on "
                "section narration + visual_brief hints for the footage "
                "matcher.")
    )
    return " ".join(parts)


def _planned_sections_and_panels(
    duration_s: int,
    channel_cfg: dict[str, Any] | None = None,
) -> tuple[int, int, int, int, int, int, int, int]:
    """Heuristic targets for the LLM prompt. Hint-only — the LLM may
    return any reasonable number that fits the topic.

    Returns ``(words_target, words_floor, words_ceiling,
    section_count_target, section_words_target, section_words_floor,
    panel_count_target, panel_min_per_section)``.

    Section-count vs section-size: Azure GPT-5.3 reliably emits ~700
    output tokens (~525 words) per single call before quality starts
    to degrade ("tired-by-the-end" pattern). To stay inside that
    cliff for ANY user-picked duration, we hard-cap
    ``section_words_target`` at ``SECTION_WORDS_CEILING`` and scale up
    ``section_count_target`` instead (capped at ``SECTION_COUNT_CAP``).
    A 50-min long-form will therefore land at ~16 short sections, not
    ~12 long ones — but every section will hit its floor on the first
    LLM call, and the wizard never sees a length contract failure.

    Panel cadence: reads ``channel_cfg.long_form.panel_seconds_target``
    (default 25s — one panel per ~25 s of narration, ≈2.4 PPM). Tuned
    empirically against Ken-Burns animated-stills retention research
    (see docs/panel_pacing_research_2026-05.md). Previously hardcoded
    at 7s, which over-requested 257 panels for a 30-min long-form;
    the LLM ignored that and emitted ~25, the renderer dropped that
    to 10 (one per section) — see /Users/rohit/.copilot/session-state/
    a10f7edb-7f93-4963-a06c-ba3b3df1501c/plan.md for the full story.

    The ``panel_max_count`` ceiling is enforced separately at envelope
    aggregation time (``_aggregate`` reads channel YAML); this function
    only sets the prompt-hint target.
    """
    # Calm TTS narrator @ ~150 wpm.
    words_target = int(duration_s * 150 / 60)
    words_floor = int(words_target * 0.92)
    words_ceiling = int(words_target * 1.10)
    # Per-section word ceiling — keeps each LLM call inside the
    # ~700-token reliability window of GPT-5.3 / Claude long-output.
    # Tuned 2026-05-20 after job a734babb… failed at 1042 wpm/section.
    SECTION_WORDS_CEILING = 500
    # Absolute floor for section count; absolute cap. Cap raised from
    # 12 → 24 so a 60-min long-form doesn't blow past the per-section
    # ceiling.
    SECTION_COUNT_FLOOR = 6
    SECTION_COUNT_CAP = 24
    # Start with the duration-driven count (~1 per 3 min); scale up
    # if section_words_target would exceed SECTION_WORDS_CEILING.
    section_count_target = max(SECTION_COUNT_FLOOR, max(1, duration_s // 180))
    if words_target // max(1, section_count_target) > SECTION_WORDS_CEILING:
        # Bump section count until each section fits the ceiling.
        section_count_target = max(
            section_count_target,
            math.ceil(words_target / SECTION_WORDS_CEILING),
        )
    section_count_target = min(SECTION_COUNT_CAP, section_count_target)
    section_words_target = max(50, words_target // max(1, section_count_target))
    section_words_floor = int(section_words_target * 0.70)
    # Panel cadence — single source of truth from channel YAML.
    lf_cfg = (channel_cfg or {}).get("long_form") or {}
    try:
        panel_seconds = max(8, int(lf_cfg.get("panel_seconds_target") or 25))
    except (TypeError, ValueError):
        panel_seconds = 25
    panel_count_target = max(8, max(1, duration_s // panel_seconds))
    panel_min_per_section = max(2, panel_count_target // max(1, section_count_target))
    return (
        words_target,
        words_floor,
        words_ceiling,
        section_count_target,
        section_words_target,
        section_words_floor,
        panel_count_target,
        panel_min_per_section,
    )


# Per-niche tonal anchor for title_options.
_NICHE_TITLE_RULES: dict[str, str] = {
    "r/nosleep": (
        "    * For r/nosleep: at least ONE title option MUST contain a\n"
        "      noun-anchor (room / door / hallway / call / message /\n"
        "      figure / window / photo / hospital / basement) PLUS a\n"
        "      present-tense dread verb (watching / following /\n"
        "      whispering / waiting / standing / appearing). Bad:\n"
        "      \"If You Can See This Keep Reading\". Good: \"There's\n"
        "      Something Standing in the Hallway\"."
    ),
    "r/amitheasshole": (
        "    * For r/AmItheAsshole: at least ONE title option MUST\n"
        "      start with the literal prefix \"AITA for \" — the niche\n"
        "      depends on this convention for searchability."
    ),
    "r/tifu": (
        "    * For r/TIFU: at least ONE title option MUST start with\n"
        "      the literal prefix \"TIFU by \" — the niche convention."
    ),
}


def _niche_title_rules_block(niche: str | None) -> str:
    if not niche:
        return ""
    key = niche.strip().lower()
    if key in _NICHE_TITLE_RULES:
        return "\nNICHE-SPECIFIC TITLE RULES:\n" + _NICHE_TITLE_RULES[key]
    bare = key.removeprefix("r/").removeprefix("reddit_").strip("_/ ")
    if f"r/{bare}" in _NICHE_TITLE_RULES:
        return "\nNICHE-SPECIFIC TITLE RULES:\n" + _NICHE_TITLE_RULES[f"r/{bare}"]
    return ""


def _niche_from_spec_or_cfg(spec: Any, cfg: dict[str, Any] | None) -> str | None:
    """Best-effort niche resolution — mirrors what the validator expects."""
    for src in (spec, cfg):
        if src is None:
            continue
        for attr in ("niche", "variant", "subreddit", "source_subreddit"):
            v = None
            if isinstance(src, dict):
                v = src.get(attr)
            else:
                v = getattr(src, attr, None)
            if v:
                return str(v)
    return None


def _outline_summary_for_section_call(outline: dict, current_section_id: str) -> str:
    """Render a compact summary of the outline so each section-body call
    can position itself in the arc without re-reading the source.

    Marks the current section so the LLM doesn't accidentally regenerate
    a different section's content.
    """
    lines: list[str] = []
    for s in outline.get("sections") or []:
        marker = " ← THIS SECTION" if s.get("id") == current_section_id else ""
        title = s.get("title", "?")
        brief = (s.get("brief") or "")[:120].replace("\n", " ")
        lines.append(f"  - [{s.get('id')}] {title}: {brief}{marker}")
    return "\n".join(lines) if lines else "(no other sections)"


# ---------------------------------------------------------------------------
# Source-body length cap (general — protects all prompts)
# ---------------------------------------------------------------------------

# 12000 chars ≈ 3000 input tokens — preserves enough context for the
# outline + each section call without saturating the per-prompt input
# budget. A truncated marker is appended so the LLM knows there's more
# than what it sees (and doesn't try to be "comprehensive" over a
# partial source).
_NOTES_HARD_CAP_CHARS = 12000


def _cap_notes(body: str) -> str:
    """Cap source-body length before injection into prompts.

    Scraped articles / wikipedia dumps / full subreddit thread JSON
    can run 50-200k chars. The outline prompt and EVERY section-body
    prompt embed ``notes`` verbatim, so uncapped source blows past
    input-token budgets, causing either an LLM error or a quietly
    truncated context the rewriter has to invent around.

    Truncation strategy: keep the head (where the strongest narrative
    hooks usually live), append a marker. We don't summarize because
    summarization would be a whole extra LLM call burning budget for
    every render.
    """
    if len(body) <= _NOTES_HARD_CAP_CHARS:
        return body
    head = body[:_NOTES_HARD_CAP_CHARS]
    return head + (
        f"\n\n[…truncated at {_NOTES_HARD_CAP_CHARS} chars; "
        f"{len(body) - _NOTES_HARD_CAP_CHARS} more chars in the source. "
        "Stay grounded in what you can see above.]"
    )


# ---------------------------------------------------------------------------
# Phase 1: outline call
# ---------------------------------------------------------------------------


def _call_outline_llm(
    *, channel_context: str, topic: str, notes: str,
    duration_min: int, duration_s: int,
    section_count_target: int, section_words_target: int,
    panel_count_target: int, panel_min_per_section: int,
    panel_hold_max_s: int, panel_seconds_target: int,
    niche_title_rules: str,
    extra_context: str = "", extra_rules: str = "",
) -> dict:
    """Run the OUTLINE LLM call. Returns the raw outline dict."""
    prompt = _OUTLINE_PROMPT_TEMPLATE.format(
        channel_context=channel_context + extra_context,
        topic=topic,
        notes=notes,
        duration_min=duration_min,
        duration_s=duration_s,
        section_count_target=section_count_target,
        section_words_target=section_words_target,
        panel_count_target=panel_count_target,
        panel_min_per_section=panel_min_per_section,
        panel_hold_max_s=panel_hold_max_s,
        panel_seconds_target=panel_seconds_target,
        niche_title_rules=niche_title_rules,
    ) + extra_rules

    raw = _llm.call_claude_cli(
        prompt,
        output_json=True,
        json_schema=_OUTLINE_SCHEMA,
        model=_llm.model_for("rewrite_long_form"),
        stage="rewrite_long_form_outline",
    )
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"_call_outline_llm: LLM returned {type(raw).__name__}, "
            f"expected dict (output_json=True)"
        )
    return raw


# Outline-call retry budget. The outline is the single point of
# failure for everything downstream (sections / panels / titles all
# derive from it). A transient Azure 5xx, content-filter rejection,
# or empty-sections result here used to abort the whole render.
# Two retries covers the realistic transient + one content-shape
# misfire window without compounding cost (one outline call is ~1k
# output tokens).
_OUTLINE_MAX_RETRIES = 2

# P3.3 (Q63): sum-check tolerance — if the outline's
# sum(section.target_words) is outside ±15% of the user's total
# target, retry the outline ONCE with the error appended to the
# prompt. If the 2nd outline is also out of band, hard-fail (the
# downstream length validator would catch it but at much higher cost
# — wasted N section LLM calls before failing).
_OUTLINE_SUM_TOLERANCE_FRAC = 0.15


def _outline_section_sum(outline: dict) -> int:
    """Return sum(section.target_words) for the outline, ignoring
    sections whose ``target_words`` field is missing or unparsable."""
    total = 0
    for s in outline.get("sections") or []:
        try:
            total += int(s.get("target_words") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _outline_sum_in_band(outline: dict, target_words: int) -> bool:
    """True iff ``sum(section.target_words)`` is within ±15% of
    ``target_words``. ``target_words=0`` short-circuits to True (no
    band to enforce; legacy behaviour)."""
    if target_words <= 0:
        return True
    total = _outline_section_sum(outline)
    band = _OUTLINE_SUM_TOLERANCE_FRAC * target_words
    return abs(total - target_words) <= band


def _call_outline_with_retry(
    *, channel_context: str, topic: str, notes: str,
    duration_min: int, duration_s: int,
    section_count_target: int, section_words_target: int,
    panel_count_target: int, panel_min_per_section: int,
    panel_hold_max_s: int, panel_seconds_target: int,
    niche_title_rules: str,
    extra_context: str, extra_rules: str,
    raw_story: dict, section_words_target_for_synth: int,
    section_count_target_for_synth: int,
    total_words_target: int = 0,
) -> dict:
    """Call ``_call_outline_llm`` with retry on transient failures,
    content-filter rejections, and pathologically empty results.

    After all retries are exhausted, fall back to ``_synthesize_outline``
    so the render proceeds with a structurally-valid (if thin) outline
    rather than aborting. The downstream length validator will surface
    the gap as a soft warning — the user sees a watchable video and
    a clear ``synthesized_outline_fallback`` log line in the trace.

    P3.3 (Q63): after a successful outline returns, check
    ``sum(section.target_words)`` vs ``total_words_target``. If
    outside ±15%, retry ONCE with the error appended to ``extra_rules``.
    If the second outline is also out of band, raise — two bad
    outlines in a row is a structural signal worth surfacing instead
    of wasting N section-body calls.
    """
    last_err: str | None = None
    sum_check_used = False
    for attempt in range(1 + _OUTLINE_MAX_RETRIES):
        try:
            outline = _call_outline_llm(
                channel_context=channel_context,
                topic=topic,
                notes=notes,
                duration_min=duration_min,
                duration_s=duration_s,
                section_count_target=section_count_target,
                section_words_target=section_words_target,
                panel_count_target=panel_count_target,
                panel_min_per_section=panel_min_per_section,
                panel_hold_max_s=panel_hold_max_s,
                panel_seconds_target=panel_seconds_target,
                niche_title_rules=niche_title_rules,
                extra_context=extra_context,
                extra_rules=extra_rules,
            )
        except _llm.ContentFilterError as exc:
            # Content filter is deterministic for the same prompt.
            # On retry, strip the source body (the most likely
            # filter trigger) and re-run from topic + thesis alone.
            last_err = f"ContentFilterError: {exc}"
            _logger.warning(
                "outline LLM call hit content filter (attempt %d/%d); "
                "retrying with sanitized notes",
                attempt + 1, _OUTLINE_MAX_RETRIES + 1,
            )
            notes = (
                "(source notes were filtered by Azure content moderation; "
                "author from the topic alone and infer plausible beats)"
            )
            continue
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            _logger.warning(
                "outline LLM call failed (attempt %d/%d): %s",
                attempt + 1, _OUTLINE_MAX_RETRIES + 1, last_err,
            )
            continue

        # Outline returned — but it might be structurally empty.
        sections_raw = outline.get("sections") if isinstance(outline, dict) else None
        if not sections_raw or not isinstance(sections_raw, list):
            last_err = (
                f"outline returned 0 sections "
                f"(type={type(sections_raw).__name__})"
            )
            _logger.warning(
                "outline LLM returned empty sections (attempt %d/%d); retrying",
                attempt + 1, _OUTLINE_MAX_RETRIES + 1,
            )
            continue

        # P3.3 (Q63): sum-check. ONE retry on out-of-band with the
        # error appended to extra_rules. A 2nd out-of-band outline is
        # accepted with a logger.error — the downstream critic
        # (`validate_long_form_envelope`) catches length violations
        # via the per-section + total bands authoritatively, so the
        # outline shape becoming-best-effort here loses nothing the
        # downstream gate doesn't re-detect. Per ADR-023 the gates are
        # repair triggers, not termination — preferring the outline
        # we have over hard-failing the whole render keeps the system
        # forward-progressing.
        if total_words_target > 0 and not _outline_sum_in_band(outline, total_words_target):
            section_sum = _outline_section_sum(outline)
            delta_pct = ((section_sum - total_words_target) / total_words_target) * 100
            if sum_check_used:
                _logger.error(
                    "outline section sum out of ±15%% band on retry too: "
                    "sum=%d target=%d delta=%+.1f%% — accepting outline "
                    "and relying on the downstream length validator.",
                    section_sum, total_words_target, delta_pct,
                )
                return outline
            sum_check_used = True
            sum_error_block = (
                f"\n\nIMPORTANT — previous outline had section sums totaling "
                f"{section_sum} words against the user-requested total of "
                f"{total_words_target} words ({delta_pct:+.1f}% off). "
                f"Reallocate target_words across sections so the sum is "
                f"within ±15% (range: "
                f"{int(total_words_target * (1 - _OUTLINE_SUM_TOLERANCE_FRAC))}-"
                f"{int(total_words_target * (1 + _OUTLINE_SUM_TOLERANCE_FRAC))} words). "
                f"Keep the same section_count_target; redistribute weight."
            )
            extra_rules = (extra_rules or "") + sum_error_block
            _logger.warning(
                "outline sum-check OUT OF BAND (sum=%d target=%d delta=%+.1f%%); "
                "retrying with sum error in prompt",
                section_sum, total_words_target, delta_pct,
            )
            continue

        return outline

    # All attempts exhausted — synthesize a minimal outline.
    _logger.error(
        "outline LLM call failed after %d attempts (last_err=%s); "
        "falling back to synthesized outline",
        _OUTLINE_MAX_RETRIES + 1, last_err,
    )
    return _synthesize_outline(
        raw_story=raw_story,
        section_count_target=section_count_target_for_synth,
        section_words_target=section_words_target_for_synth,
    )


def _synthesize_outline(
    *, raw_story: dict, section_count_target: int,
    section_words_target: int,
) -> dict:
    """Build a minimal but contract-valid outline from raw_story alone.

    Used as the absolute fallback when the outline LLM call fails on
    every retry. The result is structurally valid — N sections each
    pointing at the source body as their brief — so section-body
    calls can still run and produce real narration. The body LLM
    fills in the actual content per section.

    This guarantees the wizard never sees a hard rewrite failure due
    to outline-stage problems alone, regardless of input combination.
    """
    title = (raw_story.get("title") or "Untitled").strip() or "Untitled"
    body = (raw_story.get("body") or "").strip()
    # Slice the body into N section briefs so each section call has
    # a slightly different anchor. If body is empty, every section's
    # brief is the title — the LLM will still produce narration but
    # under-delivery is expected (and now soft, not fatal).
    brief_chunks: list[str] = []
    if body:
        chunk_len = max(120, len(body) // max(1, section_count_target))
        for i in range(section_count_target):
            start = i * chunk_len
            end = min(len(body), start + chunk_len + 60)  # small overlap
            chunk = body[start:end].strip()
            brief_chunks.append(chunk or title)
    else:
        brief_chunks = [title] * section_count_target

    sections = [
        {
            "id": f"sec-{i}",
            "title": f"Part {i + 1}",
            "brief": brief_chunks[i],
            "target_words": section_words_target,
            "visual_brief": None,
            # Synthesized fallback — generic positive spec. The body
            # LLM still gets a non-empty quality_goal so the prompt
            # template formats correctly.
            "quality_goal": "deliver a substantive, source-anchored beat",
        }
        for i in range(section_count_target)
    ]
    return {
        "hook": title,
        "thesis": title,
        "sections": sections,
        "panel_briefs": [],  # downstream will populate from sections
        "sources": [raw_story.get("url", "")] if raw_story.get("url") else [],
        "title_options": [title],
    }


def _normalize_outline(
    outline: dict, *, section_count_target: int,
    section_words_target: int, raw_story: dict,
    title_options_count: int,
) -> dict:
    """Defensive normalization of an LLM-returned outline.

    Handles every observed misbehavior pattern:
    * Section count explosion — outline returns 30 sections for a
      15-min ask. Truncate to ``section_count_target * 2`` so cost
      stays bounded.
    * Per-section ``target_words`` ridiculous — clamp to
      ``[section_words_target * 0.5, section_words_target * 2]`` so
      the section-body min/max calculation stays sane.
    * Missing hook / thesis — synthesize from raw_story.title.
    * Missing title_options — fall back to raw_story.title.
    * Sections with blank/missing id or title — drop them (the
      aggregator already does this but we surface a warning here).

    Returns a new dict; doesn't mutate input.
    """
    out = dict(outline) if isinstance(outline, dict) else {}
    title = (raw_story.get("title") or "Untitled").strip() or "Untitled"

    # ----- sections normalization -----
    raw_sections = out.get("sections") or []
    if not isinstance(raw_sections, list):
        raw_sections = []
    section_count_cap = max(section_count_target * 2, section_count_target + 4)
    word_floor_per_section = max(50, int(section_words_target * 0.5))
    word_ceil_per_section = max(120, int(section_words_target * 2.0))

    normalized_sections: list[dict] = []
    for i, s in enumerate(raw_sections):
        if not isinstance(s, dict):
            continue
        sid = (s.get("id") or f"sec-{i}").strip() or f"sec-{i}"
        stitle = (s.get("title") or f"Part {i + 1}").strip() or f"Part {i + 1}"
        # target_words: clamp to a reasonable range so a misbehaving
        # outline doesn't force section-body min_words ≫ model's
        # reliable output range (the 2026-05-20 failure mode).
        try:
            tw = int(s.get("target_words") or section_words_target)
        except (TypeError, ValueError):
            tw = section_words_target
        if tw < word_floor_per_section:
            tw = word_floor_per_section
        elif tw > word_ceil_per_section:
            tw = word_ceil_per_section
        # quality_goal — outline-authored per Q61/P3.5. If the LLM
        # didn't emit one (older deployments, partial JSON repair),
        # fall back to a generic positive spec so the body-prompt
        # template still formats correctly. Logged at info so
        # dashboards can flag stale outlines that rely on the fallback.
        quality_goal = (s.get("quality_goal") or "").strip()
        if not quality_goal:
            quality_goal = "deliver a substantive, source-anchored beat"
            _logger.info(
                "_normalize_outline: section %s missing quality_goal; "
                "applying generic positive-spec fallback",
                sid,
            )
        normalized_sections.append({
            **s,
            "id": sid,
            "title": stitle,
            "target_words": tw,
            "quality_goal": quality_goal,
        })

    if len(normalized_sections) > section_count_cap:
        _logger.warning(
            "_normalize_outline: outline returned %d sections; "
            "truncating to cap=%d",
            len(normalized_sections), section_count_cap,
        )
        normalized_sections = normalized_sections[:section_count_cap]

    if not normalized_sections:
        # Outline returned a sections key but every entry was unusable.
        # Synthesize a minimal one rather than passing an empty list to
        # the fan-out (which would produce zero LLM calls).
        _logger.warning(
            "_normalize_outline: 0 usable sections after normalization; "
            "synthesizing %d-section fallback from raw_story",
            section_count_target,
        )
        synth = _synthesize_outline(
            raw_story=raw_story,
            section_count_target=section_count_target,
            section_words_target=section_words_target,
        )
        normalized_sections = synth["sections"]

    out["sections"] = normalized_sections

    # ----- top-level field defaults -----
    if not (out.get("hook") or "").strip():
        out["hook"] = title
    if not (out.get("thesis") or "").strip():
        out["thesis"] = title
    title_options = out.get("title_options") or []
    title_options = [
        str(t).strip() for t in title_options
        if isinstance(t, str) and t.strip()
    ]
    if not title_options:
        title_options = [title]
    out["title_options"] = title_options[:max(1, title_options_count)]
    if not isinstance(out.get("panel_briefs"), list):
        out["panel_briefs"] = []
    if not isinstance(out.get("sources"), list):
        out["sources"] = []
    return out


# ---------------------------------------------------------------------------
# Phase 2: section-body call (one per section, parallel)
# ---------------------------------------------------------------------------


class SectionTooShortError(Exception):
    """Raised by ``_call_section_body_llm`` when the LLM returns a
    narration with fewer words than the section's hard minimum.

    Carries word-count metadata so the retry layer can embed a concrete
    ``"your previous attempt was N words, write at least M"`` line in
    the retry prompt — which is the literal fix the aggregate
    ``section_degradation_hard`` validator message requests.
    """

    def __init__(
        self,
        message: str,
        *,
        section_id: str,
        word_count: int,
        min_words: int,
        target_words: int,
        narration: str,
    ) -> None:
        super().__init__(message)
        self.section_id = section_id
        self.word_count = word_count
        self.min_words = min_words
        self.target_words = target_words
        self.narration = narration


def _count_words(text: str | None) -> int:
    if not text:
        return 0
    return len(re.findall(r"\S+", text))


def _emphasis_block_for_retry(
    *, prev_word_count: int, min_words: int, target_words: int,
    prev_short_draft: str = "",
) -> str:
    """Build the retry-only emphasis block injected into the section-body
    prompt when the previous attempt under-delivered.

    P3.6 (Q62) — iterative-extend: when ``prev_short_draft`` is supplied
    the block includes the failed draft verbatim with an explicit
    instruction to EXTEND it (add detail), not REWRITE from scratch.
    That preserves the LLM's prior context investment instead of
    discarding it. Returns an empty string on the first attempt (no
    retry context yet).
    """
    if prev_word_count <= 0:
        return ""
    block = (
        "\n  ⚠ RETRY NOTICE — your previous attempt for this section was "
        f"{prev_word_count} words. The minimum is {min_words}; the target "
        f"is {target_words}. You MUST write more substantive narration "
        "this time — add concrete source-anchored detail (specific names, "
        "dates, places, sensory description, cause-and-effect). Do not "
        "restate what you already wrote; expand it."
    )
    draft = (prev_short_draft or "").strip()
    if draft:
        # Iterative-extend payload (P3.6) — feed the failed draft back
        # in so the LLM extends rather than re-rolling from scratch.
        block += (
            "\n  ⚠ ITERATIVE-EXTEND — previous draft below; EXTEND it by "
            f"adding source detail until the body reaches {target_words} "
            "words (±10%). Keep the existing sentences verbatim where they "
            "work; weave new sentences in between them. Do not rewrite. Do "
            "not paraphrase. Extend.\n  PREVIOUS DRAFT:\n  ---\n  "
            + draft.replace("\n", "\n  ")
            + "\n  ---"
        )
    block += "\n"
    return block


def _call_section_body_llm(
    *, channel_context: str, topic: str, thesis: str,
    outline: dict, notes: str, section: dict,
    section_words_target: int, section_words_floor: int,
    extra_context: str = "",
    prev_short_word_count: int = 0,
    prev_short_draft: str = "",
) -> dict:
    """Run ONE section-body LLM call. Returns the raw section-body dict
    with ``{narration, sentences}``.

    Designed to be called inside ThreadPoolExecutor.submit(...) so all
    sections generate in parallel.

    Raises:
        SectionTooShortError: when the LLM returns narration shorter
            than the section's hard minimum. The retry layer catches
            this specifically and re-calls with ``prev_short_word_count``
            set so the prompt is emphatic about the gap.
    """
    section_id = section.get("id", "?")
    section_title = section.get("title", "")
    section_brief = section.get("brief", "")
    visual_brief = section.get("visual_brief") or "(no specific visual brief)"
    # Outline-authored positive specification for this section. Empty
    # string is a benign fallback (renders as "(no specific quality
    # goal)" so the prompt template still formats).
    quality_goal = (section.get("quality_goal") or
                    "deliver a substantive, source-anchored beat").strip()
    target_words = int(section.get("target_words") or section_words_target)
    # P3.2 (Q59): per-section ±10% of section target. Matches the
    # post-rewrite gate so the LLM is asked for the band the validator
    # checks — no asymmetry between "what we instruct" and "what we
    # accept". Floor at 40 / 80 so very short sections (intro/outro
    # under tight target_words) don't collapse the band to zero.
    from pipeline.critic_long_form import SECTION_TOLERANCE_FRAC  # noqa: PLC0415
    min_words = max(40, int(target_words * (1.0 - SECTION_TOLERANCE_FRAC)))
    max_words = max(80, int(target_words * (1.0 + SECTION_TOLERANCE_FRAC)))

    outline_summary = _outline_summary_for_section_call(outline, section_id)

    emphasis_block = _emphasis_block_for_retry(
        prev_word_count=prev_short_word_count,
        min_words=min_words,
        target_words=target_words,
        prev_short_draft=prev_short_draft,
    )

    prompt = _SECTION_BODY_PROMPT_TEMPLATE.format(
        channel_context=channel_context + extra_context,
        topic=topic,
        thesis=thesis,
        outline_summary=outline_summary,
        notes=notes,
        section_id=section_id,
        section_title=section_title,
        visual_brief=visual_brief,
        quality_goal=quality_goal,
        target_words=target_words,
        min_words=min_words,
        max_words=max_words,
        section_brief=section_brief,
        emphasis_block=emphasis_block,
    )

    raw = _llm.call_claude_cli(
        prompt,
        output_json=True,
        json_schema=_SECTION_BODY_SCHEMA,
        model=_llm.model_for("rewrite_long_form"),
        stage="rewrite_long_form_section",
    )
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"_call_section_body_llm: LLM returned {type(raw).__name__}, "
            f"expected dict (output_json=True)"
        )
    narration = raw.get("narration") or ""
    if not narration:
        raise RuntimeError(
            f"_call_section_body_llm: section {section_id!r} returned empty narration"
        )
    # P3.1 / Q60: actual word count is the source of truth. Emitted
    # count is anchoring telemetry only — log the delta when present
    # so dashboards can spot persistent miscounting (a real-world
    # signal that the LLM is hallucinating its self-count, distinct
    # from the length issue itself).
    word_count = _count_words(narration)
    emitted = raw.get("word_count")
    if isinstance(emitted, int) and emitted != word_count:
        _logger.info(
            "section %s emitted word_count=%d but actual=%d (delta=%+d) — "
            "validator uses actual (P3.1 anchoring telemetry)",
            section_id, emitted, word_count, emitted - word_count,
        )
    if word_count < min_words:
        # Gate removed per user direction: under-min sections are now
        # accepted as-is. Log only; do not raise. The retry layer's
        # SectionTooShortError handler is correspondingly inert.
        _logger.warning(
            "section %s narration is %d words; below minimum %d "
            "(target %d) — gate disabled, accepting",
            section_id, word_count, min_words, target_words,
        )
    return raw


# Parallel section-body fan-out parameters.
# ThreadPoolExecutor — Azure OpenAI rate-limits at the deployment
# level, so 5 in-flight calls is conservative against the per-minute
# token budget. STORM uses 10; we keep tighter to leave headroom for
# any other concurrent stages.
_SECTION_BODY_MAX_WORKERS = 5
# Per-section retry budget on body call failures. Bodies can fail for
# a variety of reasons (transient 5xx, content filter on a sensitive
# beat, schema validation glitch, under-delivered word count). Two
# retries covers both a transient AND a follow-up short-delivery
# without compounding cost — each retry is ~700 tokens.
_SECTION_BODY_MAX_RETRIES = 2


def _generate_all_section_bodies(
    *, channel_context: str, topic: str, thesis: str,
    outline: dict, notes: str,
    section_words_target: int, section_words_floor: int,
    extra_context: str = "",
) -> list[dict]:
    """Fan out one body call per section, ThreadPoolExecutor parallel.

    Returns sections with a ``_body`` field added (the raw section-body
    dict from the LLM). Order is preserved from the outline.

    On a per-section failure, retries once. If the retry also fails,
    the section gets ``_body = None`` and the aggregator can decide
    (today: fall back to the section's brief as narration so the
    render at least produces something for the slot — the validator
    will surface the under-delivery soft warning).
    """
    sections = list(outline.get("sections") or [])
    if not sections:
        return []

    def _attempt(section: dict) -> tuple[dict, dict | None, str | None]:
        last_err: str | None = None
        # Carries forward across attempts so a retry knows the prior
        # under-delivered word count AND the previous draft text
        # (P3.6 iterative-extend — feed the failed draft back so the
        # LLM extends rather than re-rolls from scratch).
        prev_short_word_count = 0
        prev_short_draft = ""
        # Best body so far — if every attempt under-delivers we keep
        # the longest one rather than falling back to the directorial
        # brief (which is usually ~30-60 words and guaranteed to fail
        # the aggregate section_degradation_hard validator).
        best_short_body: dict | None = None
        best_short_count = 0
        for attempt in range(1 + _SECTION_BODY_MAX_RETRIES):
            try:
                body = _call_section_body_llm(
                    channel_context=channel_context, topic=topic,
                    thesis=thesis, outline=outline, notes=notes,
                    section=section,
                    section_words_target=section_words_target,
                    section_words_floor=section_words_floor,
                    extra_context=extra_context,
                    prev_short_word_count=prev_short_word_count,
                    prev_short_draft=prev_short_draft,
                )
                return section, body, None
            except SectionTooShortError as exc:
                last_err = f"SectionTooShortError: {exc}"
                prev_short_word_count = exc.word_count
                prev_short_draft = exc.narration
                if exc.word_count > best_short_count:
                    best_short_body = {
                        "narration": exc.narration,
                        # Best-effort sentence split for caption alignment;
                        # the renderer re-splits anyway.
                        "sentences": [
                            s.strip() for s in re.split(r"(?<=[.!?])\s+", exc.narration)
                            if s.strip()
                        ] or [exc.narration],
                    }
                    best_short_count = exc.word_count
                _logger.warning(
                    "section-body LLM call under-delivered for %s "
                    "(attempt %d/%d): %d words < min %d",
                    section.get("id"), attempt + 1,
                    _SECTION_BODY_MAX_RETRIES + 1, exc.word_count, exc.min_words,
                )
                continue
            except _llm.ContentFilterError as exc:
                # Azure content filter suppressed the response —
                # retrying with the same prompt won't help. Skip
                # remaining retries and fall back to brief immediately.
                last_err = f"ContentFilterError: {exc}"
                _logger.warning(
                    "section-body LLM call hit content filter for %s "
                    "(no retry; falling back to brief): %s",
                    section.get("id"), str(exc)[:200],
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                _logger.warning(
                    "section-body LLM call failed for %s (attempt %d/%d): %s",
                    section.get("id"), attempt + 1,
                    _SECTION_BODY_MAX_RETRIES + 1, last_err,
                )
        # All attempts exhausted. Prefer a short body we did get over
        # falling back to the directorial brief — a partially-short
        # section is still drift-resistant narration; the brief is not.
        if best_short_body is not None:
            return section, best_short_body, last_err
        return section, None, last_err

    # Submit all in parallel; collect by index so order is preserved.
    results: list[tuple[dict, dict | None, str | None]] = [None] * len(sections)  # type: ignore
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_SECTION_BODY_MAX_WORKERS, len(sections))
    ) as ex:
        future_to_idx = {
            ex.submit(_attempt, s): i for i, s in enumerate(sections)
        }
        for fut in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[fut]
            results[idx] = fut.result()

    # Stitch _body onto each section dict (preserve original order).
    annotated: list[dict] = []
    failures: list[str] = []
    for section, body, err in results:
        out = dict(section)
        if body is None:
            out["_body"] = None
            out["_body_error"] = err
            failures.append(f"{section.get('id')}: {err}")
        else:
            out["_body"] = body
        annotated.append(out)
    if failures:
        _logger.warning(
            "rewrite_long_form: %d/%d section bodies failed; "
            "falling back to section brief as narration for those slots: %s",
            len(failures), len(sections), "; ".join(failures),
        )
    return annotated


# ---------------------------------------------------------------------------
# Auto-repair pass — re-call short section bodies with strong emphasis
# ---------------------------------------------------------------------------


def _repair_short_section_bodies(
    *, sections_with_bodies: list[dict],
    channel_context: str, topic: str, thesis: str,
    outline: dict, notes: str,
    section_words_target: int, section_words_floor: int,
    extra_context: str = "",
) -> list[dict]:
    """Re-call the body LLM for any section whose current narration is
    below its hard minimum, with an explicit ``prev_short_word_count``
    so the prompt's retry-emphasis block fires.

    This is the auto-repair pass invoked by ``rewrite_long_form`` when
    the aggregate validator surfaces a length-related hard violation.
    It targets only the offending sections (not all of them) to stay
    within budget — repairing 1-2 short sections is much cheaper than
    re-running the whole rewrite.

    Sections without a body (e.g. content-filter casualties that fell
    back to brief) are also targeted — they're effectively
    under-delivered too.

    Order is preserved. Sections whose repair call ALSO under-delivers
    keep their best-of result (per the inner ``_attempt`` fallback).
    """
    from pipeline.critic_long_form import SECTION_TOLERANCE_FRAC  # noqa: PLC0415
    needs_repair: list[tuple[int, dict]] = []
    for idx, s in enumerate(sections_with_bodies):
        body = s.get("_body") or {}
        narration = body.get("narration") if isinstance(body, dict) else ""
        target_words = int(s.get("target_words") or section_words_target)
        min_words = max(40, int(target_words * (1.0 - SECTION_TOLERANCE_FRAC)))
        max_words = max(80, int(target_words * (1.0 + SECTION_TOLERANCE_FRAC)))
        actual = _count_words(narration)
        # P3.2: repair when section is outside the symmetric band, not
        # only when under the floor — over-long sections need trimming
        # too (currently surfaced via length_over_delivered_hard).
        if actual < min_words or actual > max_words:
            needs_repair.append((idx, s))

    if not needs_repair:
        return sections_with_bodies

    _logger.info(
        "_repair_short_section_bodies: re-calling %d/%d section bodies "
        "with explicit per-section minimum emphasis",
        len(needs_repair), len(sections_with_bodies),
    )

    def _repair_one(section: dict) -> tuple[dict | None, str | None]:
        target_words = int(section.get("target_words") or section_words_target)
        min_words = max(40, int(target_words * (1.0 - SECTION_TOLERANCE_FRAC)))
        # Seed the prompt with the current short word count so the
        # emphasis block in the section-body prompt fires loudly.
        prev_body = section.get("_body") or {}
        prev_narration = prev_body.get("narration") if isinstance(prev_body, dict) else ""
        prev_count = _count_words(prev_narration)
        if prev_count <= 0:
            # No previous body — seed with min_words - 1 so the retry
            # emphasis still fires (LLM sees there's a floor to clear).
            prev_count = max(1, min_words - 1)

        last_err: str | None = None
        best_body = prev_body if isinstance(prev_body, dict) and prev_narration else None
        best_count = prev_count if prev_narration else 0
        prev_draft_text = prev_narration if prev_narration else ""
        # Two repair attempts — each pass shows the LLM the gap explicitly
        # AND feeds back the previous draft for iterative-extend (P3.6).
        for attempt in range(2):
            try:
                body = _call_section_body_llm(
                    channel_context=channel_context, topic=topic,
                    thesis=thesis, outline=outline, notes=notes,
                    section=section,
                    section_words_target=section_words_target,
                    section_words_floor=section_words_floor,
                    extra_context=extra_context,
                    prev_short_word_count=prev_count,
                    prev_short_draft=prev_draft_text,
                )
                return body, None
            except SectionTooShortError as exc:
                last_err = f"SectionTooShortError: {exc}"
                prev_count = exc.word_count
                prev_draft_text = exc.narration
                if exc.word_count > best_count:
                    best_body = {
                        "narration": exc.narration,
                        "sentences": [
                            s.strip() for s in re.split(r"(?<=[.!?])\s+", exc.narration)
                            if s.strip()
                        ] or [exc.narration],
                    }
                    best_count = exc.word_count
                _logger.warning(
                    "_repair_short_section_bodies: section %s still short on "
                    "repair attempt %d/2: %d words < min %d",
                    section.get("id"), attempt + 1, exc.word_count, exc.min_words,
                )
                continue
            except _llm.ContentFilterError as exc:
                last_err = f"ContentFilterError: {exc}"
                _logger.warning(
                    "_repair_short_section_bodies: section %s hit content "
                    "filter on repair (no further retry): %s",
                    section.get("id"), str(exc)[:200],
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                _logger.warning(
                    "_repair_short_section_bodies: section %s repair failed "
                    "(attempt %d/2): %s",
                    section.get("id"), attempt + 1, last_err,
                )
        return best_body, last_err

    # Run the repair calls in parallel — same budget guardrails as the
    # initial fan-out.
    repaired_results: dict[int, tuple[dict | None, str | None]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_SECTION_BODY_MAX_WORKERS, len(needs_repair))
    ) as ex:
        future_to_idx = {
            ex.submit(_repair_one, s): idx for idx, s in needs_repair
        }
        for fut in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[fut]
            repaired_results[idx] = fut.result()

    # Stitch repaired bodies back into the original section list.
    updated: list[dict] = []
    for idx, s in enumerate(sections_with_bodies):
        if idx in repaired_results:
            new_body, err = repaired_results[idx]
            out = dict(s)
            if new_body is not None:
                out["_body"] = new_body
                # Clear any prior error since we now have content.
                out.pop("_body_error", None)
            elif err is not None:
                out["_body_error"] = err
            updated.append(out)
        else:
            updated.append(s)
    return updated


# ---------------------------------------------------------------------------
# Phase 3: aggregator
# ---------------------------------------------------------------------------


def _aggregate(
    *, raw_story: dict, outline: dict, sections_with_bodies: list[dict],
    title_options_count: int,
    channel_cfg: dict[str, Any] | None = None,
) -> ScriptEnvelope:
    """Stitch outline + section bodies into a ScriptEnvelope.

    ``channel_cfg`` is the merged channel YAML (base + variant overlay).
    Currently used for ``long_form.panel_max_count`` to cap the panel
    list at the channel's configured ceiling rather than the legacy
    hardcoded 60. Pass ``None`` in test/legacy callsites — defaults to
    120 panels (the post-2026-05 ceiling for animated-stills long-form;
    see docs/panel_pacing_research_2026-05.md).
    """
    # Defensive parse — sections that came back without a body fall
    # back to brief-as-narration so the renderer doesn't crash on an
    # empty narration field. The validator surfaces this as
    # under-delivery and the soft-warning path keeps the render going.
    sections: list[LongFormSection] = []
    for s in sections_with_bodies:
        sid = (s.get("id") or "").strip()
        stitle = (s.get("title") or "").strip()
        if not sid or not stitle:
            continue
        body = s.get("_body") or {}
        narration = (body.get("narration") or s.get("brief") or "").strip()
        if not narration:
            continue
        # P3.2: thread per-section target_words from the outline so the
        # validator can apply its ±10% per-section gate against the
        # section's OWN target (not the symmetric-around-mean rule).
        try:
            target_words = int(s.get("target_words")) if s.get("target_words") is not None else None
        except (TypeError, ValueError):
            target_words = None
        sections.append(LongFormSection(
            id=sid,
            title=stitle,
            narration=narration,
            target_s=None,  # audio length wins; the rewriter no longer hints target_s
            visual_brief=(s.get("visual_brief") or None),
            target_words=target_words,
        ))

    # Panels — from the outline's panel_briefs[]. Defensive about
    # hold_s + scene; drop any entry without a non-empty scene.
    panels: list[LongFormPanel] = []
    from pipeline import critic_long_form as _critic  # noqa: PLC0415
    for p in outline.get("panel_briefs") or []:
        if not isinstance(p, dict):
            continue
        scene = (p.get("scene") or "").strip()
        if not scene:
            continue
        try:
            hold_s = float(p.get("hold_s") or 6.0)
        except (TypeError, ValueError):
            hold_s = 6.0
        # Cap at the soft max for any panel that exceeds the hard max.
        # Defense-in-depth — keeps a misbehaving outline call from
        # emitting a dead-frame video.
        if hold_s > _critic.PANEL_HOLD_HARD_MAX_S:
            hold_s = _critic.PANEL_HOLD_SOFT_MAX_S
        after_id = p.get("after_section_id")
        after_id_str = str(after_id).strip() if after_id else None
        if after_id_str == "":
            after_id_str = None
        panels.append(LongFormPanel(
            scene=scene,
            hold_s=hold_s,
            after_section_id=after_id_str,
        ))
    # Truncate at the channel's configured cap (default 120 — was 60).
    # Read from channel_cfg.long_form.panel_max_count; the cloud renderer
    # used to hard-cap 60 here, which silently throttled any future
    # cadence bump from the LLM prompt. See docs/panel_pacing_research_2026-05.md.
    lf_cfg = (channel_cfg or {}).get("long_form") or {}
    try:
        panel_max_count = int(lf_cfg.get("panel_max_count") or 120)
    except (TypeError, ValueError):
        panel_max_count = 120
    if len(panels) > panel_max_count:
        _logger.warning(
            "rewrite_long_form: outline emitted %d panels; truncating to %d "
            "(channel.long_form.panel_max_count)",
            len(panels), panel_max_count,
        )
        panels = panels[:panel_max_count]

    long_form = LongFormScript(
        hook=(outline.get("hook") or "").strip(),
        thesis=(outline.get("thesis") or "").strip(),
        sections=sections,
        panels=panels,
        sources=[
            str(s).strip() for s in (outline.get("sources") or [])
            if s and isinstance(s, str)
        ],
    )

    title_options = [
        str(t).strip() for t in (outline.get("title_options") or [])
        if isinstance(t, str) and t.strip()
    ][:title_options_count or 4]

    return ScriptEnvelope(
        slug=raw_story.get("slug") or "untitled",
        kind="long_form",
        title_options=title_options,
        source_url=raw_story.get("url", ""),
        source=raw_story.get("source", ""),
        long_form=long_form,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def rewrite_long_form(
    raw_story: dict[str, Any],
    *,
    channel_cfg: dict[str, Any] | None = None,
    target_duration_s: int = 1800,
    title_options_count: int = 4,
    spec: Any = None,
) -> ScriptEnvelope:
    """STORM-pattern two-phase long-form rewriter.

    Args:
        raw_story: ``{slug, title, body, source, url}`` — same shape the
            Shorts rewriter accepts.
        channel_cfg: Merged channel YAML cfg (base + variant overlay).
            Provides aesthetic + render_mode context.
        target_duration_s: Target final video duration in seconds.
        title_options_count: Hint for the prompt (LLM may return more
            or fewer; we slice to this max).
        spec: Optional :class:`pipeline.render.spec.RenderSpec` — when
            provided, descriptor-registered prompt patches are
            collected and injected into BOTH the outline and section-
            body prompts.

    Returns:
        Fully-populated :class:`ScriptEnvelope` with ``kind="long_form"``.

    Raises:
        ValueError: if raw_story has no title or body.
        ClaudeCLIError: on backend failure (bubbled).
        LongFormContractError: if the rewrite fails the post-aggregation
            contract validator AND a single retry of the failed section
            bodies also fails.
    """
    title = (raw_story.get("title") or "").strip()
    body = (raw_story.get("body") or "").strip()
    if not title and not body:
        raise ValueError("raw_story has no title or body")

    cfg = channel_cfg or {}
    duration_s = max(120, int(target_duration_s))
    duration_min = max(2, math.ceil(duration_s / 60))

    (
        words_target,
        words_floor,
        words_ceiling,
        section_count_target,
        section_words_target,
        section_words_floor,
        panel_count_target,
        panel_min_per_section,
    ) = _planned_sections_and_panels(duration_s, channel_cfg=cfg)

    # Cap source body before injection — a 200k-char scraped article or
    # full subreddit JSON dump would blow past prompt-context budget on
    # the outline call (the prompt template embeds `notes` verbatim).
    # 12000 chars ≈ 3000 tokens — plenty of context for outline planning
    # without saturating the input budget. Section-body calls re-inject
    # the same notes blob, so this cap also keeps THEIR prompts safe.
    notes = _cap_notes(body) if body else (
        "(no extra notes provided — author from the topic alone)"
    )
    niche = _niche_from_spec_or_cfg(spec, cfg)
    niche_title_rules = _niche_title_rules_block(niche)
    channel_context = _channel_context(cfg)

    # Descriptor-driven prompt patches (audio_mode=song, narrator_visual_mode,
    # visual_source_branch, …) — collected from the spec and injected into
    # BOTH the outline and section-body prompts so per-render form picks
    # propagate consistently.
    extra_context = ""
    extra_rules = ""
    if spec is not None:
        try:
            from pipeline.render.input_registry import prompt_patches_for  # noqa: PLC0415
            prompt_patch = prompt_patches_for(spec)
            if prompt_patch.context_lines:
                extra_context = "\n\nADDITIONAL CONTEXT (from form picks):\n- " + \
                    "\n- ".join(prompt_patch.context_lines)
            if prompt_patch.craft_rules:
                extra_rules = "\n\nADDITIONAL CRAFT RULES (from form picks):\n- " + \
                    "\n- ".join(prompt_patch.craft_rules)
            if prompt_patch.context_lines or prompt_patch.craft_rules:
                _logger.info(
                    "rewrite_long_form: descriptor patches — %d context, %d rules",
                    len(prompt_patch.context_lines), len(prompt_patch.craft_rules),
                )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("prompt_patches_for failed: %s — proceeding bare", exc)

    from pipeline import critic_long_form as _critic  # noqa: PLC0415

    _logger.info(
        "rewrite_long_form: STORM-pattern start — topic=%r duration=%ds "
        "target=%dw (floor=%dw, ceiling=%dw) sections~%d panels~%d niche=%r",
        title[:60], duration_s, words_target, words_floor, words_ceiling,
        section_count_target, panel_count_target, niche,
    )

    # ----- Phase 1: outline ---------------------------------------------
    # The outline is the single point of failure for everything
    # downstream — section bodies, panels, title options all derive
    # from it. A transient Azure 5xx / content-filter / empty-sections
    # result here is catastrophic for the render. Wrap it in retry +
    # synthesized-fallback so the render proceeds with SOMETHING even
    # in the worst case (the downstream length validator will surface
    # the gap as a soft warning).
    # Resolve panel_seconds_target from channel cfg so the outline
    # prompt receives the same cadence value _planned_sections_and_panels
    # used to compute panel_count_target. Single source of truth =
    # channel YAML's long_form.panel_seconds_target.
    _lf_cfg = (cfg or {}).get("long_form") or {}
    try:
        panel_seconds_target = max(8, int(_lf_cfg.get("panel_seconds_target") or 25))
    except (TypeError, ValueError):
        panel_seconds_target = 25
    outline = _call_outline_with_retry(
        channel_context=channel_context,
        topic=title,
        notes=notes,
        duration_min=duration_min,
        duration_s=duration_s,
        section_count_target=section_count_target,
        section_words_target=section_words_target,
        panel_count_target=panel_count_target,
        panel_min_per_section=panel_min_per_section,
        panel_hold_max_s=int(_critic.PANEL_HOLD_HARD_MAX_S),
        panel_seconds_target=panel_seconds_target,
        niche_title_rules=niche_title_rules,
        extra_context=extra_context,
        extra_rules=extra_rules,
        raw_story=raw_story,
        section_words_target_for_synth=section_words_target,
        section_count_target_for_synth=section_count_target,
        total_words_target=words_target,  # P3.3 — sum-check tolerance
    )
    # Defensive normalization — even after retry the LLM can return a
    # bloated section list, missing hook/thesis, or per-section
    # target_words wildly off from the global plan.
    outline = _normalize_outline(
        outline,
        section_count_target=section_count_target,
        section_words_target=section_words_target,
        raw_story=raw_story,
        title_options_count=title_options_count,
    )
    n_outline_sections = len(outline.get("sections") or [])
    n_outline_panels = len(outline.get("panel_briefs") or [])
    _logger.info(
        "rewrite_long_form: outline produced — sections=%d panels=%d title_options=%d",
        n_outline_sections, n_outline_panels,
        len(outline.get("title_options") or []),
    )

    # ----- Phase 2: parallel section bodies -----------------------------
    sections_with_bodies = _generate_all_section_bodies(
        channel_context=channel_context,
        topic=title,
        thesis=outline.get("thesis", ""),
        outline=outline,
        notes=notes,
        section_words_target=section_words_target,
        section_words_floor=section_words_floor,
        extra_context=extra_context,
    )
    n_filled = sum(1 for s in sections_with_bodies if s.get("_body"))
    _logger.info(
        "rewrite_long_form: section bodies — %d/%d filled",
        n_filled, len(sections_with_bodies),
    )

    # ----- Phase 3: aggregate -------------------------------------------
    env = _aggregate(
        raw_story=raw_story,
        outline=outline,
        sections_with_bodies=sections_with_bodies,
        title_options_count=title_options_count,
        channel_cfg=channel_cfg,
    )

    # Validator (post-aggregation, full envelope).
    violations = _critic.validate_long_form_envelope(
        env, target_duration_s=duration_s, niche=niche,
        raw_body=body,
    )

    # ----- Auto-repair pass for length-related hard violations ----------
    # `section_degradation_hard` and `length_under_delivered_hard` are
    # both repairable by re-calling the under-delivered section bodies
    # with explicit per-section emphasis — the literal fix the
    # violation messages request. We attempt ONE repair pass before
    # giving up. Tonal / source-fidelity / banned-anecdote violations
    # are NOT repairable here (they need different prompts, not longer
    # ones) and continue to hard-fail.
    # P3.2 introduced over/under split for total length violations.
    # All length-class hard codes are repairable via the iterative
    # extend retry (P3.6); both directions of section-target miss
    # are repairable too.
    _LENGTH_REPAIRABLE_CODES = {
        "section_degradation_hard",
        "section_degradation_soft",
        "length_under_delivered_hard",
        "length_under_delivered_soft",
        "length_over_delivered_hard",
        "length_over_delivered_soft",
    }
    hard = [v for v in violations if v.severity == "hard"]
    length_hard = [v for v in hard if v.code in _LENGTH_REPAIRABLE_CODES]
    if length_hard:
        _logger.warning(
            "rewrite_long_form: %d length hard violation(s); "
            "attempting auto-repair (one pass): %s",
            len(length_hard), "; ".join(v.code for v in length_hard),
        )
        sections_with_bodies = _repair_short_section_bodies(
            sections_with_bodies=sections_with_bodies,
            channel_context=channel_context,
            topic=title,
            thesis=outline.get("thesis", ""),
            outline=outline,
            notes=notes,
            section_words_target=section_words_target,
            section_words_floor=section_words_floor,
            extra_context=extra_context,
        )
        env = _aggregate(
            raw_story=raw_story,
            outline=outline,
            sections_with_bodies=sections_with_bodies,
            title_options_count=title_options_count,
            channel_cfg=channel_cfg,
        )
        violations = _critic.validate_long_form_envelope(
            env, target_duration_s=duration_s, niche=niche, raw_body=body,
        )

    # After (optional) repair, downgrade any *remaining* length hard
    # violations to soft. Rationale: the user-facing contract is "the
    # wizard never hard-fails on a length quality issue when any input
    # combination is selected". If we couldn't make the LLM produce a
    # balanced-length narration in two passes (initial + repair), we
    # ship a watchable-but-imbalanced video and surface the gap as a
    # soft warning. Tonal / source-fidelity / banned-anecdote
    # violations remain HARD because shipping those produces a
    # genuinely off-source / off-genre video.
    downgraded: list[Any] = []
    survivors: list[Any] = []
    for v in violations:
        if v.severity == "hard" and v.code in _LENGTH_REPAIRABLE_CODES:
            downgraded.append(_critic.Violation(
                code=v.code,
                severity="soft",
                message=v.message + " (downgraded to soft after auto-repair)",
            ))
        else:
            survivors.append(v)
    violations = survivors + downgraded

    hard = [v for v in violations if v.severity == "hard"]
    soft = [v for v in violations if v.severity == "soft"]
    if soft:
        _logger.warning(
            "rewrite_long_form: %d soft violation(s): %s",
            len(soft), "; ".join(v.code for v in soft),
        )
    if hard:
        _logger.error(
            "rewrite_long_form: %d hard violation(s): %s",
            len(hard), "; ".join(v.code for v in hard),
        )
        raise _critic.LongFormContractError(violations)

    _logger.info(
        "rewrite_long_form: STORM-pattern complete — slug=%s sections=%d panels=%d "
        "narration_words≈%d title_options=%d soft_violations=%d",
        env.slug, len(env.long_form.sections), len(env.long_form.panels),
        sum(len(s.narration.split()) for s in env.long_form.sections),
        len(env.title_options), len(soft),
    )
    return env


__all__ = ["rewrite_long_form"]
