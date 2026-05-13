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
                    {{id, title, brief, target_words, visual_brief}}:
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

  panel_briefs    — array of {{scene, hold_s, after_section_id}}
                    panel cues. AT LEAST {panel_min_per_section}
                    panels per section (target {panel_count_target}
                    total panels — HARD MAX 24 by default; the
                    renderer's mode-aware cap may allow more on
                    cloud-rendered channels).

                    scene is a detailed image-gen prompt (the
                    channel's image_style_prefix is prepended at
                    render time — DON'T repeat aesthetic
                    instructions like "flat 2D crayon". Just describe
                    WHAT is in the frame: subject, action, props,
                    mood, composition).

                    hold_s ≤ {panel_hold_max_s} (panels held longer
                    read as DEAD on screen).

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
                "required": ["id", "title", "brief"],
                "properties": {
                    "id":           {"type": "string", "minLength": 1, "maxLength": 80},
                    "title":        {"type": "string", "minLength": 1, "maxLength": 120},
                    "brief":        {"type": "string", "minLength": 30, "maxLength": 2000},
                    "target_words": {"type": ["integer", "null"]},
                    "visual_brief": {"type": ["string", "null"]},
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
  Target words: {target_words}  (soft target — write natural prose
                                 between {min_words} and {max_words}
                                 words; the validator hard-fails
                                 outside this range)

  Directorial brief from the outline:
  {section_brief}

OUTPUT — return ONE JSON object with EXACTLY these keys:

  narration — string. The actual prose the narrator reads. NO
              chapter prefixes ("Section 3:" / "Chapter:"). Plain
              prose. Vary sentence length naturally. End on a small
              open loop pulling viewers into the next section
              (unless this is the final section — then close with
              the arc payoff plus a binary-opinion CTA the closer
              can attach to).

  sentences — array of strings. The same narration, split into
              individual sentences (for caption alignment). Each
              array element should be ONE complete sentence,
              roughly 5-25 words. Keep punctuation; the renderer
              uses these as caption cue boundaries.

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
    "required": ["narration", "sentences"],
    "properties": {
        "narration": {"type": "string", "minLength": 50},
        "sentences": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
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


def _planned_sections_and_panels(duration_s: int) -> tuple[int, int, int, int, int, int, int, int]:
    """Heuristic targets for the LLM prompt. Hint-only — the LLM may
    return any reasonable number that fits the topic.

    Returns ``(words_target, words_floor, words_ceiling,
    section_count_target, section_words_target, section_words_floor,
    panel_count_target, panel_min_per_section)``.
    """
    # Calm TTS narrator @ ~150 wpm.
    words_target = int(duration_s * 150 / 60)
    words_floor = int(words_target * 0.92)
    words_ceiling = int(words_target * 1.10)
    # Roughly one section per ~3 minutes of narration, capped to 6-12.
    section_count_target = max(6, min(12, max(1, duration_s // 180)))
    section_words_target = max(50, words_target // max(1, section_count_target))
    section_words_floor = int(section_words_target * 0.70)
    # No prompt-side ceiling on count: the renderer's PANEL_HARD_CAP is
    # mode-aware (24 on local mflux, 60 on Cloud Run NVIDIA L4 where
    # the Metal command-buffer watchdog doesn't exist).
    panel_seconds = 7
    panel_count_target = max(8, max(1, duration_s // panel_seconds))
    panel_min_per_section = max(4, panel_count_target // max(1, section_count_target))
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
# Phase 1: outline call
# ---------------------------------------------------------------------------


def _call_outline_llm(
    *, channel_context: str, topic: str, notes: str,
    duration_min: int, duration_s: int,
    section_count_target: int, section_words_target: int,
    panel_count_target: int, panel_min_per_section: int,
    panel_hold_max_s: int, niche_title_rules: str,
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


# ---------------------------------------------------------------------------
# Phase 2: section-body call (one per section, parallel)
# ---------------------------------------------------------------------------


def _call_section_body_llm(
    *, channel_context: str, topic: str, thesis: str,
    outline: dict, notes: str, section: dict,
    section_words_target: int, section_words_floor: int,
    extra_context: str = "",
) -> dict:
    """Run ONE section-body LLM call. Returns the raw section-body dict
    with ``{narration, sentences}``.

    Designed to be called inside ThreadPoolExecutor.submit(...) so all
    sections generate in parallel.
    """
    section_id = section.get("id", "?")
    section_title = section.get("title", "")
    section_brief = section.get("brief", "")
    visual_brief = section.get("visual_brief") or "(no specific visual brief)"
    target_words = int(section.get("target_words") or section_words_target)
    # Sectioned word range — narrower than the channel-wide range to
    # discourage one section from sprawling and crowding others.
    min_words = max(40, int(target_words * 0.70))
    max_words = max(80, int(target_words * 1.30))

    outline_summary = _outline_summary_for_section_call(outline, section_id)

    prompt = _SECTION_BODY_PROMPT_TEMPLATE.format(
        channel_context=channel_context + extra_context,
        topic=topic,
        thesis=thesis,
        outline_summary=outline_summary,
        notes=notes,
        section_id=section_id,
        section_title=section_title,
        visual_brief=visual_brief,
        target_words=target_words,
        min_words=min_words,
        max_words=max_words,
        section_brief=section_brief,
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
    if not raw.get("narration"):
        raise RuntimeError(
            f"_call_section_body_llm: section {section_id!r} returned empty narration"
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
# r/nosleep beat, schema validation glitch). One retry covers
# transients without compounding cost.
_SECTION_BODY_MAX_RETRIES = 1


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
        for attempt in range(1 + _SECTION_BODY_MAX_RETRIES):
            try:
                body = _call_section_body_llm(
                    channel_context=channel_context, topic=topic,
                    thesis=thesis, outline=outline, notes=notes,
                    section=section,
                    section_words_target=section_words_target,
                    section_words_floor=section_words_floor,
                    extra_context=extra_context,
                )
                return section, body, None
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                _logger.warning(
                    "section-body LLM call failed for %s (attempt %d/%d): %s",
                    section.get("id"), attempt + 1,
                    _SECTION_BODY_MAX_RETRIES + 1, last_err,
                )
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
# Phase 3: aggregator
# ---------------------------------------------------------------------------


def _aggregate(
    *, raw_story: dict, outline: dict, sections_with_bodies: list[dict],
    title_options_count: int,
) -> ScriptEnvelope:
    """Stitch outline + section bodies into a ScriptEnvelope."""
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
        sections.append(LongFormSection(
            id=sid,
            title=stitle,
            narration=narration,
            target_s=None,  # audio length wins; the rewriter no longer hints target_s
            visual_brief=(s.get("visual_brief") or None),
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
        # Cap at the soft max (8.0) for any panel that exceeds the
        # hard max (12.0). Defense-in-depth — keeps a misbehaving
        # outline call from emitting a dead-frame video.
        if hold_s > _critic.PANEL_HOLD_HARD_MAX_S:
            hold_s = _critic.PANEL_HOLD_SOFT_MAX_S
        panels.append(LongFormPanel(scene=scene, hold_s=hold_s))
    # Truncate at the cloud-renderer's max (60); local renderers
    # re-truncate against their stricter cap downstream.
    if len(panels) > 60:
        _logger.warning(
            "rewrite_long_form: outline emitted %d panels; truncating to 60",
            len(panels),
        )
        panels = panels[:60]

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
    ) = _planned_sections_and_panels(duration_s)

    notes = body or "(no extra notes provided — author from the topic alone)"
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
    outline = _call_outline_llm(
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
        niche_title_rules=niche_title_rules,
        extra_context=extra_context,
        extra_rules=extra_rules,
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
    )

    # Validator (post-aggregation, full envelope). We're soft about the
    # outcome here — if the validator surfaces hard violations we log
    # them but still return the envelope. Phase 0 / Fix D in the
    # analysis doc makes this even softer (worker decides ship vs fail
    # on the violations); for now we behave as before but the salvager
    # surface is gone, so violations are about content quality not
    # parse failures.
    violations = _critic.validate_long_form_envelope(
        env, target_duration_s=duration_s, niche=niche,
    )
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
        # NOTE: Fix D (soft validator) lets the worker decide. For now
        # we still raise to preserve the prior contract but the failure
        # is structurally rare with STORM (no truncation, no salvager).
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
