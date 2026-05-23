"""LLM prompt-refiner pre-step for Z-Image-Turbo (sole production image model).

Why this exists
---------------

Z-Image-Turbo (6B S3-DiT, CFG-distilled, ``guidance_scale=0.0``) ignores
negative prompts entirely — every "no X" instruction in our prompts is
linguistic-only and loses to training-data attractors. The model wants
**rich, positive, structured prompts** in the 80-250-word sweet spot.
Short 4-10-word noun phrases (the previous FLUX.2 klein calibration)
underspecify: z-turbo fills the gaps with whatever its training data
correlates most strongly with the few tokens it got — generic product-
photo backgrounds, blank rooms, floating-object compositions.

This module closes that gap by taking the authored ``{key_visual, scene}``
per beat plus channel context and emitting structured z-turbo-optimised
fields. The refiner does **not** own the final prompt string. Render-time
invariants — era anchor, character description / cast lock, kit lock —
remain code-owned in ``pipeline/images/images.py::build_full_prompt``.
The refiner only owns the subject description, the environment, the
lighting, and the style/medium/mood block.

Output schema (per beat)
------------------------

::

    {
      "refined_visual":      "<polished noun phrase, 4-10 words>",
      "refined_scene":       "no readable text in image. <shot>, <lighting>, <posture/action>, <setting>",
      "style_block":         "Style: <s>. Mood: <m>.",
      "refined_version":     "v1",
      "refined_input_hash":  "<sha256[:16] of stable input tuple>"
    }

``refined_scene`` starts with the literal canonical anti-text wording so
the existing
:func:`pipeline.images.images_cloudrun._append_anti_text_suffix`
idempotence check (line 418, ``"no readable text in image" in prompt.lower()``)
collapses to a no-op and we don't double-suffix.

Render-time consumer rules
--------------------------

A render uses the refined fields only when **all** of the following hold
(checked by the caller, not this module):

1. ``YTFACTORY_PROMPT_REFINER=1`` env flag is set at render time.
2. All three of ``refined_visual``, ``refined_scene``, ``style_block`` are
   present and truthy on the beat.
3. ``refined_input_hash`` matches the freshly-recomputed hash for the
   current beat / era / character / style / mood inputs (so any critic
   patch to ``scene``, any cast change, etc., auto-invalidates the cache).
4. ``refined_version`` equals :data:`REFINER_VERSION`.

Failure modes
-------------

- Whole-batch LLM failure → returns ``N`` empty dicts → every beat falls
  back to the legacy path. Zero regression.
- Per-beat malformed output (missing field, anti-text prefix missing,
  text-bait sneaks back in after refining) → that beat's slot becomes
  ``{}`` → that beat only falls back. Other beats keep their refined
  fields.
- LLM never sees attractor words from the narration: every input is run
  through :func:`sanitize_attractors` before being shown to the model.

"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Callable

from pipeline import observability as _obs
from pipeline.render.artifacts import emit_artifact_json

logger = logging.getLogger(__name__)


def _track_refiner_fallback(beat_index: int, reason: str) -> None:
    try:
        _obs.track(
            "image.refiner.fallback",
            category="image",
            success=False,
            metadata={"beat_index": beat_index, "reason": reason},
        )
    except Exception:  # noqa: BLE001
        pass


def _raw_response_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return repr(value)


def _emit_refiner_io_artifact(
    *,
    input_batch: dict[str, Any],
    raw_response_str: str | None,
    parsed: Any,
    fallback_count: int,
) -> None:
    try:
        job_id = os.environ.get("YTFACTORY_JOB_ID") or None
        if not job_id:
            return
        emit_artifact_json(
            job_id,
            "refiner_io",
            {
                "input_batch": input_batch,
                "raw_response": raw_response_str,
                "parsed": parsed,
                "fallback_count": fallback_count,
            },
            filename="refiner_io.json",
        )
    except Exception:  # noqa: BLE001
        pass


REFINER_VERSION = "v2-zturbo"  # 2026-05-23 P4.1: bumped from v1 (klein) to
# v2-zturbo so cached refined-* fields from the klein era auto-invalidate
# at render time via the input-hash mismatch in ``refined_fields_for_render``.

# Camera rotation per beat — composition vocabulary that elicits distinct
# latents on Z-Image-Turbo. Cycling through forces visual diversity across
# the rolling 3-beat window (the author-side _SYSTEM Rule 10 already
# instructs the LLM to vary composition, but it doesn't inject explicit
# shot tokens — this rotation does).
#
# The exact wording matters: BFL docs (prompting_unified_reference.md,
# Composition Techniques) recommend "wide shot", "medium shot",
# "close-up shot", "over-the-shoulder", "bird's-eye view", etc.; we wrap
# each with the spatial cue that empirically pushes FLUX out of the
# centroid "single character, medium shot, neutral background" attractor.
SHOT_ROTATION: tuple[str, ...] = (
    "wide establishing shot, small subject in large environment",
    "medium shot, waist-up, subject centered",
    "close-up shot, face and shoulders, tight framing",
    "over-the-shoulder angle, depth implied",
    "bird's-eye view overhead, geometric top-down perspective",
    "extreme close-up, macro detail, texture visible",
    "low-angle worm's-eye view, dramatic upward perspective",
    "medium shot from side profile, ninety-degree angle",
    "point-of-view shot, first-person perspective",
)


def shot_for_beat(beat_index: int) -> str:
    """Return the canonical shot-type token for a given beat index.

    Cycles through :data:`SHOT_ROTATION` modulo its length. Callers pass
    this to the refiner system prompt as the per-beat ``shot_type``
    directive.
    """
    return SHOT_ROTATION[beat_index % len(SHOT_ROTATION)]


# Attractor-word rewrite map — script terms that pattern-match
# YouTube-tutorial / how-to-grow-your-channel training data and pull FLUX
# toward platform-UI hallucinations (thumbs-up buttons, bell icons,
# Subscribe banners). Applied to scene/key_visual BEFORE the refiner LLM
# call so the model never sees the trigger word. The original narration
# (used by TTS and captions) is unaffected — only the image-prompt path
# is sanitised.
#
# Each entry is (pattern, replacement). Patterns are case-insensitive.
# The list is empirical and short on purpose — the refiner system prompt
# also instructs the LLM to avoid platform-UI concepts, so this is the
# first line of defence, not the only one.
_ATTRACTOR_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Narrow phrasings only — bare "viral" matches medical/science usage
    # ("viral infection", "viral load") which we must NOT mangle. The
    # YouTube-tutorial attractor specifically fires on social phrasings.
    # Rubber-duck 2026-05-14 narrowed this from a bare "viral" match.
    (re.compile(r"\bgoing\s+viral\b", re.I), "becoming popular"),
    (re.compile(r"\bviral\s+(?:video|videos|post|posts|clip|clips|tweet|tweets|content)\b", re.I),
     "popular content"),
    (re.compile(r"\btrending\b", re.I), "popular"),
    (re.compile(r"\bsubscribe(?:r|rs|d)?\b", re.I), "follow"),
    (re.compile(r"\blike\s+button\b", re.I), "thumbs-up gesture"),
    (re.compile(r"\bnotification\s+bell\b", re.I), "reminder"),
    (re.compile(r"\bclickbait\b", re.I), "eye-catching headline"),
    (re.compile(
        r"\bset\s+(?:a\s+)?(?:tiny|small|little)?\s*(?:legal\s+)?trap\b", re.I
    ), "left a small note"),
    (re.compile(r"\bcontent\s+creator\b", re.I), "storyteller"),
    (re.compile(r"\bYouTube\b", re.I), "the platform"),
)


def sanitize_attractors(text: str) -> str:
    """Rewrite attractor trigger phrases before they reach the refiner LLM.

    Idempotent under repeated application; runs in linear time over the
    pattern list. Returns the input unchanged if it doesn't contain any
    triggers.

    The transformation is for the **image-prompt** path only — the same
    underlying narration is preserved verbatim for TTS and on-screen
    captions. See :doc:`docs/post-audit-2026-05-14.md` for the full
    "two-track text" pattern.
    """
    if not text:
        return text
    for pat, repl in _ATTRACTOR_REWRITES:
        text = pat.sub(repl, text)
    return text


def compute_input_hash(
    *,
    beat: dict[str, Any],
    era_anchor_prefix: str | None,
    character_description: str | None,
    style: str | None,
    mood: str | None,
) -> str:
    """Stable 16-char hash of the refiner inputs for a single beat.

    Cache validity: when the renderer reads ``refined_input_hash`` off a
    cached beat and recomputes via this function, a mismatch means the
    cache is stale (the authored ``scene`` was critic-patched, the cast
    description changed, the era anchor was retuned, the channel mood
    moved, or :data:`REFINER_VERSION` was bumped). Stale → fall back to
    the legacy path for that beat.

    Why 16 chars: collision rate at our volume (millions of beats over
    the channel lifetime) is negligible; full 64-char SHA256 is wasteful
    in the cached JSON.
    """
    parts = (
        REFINER_VERSION,
        (beat.get("key_visual") or "").strip(),
        (beat.get("scene") or "").strip(),
        (era_anchor_prefix or "").strip(),
        (character_description or "").strip(),
        (style or "").strip(),
        (mood or "").strip(),
    )
    # Unit-separator joins so accidental whitespace in one field can't
    # collide with another field's content.
    blob = "\u241f".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# The refiner system prompt — calibrated 2026-05-23 for Z-Image-Turbo
# (P4.1, Q67-Q69). Replaces the prior FLUX.2 klein calibration whose 4-10
# word ``refined_visual`` underspecified z-turbo and let it default to
# generic product-photo backgrounds. Z-turbo wants 80-250 words structured
# across the (Shot+Subject / Age+Appearance / Clothing+Palette /
# Environment / Lighting / Mood / Style+Medium / Safety) slots.
_REFINER_SYSTEM = """\
You are a Z-Image-Turbo prompt specialist. You receive an authored beat
({key_visual, scene}) plus channel context (style, mood, era anchor,
character description). Rewrite the visual fields to maximise
instruction-following on the 6B S3-DiT Z-Image-Turbo diffusion model.

MODEL CONTEXT — IMPORTANT:
Z-Image-Turbo is CFG-distilled at ``guidance_scale=0.0``. There is NO
classifier-free guidance, so negative prompts are IGNORED at the model
level — every "no X" instruction is linguistic-only and loses to
training-data attractors. Convert every negation to a POSITIVE
construction. Examples:
  - "no hat" → "bareheaded"
  - "not smiling" → "neutral expression"
  - "no people" → "empty scene"
  - "no signs visible" → "plain walls"
The ONE exception is the canonical "no readable text in image" anti-text
prefix — keep it verbatim because the pipeline depends on the exact wording.

Z-Image-Turbo behaves best at 80-250 words of densely-described visual
detail. Below ~60 words it under-specifies and falls back to generic
product-photo composition. Above ~300 words the later tokens lose
weight. Aim for ~120-180 words of combined output across the three
fields.

OUTPUT — a JSON array of objects, one per input beat, in beat order. Each
object MUST have exactly these three fields and no others:

  {
    "refined_visual":  "<dense subject description, 30-60 words, structured: shot type + subject + age band + appearance + clothing + signature props>",
    "refined_scene":   "no readable text in image. <environment description, 40-80 words: setting + props + spatial composition + atmosphere + LIGHTING tokens>",
    "style_block":     "Style: <medium + technique + 2-4 visual qualities>. Mood: <2-4 mood adjectives + emotional register>."
  }

PER-BEAT RULES (a beat failing any rule has its slot replaced with {} by
the caller, and that beat falls back to the legacy non-refined path):

1. refined_scene MUST start with the literal string "no readable text in
   image. " (lower-case, period, space). Do not paraphrase this prefix —
   downstream idempotence depends on the exact wording.

2. Inject EXACTLY ONE shot_type from the rotation the user message gives
   you, at the START of refined_visual. Do not invent shot types. Do
   not skip.

3. Inject AT LEAST ONE high-impact LIGHTING token into refined_scene.
   Z-Image-Turbo lighting vocabulary that lands cleanly: "soft diffused
   daylight", "cinematic warm key light from the left", "noir
   high-contrast side lighting", "rim lighting against a dark
   background", "golden-hour backlight", "harsh overhead noon sun",
   "cold blue moonlight wash", "neon storefront glow", "fluorescent
   office overhead", "candlelit warm tungsten". Vary across beats —
   repeating the same lighting two beats in a row defeats the purpose.

4. NEVER write any of: speech bubble, thought bubble, comic panel,
   chalkboard, whiteboard, computer screen showing text, phone screen
   showing text, sign, billboard, poster, label, license plate, name tag,
   certificate, invitation, contract, receipt, scoreboard, clock face
   with numbers, YouTube logo, Subscribe button, Like button, thumbs-up
   button, bell icon, play button, video UI. Convey emotion via POSTURE,
   FACIAL EXPRESSION, and ENVIRONMENT only.

5. NEVER render metaphors literally. "rocket of a shot" → "hard-struck
   ball". "thunderbolt" → "powerful strike". The same applies to all
   sports / action figurative language.

6. ONE main subject per beat. The user message gives you a canonical
   character_description — the renderer prepends it verbatim at compose
   time, so DO NOT re-describe the character's age/build/hair/clothing
   in refined_visual. Use only the SHOT + ACTION + SUBJECT-relative
   posture. If the story introduces a SECONDARY character, describe
   that secondary character concretely once in refined_visual.

7. Use POSITIVE constructions exclusively. Replace every "no X" or
   "without Y" with the positive equivalent. The anti-text prefix
   (rule 1) is the only exception.

8. style_block: build "Style: ..." from the provided style (medium /
   technique / 2-4 visual qualities) and "Mood: ..." from the provided
   mood (2-4 adjectives + emotional register). If either input is
   missing, expand to a neutral-but-rich placeholder ("Style: warm
   hand-drawn 2D illustration with confident ink line work and visible
   watercolor brush texture. Mood: calm, observational, lightly
   melancholic.").

9. Total word count across the three fields should land at ~120-180
   words. Aim higher for cinematic beats, lower for static establishing
   shots — but never below ~80 words combined.

Return ONLY the JSON array. No prose, no markdown, no commentary.
"""


def _build_user_prompt(
    beats: list[dict[str, Any]],
    *,
    era_anchor_prefix: str | None,
    character_description: str | None,
    style: str | None,
    mood: str | None,
) -> str:
    """Assemble the user-facing message for the refiner LLM call.

    The system prompt above contains the contract; this function inlines
    the per-render context (era, cast description, style, mood) and the
    per-beat content (with attractors already sanitised).
    """
    lines: list[str] = []
    if era_anchor_prefix and era_anchor_prefix.strip():
        lines.append(f"ERA CONTEXT (informational; renderer prepends it): {era_anchor_prefix.strip()}")
    if character_description and character_description.strip():
        lines.append(
            "CHARACTER (informational; renderer prepends it — preserve in spirit, "
            f"do not invent traits): {character_description.strip()}"
        )
    lines.append(f"STYLE: {(style or 'neutral').strip()}")
    lines.append(f"MOOD: {(mood or 'neutral').strip()}")
    lines.append("")
    lines.append("SHOT ROTATION (beat_index → shot_type):")
    for i, shot in enumerate(SHOT_ROTATION):
        lines.append(f"  {i} → {shot}")
    lines.append("Beat index modulo 9 picks the row.")
    lines.append("")
    lines.append("BEATS TO REFINE (refine each, preserving array order):")
    for i, beat in enumerate(beats):
        kv = sanitize_attractors((beat.get("key_visual") or "").strip())
        sc = sanitize_attractors((beat.get("scene") or "").strip())
        shot = shot_for_beat(i)
        lines.append(
            f"\nBEAT {i} [shot: {shot}]\n"
            f"  key_visual: {kv}\n"
            f"  scene:      {sc}"
        )
    lines.append("")
    lines.append(
        f"Return a JSON array of EXACTLY {len(beats)} objects, in beat "
        "order. Each object MUST have refined_visual, refined_scene, "
        "style_block — and no other fields."
    )
    return "\n".join(lines)


def _validate_one_item(
    item: object, *, beat_index: int,
) -> tuple[dict[str, str] | None, str | None]:
    """Validate one item emitted by the refiner LLM.

    Returns the cleaned dict on success, or ``None`` on validation
    failure so the caller can fall back to the legacy path for that beat
    only (without poisoning the whole batch).
    """
    if not isinstance(item, dict):
        logger.warning(
            "prompt_refiner: beat %d item not an object: %s",
            beat_index, type(item).__name__,
        )
        return None, "missing_field"
    rv = (item.get("refined_visual") or "").strip()
    rs = (item.get("refined_scene") or "").strip()
    sb = (item.get("style_block") or "").strip()
    if not rv or not rs or not sb:
        logger.warning(
            "prompt_refiner: beat %d missing required field "
            "(rv=%s rs=%s sb=%s)",
            beat_index, bool(rv), bool(rs), bool(sb),
        )
        return None, "missing_field"
    # Anti-text prefix MUST lead refined_scene — required for downstream
    # idempotence with images_cloudrun._append_anti_text_suffix.
    if not rs.lower().startswith("no readable text in image"):
        logger.warning(
            "prompt_refiner: beat %d refined_scene missing anti-text "
            "prefix; clearing",
            beat_index,
        )
        return None, "missing_field"
    return {"refined_visual": rv, "refined_scene": rs, "style_block": sb}, None


@_obs.traced(name="image.refiner.batch", category="llm")
def refine_prompts_batch(
    beats: list[dict[str, Any]],
    *,
    era_anchor_prefix: str | None,
    character_description: str | None,
    style: str | None,
    mood: str | None,
    channel_key: str | None = None,
    llm_call: Callable[..., Any] | None = None,
) -> list[dict[str, str]]:
    """Refine a batch of authored beats in ONE LLM call.

    Args:
        beats: Authored beats. Each must have ``key_visual`` and ``scene``
            string fields. Additional fields (``narration_line`` etc.) are
            ignored by the refiner.
        era_anchor_prefix: Era costume/period token block from
            :func:`pipeline.era_anchor.era_prefix_for`. Shown to the LLM
            as context so refined_scene aligns; the renderer still
            prepends the real era_anchor at compose time.
        character_description: Canonical cast description (narrator
            self-description). Same contract as era: informational to the
            LLM, code-owned at render time.
        style: Channel/render style (e.g. "animated comic illustration,
            clean lines"). Copied verbatim into style_block.
        mood: Render mood ("emotionally charged"). Copied verbatim.
        channel_key: Optional channel slug for telemetry (unused today;
            reserved).
        llm_call: Optional injection seam for tests. Defaults to
            :func:`pipeline.llm.cli.call_llm`. Must accept
            ``(prompt, *, output_json, model, stage)`` and return a
            parsed JSON value (list of dicts on success).

    Returns:
        A list parallel to ``beats``. Each element is either:

        - a dict ``{"refined_visual", "refined_scene", "style_block",
          "refined_version", "refined_input_hash"}`` on success, or
        - an empty dict ``{}`` on per-beat failure (caller falls back to
          legacy path for that beat only).

        On whole-batch failure (network error, malformed JSON, wrong
        array length) every slot is ``{}`` so the whole render falls
        through to the legacy path. This is the kill-switch behaviour.

    The function never raises — every failure mode is converted into an
    empty slot so the caller's render path is unaffected.
    """
    if not beats:
        return []
    n = len(beats)

    if llm_call is None:
        # Imported lazily so importing this module doesn't drag the LLM
        # dispatcher into test collection (and so the test seam is the
        # default, not a special case).
        from pipeline.llm import cli as _cli  # local import — see docstring
        llm_call = _cli.call_llm

    user_prompt = _build_user_prompt(
        beats,
        era_anchor_prefix=era_anchor_prefix,
        character_description=character_description,
        style=style,
        mood=mood,
    )
    full_prompt = _REFINER_SYSTEM + "\n\n---\n\n" + user_prompt
    input_batch = {
        "channel_key": channel_key,
        "era_anchor_prefix": era_anchor_prefix,
        "character_description": character_description,
        "style": style,
        "mood": mood,
        "beats": beats,
        "prompt": full_prompt,
    }

    try:
        raw = llm_call(
            full_prompt,
            output_json=True,
            model="haiku",
            stage="prompt_refine",
        )
    except Exception as exc:  # noqa: BLE001 — refiner must never raise
        logger.warning(
            "prompt_refiner: LLM call failed (%s); all %d beats fall "
            "back to legacy path",
            exc, n,
        )
        for i in range(n):
            _track_refiner_fallback(i, "truncated")
        _emit_refiner_io_artifact(
            input_batch=input_batch,
            raw_response_str=f"<{type(exc).__name__}: {exc}>",
            parsed=None,
            fallback_count=n,
        )
        return [{} for _ in range(n)]

    raw_response_str = _raw_response_string(raw)
    if not isinstance(raw, list) or len(raw) != n:
        reason = "dict_returned" if isinstance(raw, dict) else "truncated"
        logger.warning(
            "prompt_refiner: LLM returned %s (expected list of %d); "
            "whole batch falls back",
            type(raw).__name__ if not isinstance(raw, list) else f"list[{len(raw)}]",
            n,
        )
        for i in range(n):
            _track_refiner_fallback(i, reason)
        _emit_refiner_io_artifact(
            input_batch=input_batch,
            raw_response_str=raw_response_str,
            parsed=raw,
            fallback_count=n,
        )
        return [{} for _ in range(n)]

    out: list[dict[str, str]] = []
    # Local import — strip_text_bait lives next door and pulling it at
    # module import time would create a circular import path through
    # pipeline.images.images.
    from pipeline.images import images as _images  # noqa: PLC0415

    for i, (beat, item) in enumerate(zip(beats, raw)):
        refined, fallback_reason = _validate_one_item(item, beat_index=i)
        if not refined:
            _track_refiner_fallback(i, fallback_reason or "missing_field")
            out.append({})
            continue
        rv_clean, rv_removed = _images.strip_text_bait(refined["refined_visual"])
        rs_clean, rs_removed = _images.strip_text_bait(refined["refined_scene"])
        if rv_removed or rs_removed:
            logger.warning(
                "prompt_refiner: beat %d refined fields contained text-bait "
                "after refining (%s); clearing and falling back",
                i, list(rv_removed) + list(rs_removed),
            )
            _track_refiner_fallback(i, "length_violation")
            out.append({})
            continue
        out.append({
            "refined_visual": rv_clean,
            "refined_scene": rs_clean,
            "style_block": refined["style_block"],
            "refined_version": REFINER_VERSION,
            "refined_input_hash": compute_input_hash(
                beat=beat,
                era_anchor_prefix=era_anchor_prefix,
                character_description=character_description,
                style=style,
                mood=mood,
            ),
        })
    fallback_count = sum(1 for slot in out if not slot)
    _emit_refiner_io_artifact(
        input_batch=input_batch,
        raw_response_str=raw_response_str,
        parsed=raw,
        fallback_count=fallback_count,
    )
    return out


__all__ = [
    "REFINER_VERSION",
    "SHOT_ROTATION",
    "shot_for_beat",
    "sanitize_attractors",
    "compute_input_hash",
    "refine_prompts_batch",
    "refined_fields_for_render",
]


def refined_fields_for_render(
    beat: dict[str, Any],
    *,
    era_anchor_prefix: str | None,
    character_description: str | None,
    style: str | None,
    mood: str | None,
    env_flag_name: str = "YTFACTORY_PROMPT_REFINER",
) -> tuple[str | None, str | None, str | None]:
    """Render-time gate for the cached refined fields on a single beat.

    Returns the trio ``(refined_visual, refined_scene, style_block)`` —
    suitable to splat into
    :func:`pipeline.images.images.build_full_prompt` — when **all** of:

    1. The env flag (default ``YTFACTORY_PROMPT_REFINER``) is enabled.
    2. The beat carries a ``refined_version`` matching the current
       :data:`REFINER_VERSION` (so a bump auto-invalidates caches).
    3. The beat's ``refined_input_hash`` matches a freshly-computed
       hash over the same inputs the refiner ran with (key_visual,
       scene, era_anchor_prefix, character_description, style, mood).
       Any drift (critic-patched scene, cast change, mood retune)
       triggers a mismatch → fall back to legacy.
    4. All three of ``refined_visual``, ``refined_scene``, and
       ``style_block`` are non-empty.

    Returns ``(None, None, None)`` on any failure — the caller passes
    those to ``build_full_prompt`` and the legacy assembly path runs.

    Why this is render-time and not authoring-time only: the rubber-duck
    review (2026-05-14) pointed out that without a render-time gate, a
    cached ``refined_*`` would still be honoured even after
    ``YTFACTORY_PROMPT_REFINER`` is cleared — defeating the kill switch.
    This helper enforces the gate at the consumption site so unsetting
    the env variable disables the refiner immediately, no cache wipe
    required.
    """
    import os
    if os.environ.get(env_flag_name, "").strip().lower() not in {"1", "true", "yes", "on"}:
        return (None, None, None)
    if beat.get("refined_version") != REFINER_VERSION:
        return (None, None, None)
    expected_hash = compute_input_hash(
        beat=beat,
        era_anchor_prefix=era_anchor_prefix,
        character_description=character_description,
        style=style,
        mood=mood,
    )
    if beat.get("refined_input_hash") != expected_hash:
        return (None, None, None)
    rv = (beat.get("refined_visual") or "").strip()
    rs = (beat.get("refined_scene") or "").strip()
    sb = (beat.get("style_block") or "").strip()
    if not (rv and rs and sb):
        return (None, None, None)
    return (rv, rs, sb)
