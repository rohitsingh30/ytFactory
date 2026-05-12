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
TTS narrator at roughly 145-155 wpm, so plan ~{words_target} total
narration words distributed across {section_count_target} sections of
roughly {section_words_target} words each (the section count is a
HINT — return whatever number of sections gives the best pacing for
THIS topic; 6-12 is typical).

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
  panels          — array of {{scene, hold_s}} objects. Roughly ONE
                    panel per ~{panel_seconds}s of narration. scene is a
                    detailed image-gen prompt (the channel's
                    image_style_prefix is prepended at render time, so
                    DON'T repeat aesthetic instructions like "flat 2D
                    crayon" — just describe WHAT is in the frame:
                    subject, action, props, mood, composition).
                    hold_s is how long this panel stays on screen
                    before crossfading to the next (default 30s for
                    long-form pacing). Aim for {panel_count_target}
                    total panels.
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

- Panels: each panel scene must be CONCRETE and STAGED — name the
  subject, what they're doing, what's in their hands, the
  environment, the lighting. Avoid abstract / metaphor scenes; the
  image generator can't render abstraction.

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


def _planned_sections_and_panels(duration_s: int) -> tuple[int, int, int, int, int]:
    """Heuristic targets for the LLM prompt. Hint-only — the LLM may
    return any reasonable number that fits the topic.

    Returns ``(words_target, section_count_target, section_words_target,
    panel_count_target, panel_seconds)``.
    """
    # Calm TTS narrator @ ~150 wpm.
    words_target = int(duration_s * 150 / 60)
    # Roughly one section per ~3 minutes of narration, capped to 6-12.
    section_count_target = max(6, min(12, max(1, duration_s // 180)))
    section_words_target = max(50, words_target // max(1, section_count_target))
    # One panel per ~30-45 s. Long-form pacing: avoid <20s holds.
    panel_seconds = 35
    panel_count_target = max(8, min(24, max(1, duration_s // panel_seconds)))
    return (
        words_target,
        section_count_target,
        section_words_target,
        panel_count_target,
        panel_seconds,
    )


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
        section_count_target,
        section_words_target,
        panel_count_target,
        panel_seconds,
    ) = _planned_sections_and_panels(duration_s)

    notes = body or "(no extra notes provided — author from the topic alone)"

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

    prompt = _PROMPT_TEMPLATE.format(
        channel_context=_channel_context(cfg) + extra_context,
        topic=title,
        notes=notes,
        duration_min=duration_min,
        duration_s=duration_s,
        words_target=words_target,
        section_count_target=section_count_target,
        section_words_target=section_words_target,
        panel_count_target=panel_count_target,
        panel_seconds=panel_seconds,
    ) + extra_rules

    _logger.info(
        "rewrite_long_form: topic=%s duration=%ds → planning ~%dw / "
        "%d sections / %d panels",
        title[:60], duration_s, words_target,
        section_count_target, panel_count_target,
    )

    raw = _llm.call_claude_cli(
        prompt,
        output_json=True,
        json_schema=_SCHEMA,
        model="opus",  # long-form benefits from the larger context model
        stage="rewrite_long_form",
    )

    if not isinstance(raw, dict):
        raise RuntimeError(
            f"rewrite_long_form: LLM returned {type(raw).__name__}, "
            f"expected dict (output_json=True)"
        )

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
            hold_s=float(p.get("hold_s") or 30.0),
        )
        for p in raw.get("panels") or []
    ]

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

    _logger.info(
        "rewrite_long_form: produced envelope slug=%s sections=%d panels=%d "
        "narration_words≈%d title_options=%d",
        env.slug, len(sections), len(panels),
        sum(len(s.narration.split()) for s in sections),
        len(env.title_options),
    )
    return env


__all__ = ["rewrite_long_form"]
