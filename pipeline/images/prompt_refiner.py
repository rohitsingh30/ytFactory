"""LLM prompt-refiner pre-step for FLUX.2 [klein].

Why this exists
---------------

FLUX.2 [klein] 4B uses a Qwen3 8B text encoder and is guidance-distilled
(``guidance_scale`` is permanently clamped to 1.0). That means **there is
no classifier-free-guidance** and therefore **no negative-prompt mechanism**
at the model level — every "no X" instruction in our prompts is linguistic-
only and loses to training-data attractors (speech bubbles fill with
gibberish, the word "trap" pulls in YouTube tutorial UI, etc.).

The DALL-E 3 playbook closes that gap by inserting an LLM rewriting step
*before* the diffusion call. That's what this module does: takes the
authored ``{key_visual, scene}`` per beat plus channel context and emits a
structured set of FLUX-optimised fields.

The refiner does **not** own the final prompt string. Render-time
invariants — era anchor, character description / cast lock, kit lock —
remain code-owned in ``pipeline/images/images.py::build_full_prompt``.
The refiner only owns the visual description, the shot type, the lighting,
and the BFL-format style/mood block.

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

Reference
---------

``data/research/flux2_prompting_2026-05-14.md`` — the research report this
module implements.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)


REFINER_VERSION = "v1"

# Camera rotation per beat — BFL composition vocabulary, verified to elicit
# distinct latents on FLUX.2 [klein]. Cycling through forces visual
# diversity across the rolling 3-beat window (the author-side _SYSTEM
# Rule 10 already instructs the LLM to vary composition, but it doesn't
# inject explicit shot tokens — this rotation does).
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


# The refiner system prompt. Kept deliberately short (~50 lines vs the
# 180-line author _SYSTEM block) so the LLM doesn't forget rules and the
# output schema stays predictable. One job: take the authored fields and
# produce structured FLUX-optimised output.
_REFINER_SYSTEM = """\
You are a FLUX.2 [klein] prompt specialist. You receive an authored beat
({key_visual, scene}) and channel context. Rewrite the visual fields to
maximise instruction-following on FLUX.2 [klein] 4B.

MODEL CONTEXT — IMPORTANT:
FLUX.2 [klein] uses Qwen3 8B text encoder + guidance_scale=1.0 clamped.
There is NO classifier-free guidance, so "no X" instructions are
linguistic-only and lose to training-data attractors. Convert every
negation to a POSITIVE construction. Examples:
  - "no hat" → "bareheaded"
  - "not smiling" → "neutral expression"
  - "no people" → "empty scene"
  - "no signs visible" → "plain walls"
The ONE exception is the canonical "no readable text in image" anti-text
prefix — keep it verbatim because the pipeline depends on the exact wording.

OUTPUT — a JSON array of objects, one per input beat, in beat order. Each
object MUST have exactly these three fields and no others:

  {
    "refined_visual": "<polished concrete noun phrase, 4-10 words>",
    "refined_scene":  "no readable text in image. <SHOT_TYPE>, <LIGHTING>, <posture/action>, <setting>",
    "style_block":    "Style: <copy provided style>. Mood: <copy provided mood>."
  }

PER-BEAT RULES (a beat failing any rule has its slot replaced with {} by
the caller, and that beat falls back to the legacy non-refined path):

1. refined_scene MUST start with the literal string "no readable text in
   image. " (lower-case, period, space). Do not paraphrase this prefix —
   downstream idempotence depends on the exact wording.

2. Inject EXACTLY ONE shot_type from the rotation the user message gives
   you. Do not invent shot types. Do not skip.

3. Inject EXACTLY ONE lighting description (warm golden-hour backlight,
   harsh overhead noon sun, soft diffused window light, cold blue
   moonlight, neon storefront glow, fluorescent office overhead, etc.).
   Vary across beats — the same lighting two beats in a row defeats the
   purpose.

4. NEVER write any of: speech bubble, thought bubble, comic panel,
   chalkboard, whiteboard, computer screen showing text, phone screen
   showing text, sign, billboard, poster, label, license plate, name tag,
   certificate, invitation, contract, receipt, scoreboard, clock face
   with numbers, YouTube logo, Subscribe button, Like button, thumbs-up
   button, bell icon, play button, video UI. Convey emotion via POSTURE
   and FACIAL EXPRESSION only.

5. NEVER render metaphors literally. "rocket of a shot" → "hard-struck
   ball". "thunderbolt" → "powerful strike". The same applies to all
   sports / action figurative language.

6. ONE main subject per beat. The user message gives you a canonical
   character_description — preserve it in spirit (do not invent new
   physical traits) but do not re-describe the character explicitly in
   refined_scene; the renderer prepends the canonical description.

7. Keep refined_scene under 60 words total. Front-load the shot_type and
   lighting after the anti-text prefix.

8. style_block: copy the provided style + mood EXACTLY into the template
   "Style: <s>. Mood: <m>." Do not paraphrase. If either is missing, use
   "neutral" as the placeholder.

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


def _validate_one_item(item: object, *, beat_index: int) -> dict[str, str] | None:
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
        return None
    rv = (item.get("refined_visual") or "").strip()
    rs = (item.get("refined_scene") or "").strip()
    sb = (item.get("style_block") or "").strip()
    if not rv or not rs or not sb:
        logger.warning(
            "prompt_refiner: beat %d missing required field "
            "(rv=%s rs=%s sb=%s)",
            beat_index, bool(rv), bool(rs), bool(sb),
        )
        return None
    # Anti-text prefix MUST lead refined_scene — required for downstream
    # idempotence with images_cloudrun._append_anti_text_suffix.
    if not rs.lower().startswith("no readable text in image"):
        logger.warning(
            "prompt_refiner: beat %d refined_scene missing anti-text "
            "prefix; clearing",
            beat_index,
        )
        return None
    return {"refined_visual": rv, "refined_scene": rs, "style_block": sb}


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
        return [{} for _ in range(n)]

    if not isinstance(raw, list) or len(raw) != n:
        logger.warning(
            "prompt_refiner: LLM returned %s (expected list of %d); "
            "whole batch falls back",
            type(raw).__name__ if not isinstance(raw, list) else f"list[{len(raw)}]",
            n,
        )
        return [{} for _ in range(n)]

    out: list[dict[str, str]] = []
    # Local import — strip_text_bait lives next door and pulling it at
    # module import time would create a circular import path through
    # pipeline.images.images.
    from pipeline.images import images as _images  # noqa: PLC0415

    for i, (beat, item) in enumerate(zip(beats, raw)):
        refined = _validate_one_item(item, beat_index=i)
        if not refined:
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
