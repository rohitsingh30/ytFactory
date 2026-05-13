"""Long-form rewriter — produces a sectioned ``LongFormScript`` from a
raw topic + notes input.

This is the long-form sibling of ``pipeline.llm.rewrite.rewrite()``.
It is a SEPARATE contract, not a parameterised version of the Shorts
rewriter — per the rubber-duck critique on Slice 1, the artifact
shapes are structurally different and stuffing both into one prompt
just produces bloated Shorts prose.

Output shape: :class:`pipeline.llm.script_schema.ScriptEnvelope` with
``kind="long_form"`` and a fully-populated
:class:`pipeline.llm.script_schema.LongFormScript` payload, including
panel cues when ``visual_mode == longform_panels``.

Routes through the same backend dispatcher the Shorts rewriter uses
(``pipeline.llm.cli.call_claude_cli`` → Azure OpenAI in cloud,
``claude`` CLI on laptop). Uses the ``opus`` model tier for higher-
quality structured output on long content.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any

from . import cli as _llm
from .script_schema import (
    LongFormPanel,
    LongFormScript,
    LongFormSection,
    ScriptEnvelope,
)

_logger = logging.getLogger(__name__)


_PROMPT_TEMPLATE = """\
You are a long-form narration writer for a YouTube channel.

Channel context (the visual aesthetic + narrator persona this video
will be produced in):
{channel_context}

Topic: {topic}

User's notes / raw source material (use this if substantive; expand
with reasonable creative inferences if sparse):
{notes}

Target duration: {duration_min} minutes ({duration_s} seconds total).
Author a long-form sectioned narration that fills the WHOLE target
duration without filler. The narration will be synthesised by a calm
TTS narrator at roughly 145-155 wpm.

LENGTH CONTRACT (HARD — the rewrite is REJECTED if violated):
- Total narration MUST contain a MINIMUM of {words_floor} words AND a
  MAXIMUM of {words_ceiling} words. The post-rewrite validator
  hard-fails any script outside this range.
- Distribute words across {section_count_target} sections (6-12
  typical) of roughly {section_words_target} words each.
- Each section MUST contain at least {section_words_floor} words.
  Do NOT trail off in later sections — sustain density end-to-end.
  The "tired-by-the-end" pattern (sections 0-3 dense, 6-9 sparse)
  triggers a hard rejection.

STRUCTURE — return a SINGLE JSON object with EXACTLY these top-level
keys:

  hook            — string, 30-60 seconds of opening narration that
                    establishes the topic + emotional frame. The
                    listener should feel "I have to know how this ends"
                    by the end of the hook.
  thesis          — string, ONE sentence (≤ 25 words). The single
                    claim or question the whole video proves /
                    explores. Surfaced in the description; NOT
                    narrated verbatim.
  sections        — array of {{id, title, narration, target_s,
                    visual_brief}} objects. id is a kebab-case slug
                    unique within this video. title is a short
                    chapter label. narration is the actual section
                    text the narrator reads (no "Section 1:"
                    prefixes — just the prose). target_s is the
                    rewriter's intended duration in seconds (a hint
                    for the renderer; audio length wins). visual_brief
                    is ONE sentence describing what the camera /
                    illustration should show during this section.
  panels          — array of {{scene, hold_s}} objects. AT LEAST
                    {panel_min_per_section} panels per section
                    (target {panel_count_target} total panels). scene
                    is a detailed image-gen prompt (the channel's
                    image_style_prefix is prepended at render time, so
                    DON'T repeat aesthetic instructions like "flat 2D
                    crayon" — just describe WHAT is in the frame:
                    subject, action, props, mood, composition).
                    hold_s is how long this panel stays on screen
                    before crossfading to the next.
                    HARD CAP: hold_s MUST be ≤ {panel_hold_max_s}s
                    (any panel with hold_s > {panel_hold_max_s}s
                    triggers a rewrite rejection — long-form panels
                    held >12s without movement read as DEAD on
                    screen). Default 6-8 s.
  sources         — array of strings. Optional — URLs / refs the
                    video draws from (cited in the description's
                    Sources block). Empty array if no specific
                    sources.
  title_options   — array of 3-5 candidate YouTube titles for THIS
                    video. The first will be used; the rest are A/B
                    alternates.

CRAFT RULES that drive watch-time on long-form (NON-NEGOTIABLE):

- Each section must end on a small open loop (a question, a tension,
  a "what they didn't know was…") that pulls the viewer into the next
  section. Long-form viewers ABANDON at section boundaries; the open
  loop is what holds them.

- VARY sentence length across sections. Hook + reveal sections lean
  long-and-explanatory; pivot moments lean short-and-punchy. Don't
  let the prose flatten into one register.

- Concrete > abstract. EVERY section should anchor in at least one
  specific detail (a number, a date, a place, a named person, a
  named object, a specific quote). Long-form is where listeners
  notice padding — the moment narration drifts into "and then things
  got worse" abstractions, retention drops.

- DON'T repeat across sections. If section 3 establishes a fact, don't
  re-establish it in section 6 ("as we discussed earlier…"). Trust
  the listener.

- SOURCE FIDELITY (HARD). The narration MUST be traceable to the
  user's raw source material above. Do NOT introduce stock
  anecdotes — these are CLASS-OF-BUG drift patterns the LLM falls
  into when the source is thin. The post-rewrite validator hard-rejects
  any narration containing the following BANNED phrases:
    * British Cycling / "marginal gains" / Dave Brailsford
    * Steve Jobs / Stanford commencement speech
    * Roger Bannister / four-minute mile
    * Stanford marshmallow test / experiment
    * 10,000-hour rule
    * Sara Blakely / Spanx
    * Kobe Bryant work-ethic / Michael Jordan cut-from-team
    * "Boiling frog" metaphor
    * Atomic Habits / James Clear / Malcolm Gladwell / Simon Sinek
    * Compound-interest framing
  If the source is too thin to fill the target duration, expand by
  exploring DETAILS of the source (dialogue, sensory description,
  named locations, specific times) — never by importing TED-Talk
  filler.

- PANEL SCENE RULES (CLASS-OF-BUG fix from 2026-05-13 critique):
  * Each panel scene must be CONCRETE and STAGED — name the subject,
    what they're doing, what's in their hands, the environment, the
    lighting. Avoid abstract / metaphor scenes; the image generator
    can't render abstraction.
  * NO READABLE TEXT IN PANELS. If a panel features a phone screen,
    sign, book, poster, chalkboard, label, or document, append the
    instruction "AVOID all readable text, captions, words, or
    letters in the image — use abstract shapes or icons only" to
    the scene. The image model otherwise hallucinates garbled fake
    text that breaks immersion in close-ups.

- CHARACTER CONSISTENCY (this is the #1 long-form quality bug — fix it
  in your output, not in render):
    * Decide ONE character spec for the protagonist (and named
      supporting cast) BEFORE writing any panel. A character spec is
      a 1-2 sentence physical description: age range (e.g. "30s"),
      gender presentation, hair (length + colour), skin tone, build
      AND BODY PROPORTIONS (e.g. "petite, 5'2 frame" / "broad
      shoulders, athletic build" — drift across panels is a real
      problem when proportions aren't pinned), clothing palette, and
      any signature prop. Channel context above may already define a
      "Recurring character" — when present, USE THAT EXACTLY as the
      protagonist spec; do not invent a new one.
    * In every panel scene that features that character, repeat the
      WHOLE character spec verbatim at the start of the scene
      description. Yes, repeat the same words across all 24 panels.
      The image generator has no memory between panels — if you
      describe the protagonist as "a young child" in panel 2 and "an
      elderly man" in panel 5, you WILL get a young child and an
      elderly man, not the same person at two ages. This is
      non-negotiable.
    * If the story spans years, keep the character spec the same
      anyway — viewers tolerate a flat-art protagonist who looks the
      same age throughout much better than a protagonist who morphs
      between random ages every panel.
    * Landscape / inanimate panels (no people) don't need the
      character spec.

- TITLE OPTIONS (per-niche tonal anchor — RECOMMENDED, surfaced in
  metadata to the validator):
{niche_title_rules}

- CLOSER (the LAST section MUST have a closer beat — drives
  comments + subscribes):
    * End the final section with a BINARY OPINION question that
      invites a YES/NO comment ("Was X real or did she imagine it? —
      drop YES or NO in the comments"), NOT an open "what do you
      think" mush.
    * Add an explicit subscribe pitch in the last 2 sentences
      ("subscribe to {channel_name} for more like this").
    * Reference the source by name where natural (e.g.
      "the original r/nosleep post is linked in the description").

OUTPUT: ONE JSON object matching the structure above. No prose
preface, no markdown fence. Just the object.
"""


_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "hook", "thesis", "sections", "panels", "sources", "title_options",
    ],
    "properties": {
        "hook": {"type": "string", "minLength": 100, "maxLength": 3000},
        "thesis": {"type": "string", "minLength": 5, "maxLength": 250},
        "sections": {
            "type": "array",
            "minItems": 3,
            "maxItems": 30,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "narration"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 80},
                    "title": {"type": "string", "minLength": 1, "maxLength": 120},
                    "narration": {"type": "string", "minLength": 50},
                    "target_s": {"type": ["number", "null"]},
                    "visual_brief": {"type": ["string", "null"]},
                },
            },
        },
        "panels": {
            "type": "array",
            "minItems": 0,
            "maxItems": 60,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["scene"],
                "properties": {
                    "scene": {"type": "string", "minLength": 20, "maxLength": 1500},
                    "hold_s": {"type": ["number", "null"]},
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


def _channel_context(channel_cfg: dict[str, Any]) -> str:
    """One-paragraph description of the channel + its long-form aesthetic.

    Pulled from the channel YAML's name + long_form.image_style_prefix
    + character_description so the LLM gets a concrete sense of the
    visual world without us redefining each channel here."""
    parts: list[str] = []
    name = channel_cfg.get("name") or channel_cfg.get("channel") or "this channel"
    parts.append(f"Channel: {name}.")

    lf = channel_cfg.get("long_form") or {}
    style = (lf.get("image_style_prefix") or
             channel_cfg.get("image_style_prefix") or "").strip()
    if style:
        # Compress newlines so the prompt stays single-paragraph friendly.
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
    # Floor + ceiling enforced by post-rewrite validator.
    # 0.92 / 1.10 around target — narrow enough to catch under-delivery,
    # wide enough not to fight the natural variance.
    words_floor = int(words_target * 0.92)
    words_ceiling = int(words_target * 1.10)
    # Roughly one section per ~3 minutes of narration, capped to 6-12.
    section_count_target = max(6, min(12, max(1, duration_s // 180)))
    section_words_target = max(50, words_target // max(1, section_count_target))
    # Per-section minimum (catches the tired-by-the-end LLM degradation
    # pattern observed on job 0c05c335 — sections 6-9 hit only 48% of
    # mean while sections 0-3 hit 65%). 70% of mean is the soft floor
    # the validator enforces.
    section_words_floor = int(section_words_target * 0.70)
    # Panels: target one per ~6-8 s of narration so panels never hold
    # >12 s on screen (post-2026-05-13 fix — was one per ~30-45 s).
    # No prompt-side ceiling on count: the renderer's PANEL_HARD_CAP is
    # mode-aware (24 on local mflux, 60 on Cloud Run NVIDIA L4 where
    # the Metal command-buffer watchdog doesn't exist). Truncation
    # happens at parse time as defense-in-depth if the LLM ever exceeds.
    panel_seconds = 7
    panel_count_target = max(8, max(1, duration_s // panel_seconds))
    # At least 4 panels per section so no section has 30+ s of static.
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


# ---------- per-niche title rule injection -------------------------------
# Per-niche tonal anchor for title_options. Surfaced in the prompt so
# the rewriter LLM has concrete examples instead of inventing
# chain-letter-spammy variants like "If You Can See This… Keep Reading"
# (which is what shipped on job 0c05c335 for a r/nosleep render).
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
        return "    * (no niche-specific title rule registered)"
    key = niche.strip().lower()
    if key in _NICHE_TITLE_RULES:
        return _NICHE_TITLE_RULES[key]
    bare = key.removeprefix("r/").removeprefix("reddit_").strip("_/ ")
    if f"r/{bare}" in _NICHE_TITLE_RULES:
        return _NICHE_TITLE_RULES[f"r/{bare}"]
    return "    * (no niche-specific title rule registered for " + niche + ")"


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


def rewrite_long_form(
    raw_story: dict[str, Any],
    *,
    channel_cfg: dict[str, Any] | None = None,
    target_duration_s: int = 1800,
    title_options_count: int = 4,
    spec: Any = None,
) -> ScriptEnvelope:
    """Author a long-form ``ScriptEnvelope(kind="long_form")``.

    Args:
        raw_story: ``{slug, title, body, source, url}`` — same shape the
            Shorts rewriter accepts.
        channel_cfg: Merged channel YAML cfg (base + variant overlay).
            Provides aesthetic + render_mode context for the prompt.
        target_duration_s: Target final video duration in seconds.
            Drives the narration word budget + section/panel counts.
        title_options_count: Hint for the prompt (LLM may return
            slightly more or fewer).
        spec: Optional :class:`pipeline.render.spec.RenderSpec` — when
            provided, descriptor-registered prompt patches
            (:func:`pipeline.render.input_registry.prompt_patches_for`)
            are collected and injected into the prompt. Lets a
            ``audio_mode=song`` form pick switch the rewriter to lyrics
            mode, ``narrator_visual_mode=voice_only`` suppress narrator
            on-screen instructions, etc — all driven by descriptor
            metadata, not hardcoded prompt branches. Pre-Slice-2.P3 the
            prompt was rigid; the unified renderer
            (:func:`pipeline.render.video.render_long_form`) now passes
            spec=spec to enable per-form-input prompt biasing.

    Returns:
        Fully-populated :class:`ScriptEnvelope` with ``kind="long_form"``.

    Raises:
        ValueError: if raw_story has no title or body.
        ClaudeCLIError: on backend / parse / validation failure (bubbled
            from the LLM dispatcher).
    """
    title = (raw_story.get("title") or "").strip()
    body = (raw_story.get("body") or "").strip()
    if not title and not body:
        raise ValueError("raw_story has no title or body")

    cfg = channel_cfg or {}
    duration_s = max(120, int(target_duration_s))  # min 2 minutes
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

    # Niche resolution for per-niche title rules + post-rewrite validator.
    niche = _niche_from_spec_or_cfg(spec, cfg)
    niche_title_rules = _niche_title_rules_block(niche)
    channel_name = (cfg.get("name") or cfg.get("channel") or "this channel").strip()

    # Collect descriptor-driven prompt patches if a spec is supplied.
    # Each registered patch (audio_mode_branch, narrator_visual_branch,
    # visual_source_branch, …) contributes context_lines + craft_rules
    # the LLM needs to know about; mode-changing patches set schema_mode
    # which the rewriter respects (e.g. lyrics output instead of prose).
    prompt_patch = None
    if spec is not None:
        try:
            from pipeline.render.input_registry import prompt_patches_for  # noqa: PLC0415
            prompt_patch = prompt_patches_for(spec)
            if prompt_patch.context_lines or prompt_patch.craft_rules:
                _logger.info(
                    "rewrite_long_form: descriptor patches — %d context line(s), "
                    "%d craft rule(s), schema_mode=%r",
                    len(prompt_patch.context_lines),
                    len(prompt_patch.craft_rules),
                    prompt_patch.schema_mode,
                )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("prompt_patches_for failed: %s — proceeding "
                            "with bare prompt", exc)

    extra_context = ""
    extra_rules = ""
    if prompt_patch:
        if prompt_patch.context_lines:
            extra_context = "\n\nADDITIONAL CONTEXT (from form picks):\n- " + \
                "\n- ".join(prompt_patch.context_lines)
        if prompt_patch.craft_rules:
            extra_rules = "\n\nADDITIONAL CRAFT RULES (from form picks):\n- " + \
                "\n- ".join(prompt_patch.craft_rules)

    # Import here so the new module isn't a top-level dep of the
    # rewriter (keeps test isolation cleaner; the critic module is
    # standalone and imports nothing from the rewriter).
    from pipeline import critic_long_form as _critic  # noqa: PLC0415

    base_prompt = _PROMPT_TEMPLATE.format(
        channel_context=_channel_context(cfg) + extra_context,
        topic=title,
        notes=notes,
        duration_min=duration_min,
        duration_s=duration_s,
        words_floor=words_floor,
        words_ceiling=words_ceiling,
        section_count_target=section_count_target,
        section_words_target=section_words_target,
        section_words_floor=section_words_floor,
        panel_count_target=panel_count_target,
        panel_min_per_section=panel_min_per_section,
        panel_hold_max_s=int(_critic.PANEL_HOLD_HARD_MAX_S),
        niche_title_rules=niche_title_rules,
        channel_name=channel_name,
    ) + extra_rules

    _logger.info(
        "rewrite_long_form: topic=%s duration=%ds → planning words "
        "[floor=%d, target=%d, ceiling=%d] / %d sections (≥%d words each) / "
        "%d panels (≥%d/section, hold≤%ds) niche=%s",
        title[:60], duration_s, words_floor, words_target, words_ceiling,
        section_count_target, section_words_floor,
        panel_count_target, panel_min_per_section,
        int(_critic.PANEL_HOLD_HARD_MAX_S), niche,
    )

    # Audit D3.47 — pre-fix this hardcoded model="opus" so the
    # YTFACTORY_MODEL_REWRITE_LONG_FORM env override (which the
    # dispatcher's model_for() helper respects) was silently ignored.
    # Now: route through model_for("rewrite_long_form") so an operator
    # can flip to sonnet/haiku for fast iteration without a code edit.
    #
    # Contract retry loop (added 2026-05-13). After every rewrite,
    # validate against pipeline.critic_long_form.validate_long_form_envelope.
    # On HARD violations, append the violation summary to the prompt
    # and retry ONCE. After the second hard failure, raise
    # LongFormContractError so the worker marks the job FAILED with
    # a clear "rewrite contract failed" error rather than spending
    # ~$1 of TTS + image budget on a script that's already wrong.
    max_attempts = 2
    last_violations: list = []
    last_env: ScriptEnvelope | None = None
    prompt = base_prompt
    for attempt in range(1, max_attempts + 1):
        raw = _llm.call_claude_cli(
            prompt,
            output_json=True,
            json_schema=_SCHEMA,
            model=_llm.model_for("rewrite_long_form"),
            stage="rewrite_long_form",
        )

        if not isinstance(raw, dict):
            raise RuntimeError(
                f"rewrite_long_form: LLM returned {type(raw).__name__}, "
                f"expected dict (output_json=True)"
            )

        # Cap any panel hold_s the LLM emitted past PANEL_HOLD_HARD_MAX_S
        # right at parse time. The validator below ALSO catches this and
        # would trigger a retry, but the cap means a marginal violation
        # (one panel at 13s) doesn't burn an LLM round-trip.
        for p in raw.get("panels") or []:
            if isinstance(p, dict):
                h = p.get("hold_s")
                if h is not None:
                    try:
                        h_f = float(h)
                    except (TypeError, ValueError):
                        h_f = _critic.PANEL_HOLD_SOFT_MAX_S
                    if h_f > _critic.PANEL_HOLD_HARD_MAX_S:
                        p["hold_s"] = _critic.PANEL_HOLD_SOFT_MAX_S

        sections = [
            LongFormSection(
                id=str(s["id"]).strip(),
                title=str(s["title"]).strip(),
                narration=str(s["narration"]).strip(),
                target_s=(float(s["target_s"]) if s.get("target_s") is not None else None),
                visual_brief=(s["visual_brief"].strip() if s.get("visual_brief") else None),
            )
            for s in raw.get("sections") or []
        ]

        panels = [
            LongFormPanel(
                scene=str(p["scene"]).strip(),
                # Default hold_s drops 30.0 → 6.0 (post-2026-05-13). Long-form
                # panels held >12s read as DEAD; this default plus the cap
                # in the loop above means even a misbehaving LLM can't
                # produce a static long-form anymore.
                hold_s=float(p.get("hold_s") or 6.0),
            )
            for p in raw.get("panels") or []
        ]
        # Defensive truncation: the renderer enforces PANEL_HARD_CAP
        # (24 on local mflux, 60 on Cloud Run NVIDIA L4). The mode-
        # aware ceiling lives in pipeline/render/long_form.py — we
        # truncate here at the more permissive Cloud Run ceiling so
        # cloud-bound renders aren't artificially limited. Local
        # renders re-truncate downstream against their lower cap.
        if len(panels) > 60:
            _logger.warning(
                "rewrite_long_form: LLM emitted %d panels; truncating to 60 "
                "(renderer's max PANEL_HARD_CAP across providers)",
                len(panels),
            )
            panels = panels[:60]

        long_form = LongFormScript(
            hook=str(raw["hook"]).strip(),
            thesis=str(raw["thesis"]).strip(),
            sections=sections,
            panels=panels,
            sources=[str(s).strip() for s in (raw.get("sources") or []) if s],
        )

        env = ScriptEnvelope(
            slug=raw_story.get("slug") or "untitled",
            kind="long_form",
            title_options=[str(t).strip() for t in (raw.get("title_options") or [])][:title_options_count or 4],
            source_url=raw_story.get("url", ""),
            source=raw_story.get("source", ""),
            long_form=long_form,
        )
        last_env = env

        violations = _critic.validate_long_form_envelope(
            env, target_duration_s=duration_s, niche=niche,
        )
        last_violations = violations
        hard = [v for v in violations if v.severity == "hard"]
        soft = [v for v in violations if v.severity == "soft"]

        if not hard:
            if soft:
                _logger.warning(
                    "rewrite_long_form: %d soft violation(s) — proceeding: %s",
                    len(soft), "; ".join(v.code for v in soft),
                )
            break

        # Hard violation → log, append retry guidance, retry.
        _logger.warning(
            "rewrite_long_form: attempt %d/%d failed contract — "
            "%d hard violation(s): %s",
            attempt, max_attempts, len(hard),
            "; ".join(v.code for v in hard),
        )
        if attempt < max_attempts:
            prompt = (
                base_prompt
                + "\n\n"
                + _critic.render_violations_for_retry_prompt(violations)
            )
        else:
            raise _critic.LongFormContractError(violations)

    assert last_env is not None  # defended by the loop break above
    env = last_env

    _logger.info(
        "rewrite_long_form: produced envelope slug=%s sections=%d panels=%d "
        "narration_words≈%d title_options=%d soft_violations=%d",
        env.slug, len(env.long_form.sections), len(env.long_form.panels),
        sum(len(s.narration.split()) for s in env.long_form.sections),
        len(env.title_options),
        sum(1 for v in last_violations if v.severity == "soft"),
    )
    return env


__all__ = ["rewrite_long_form"]
