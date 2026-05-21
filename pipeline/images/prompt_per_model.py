"""Best-chance per-model prompt builder for Z-Image-Turbo (the production lane).

Originally a 4-way bake-off harness across flux2_klein / flux2_dev /
qwen_image / z_image_turbo (see ``docs/pipeline_bug_catalogue_v2_2026-05-14.html``).
After the 2026-05-16 cost-optimization sweep the bake-off lost — Cloud Run
GPU idle costs forced us to drop everything except z_image_turbo. The
other branches and their citations are preserved in git history; this
module now only knows about z_image_turbo.

What this module owns
---------------------

The :func:`best_prompt_for` factory returns the model-tuned prompt + endpoint
parameters (steps, guidance, negative-prompt, aspect ratio) for one
beat. It encodes Z-Image-Turbo's documented prompting quirks:

* **z_image_turbo** — Tongyi-MAI/Z-Image-Turbo. Distilled (CFG
  hard-clamped to 0.0 server-side), front-loaded prompt structure.
  Concise Subject + Action + Setting + Style + Lighting. Steps 4–8.
  Negative-prompt is a no-op (anti-text via POSITIVE prefix). Sources:

  - HF model card — https://huggingface.co/Tongyi-MAI/Z-Image-Turbo
    ("8 NFEs at default; concise English prompts work best").
  - ytFactory scaffold — ``cloud/image-z-image-turbo/server.py`` lines
    122–148 (CFG locked 0.0 server-side).
  - Apache 2.0 license.

Cross-cutting references:

* ytFactory anti-text design — ``audit_data/research/flux2_klein.html`` (the
  POSITIVE-PREFIX rule that produced this module's anti-text wording).
* Character-lock evidence — ``audit_data/research/cast_continuity.html``
  (era anchor + canonical character description as code-owned prefix).
* Shorts playbook — ``audit_data/research/shorts_playbook.html``
  (subject-centered, vertical 9:16 composition rules).

Hard rules
----------

1. The same authored beat (subject, action, scene, style, era, character,
   mood, shot_type) goes IN, and four different prompts come OUT — each one
   shaped for the receiving model.
2. The output dict always carries ``prompt``, ``negative_prompt``, ``steps``,
   ``guidance_scale``, ``aspect`` so the bake-off harness can wire it
   straight onto the model's POST /generate body. Guidance-distilled models
   return ``negative_prompt = ""`` (the model ignores it; we ship empty for
   wire stability) and ``guidance_scale = 1.0`` (klein) / ``0.0`` (z_image).
3. ``character`` and ``era`` are PREPENDED in front for all four models —
   they're code-owned identity anchors per the 2026-05-14 audit fixes
   (see ``pipeline/images/images.py::build_full_prompt``). Removing them
   would risk repeating the "5 different Ronaldinhos" + "WW1-in-1258" bugs.

This module does NOT call the LLM refiner from
``pipeline/images/prompt_refiner.py`` — the bake-off compares MODELS, not
prompt-refinement strategies, so we hold the prompt fully deterministic.
"""

from __future__ import annotations

from typing import Any


# Canonical anti-text framing — positive surface descriptions, NOT negations.
# Same wording as ``pipeline/images/images_cloudrun.ANTI_TEXT_PREFIX`` so the
# bake-off measures the same anti-text pressure prod uses.
ANTI_TEXT_PREFIX_POSITIVE = (
    "Clean unmarked surface, blank fabric, smooth plain backgrounds, "
    "unlabeled book covers, unmarked banners, no signage, no watermark, "
    "no logo, no caption, no street signs"
)

# Negation-form anti-text — kept for parity with documentation. Currently
# unused since Z-Image-Turbo (the only remaining provider) ignores
# negative_prompt (CFG hard-clamped to 0.0 server-side).
ANTI_TEXT_NEGATION = (
    "text, letters, words, gibberish writing, scribbles, watermark, logo, "
    "caption, subtitle, sign, banner, signage, license plate, name tag, "
    "scoreboard digits, ui buttons, youtube interface, subscribe button, "
    "deformed hands, extra fingers, lowres, blurry, jpeg artifacts"
)


VALID_MODELS: tuple[str, ...] = (
    "z_image_turbo",
)


def _strip(s: str | None) -> str:
    return (s or "").strip().rstrip(".")


def _join(*parts: str) -> str:
    return ". ".join(p for p in (x.strip().rstrip(".") for x in parts) if p) + "."


def _bfl_slot_order(
    *,
    subject: str,
    action: str,
    style: str,
    scene: str,
    era: str,
    character: str,
    mood: str,
    shot_type: str,
    anti_text: str,
) -> str:
    """Black Forest Labs canonical prompt order — Subject → Action → Style →
    Context → Lighting → Technical. Used by both flux2 variants.

    Per BFL docs (https://docs.bfl.ml/guides/prompting_guide_flux2): "Word
    order matters — FLUX.2 pays more attention to what comes first"; "style
    and quality descriptors should be placed at the end".

    Layout:
      [ANTI_TEXT positive prefix]. [ERA — era tokens].
      [CHARACTER — canonical description].
      [SUBJECT — concrete noun phrase].
      [ACTION — what they're doing].
      [SCENE — setting/context]. [SHOT_TYPE — framing].
      [STYLE — illustration style + mood].
    """
    chunks: list[str] = []
    if anti_text:
        chunks.append(anti_text)
    if era:
        chunks.append(f"[ERA — {_strip(era)}]")
    if character:
        chunks.append(_strip(character))
    chunks.append(_strip(subject))
    if action:
        chunks.append(_strip(action))
    if scene:
        chunks.append(_strip(scene))
    if shot_type:
        chunks.append(_strip(shot_type))
    tail = _strip(style)
    if mood:
        tail = (tail + f", mood: {_strip(mood)}") if tail else f"mood: {_strip(mood)}"
    if tail:
        chunks.append(tail)
    return _join(*chunks)


def _qwen_paragraph(
    *,
    subject: str,
    action: str,
    style: str,
    scene: str,
    era: str,
    character: str,
    mood: str,
    shot_type: str,
) -> str:
    """Qwen-Image is instruction-tuned — it prefers descriptive natural-
    language paragraphs over comma-separated tag lists. The HF model card
    explicitly recommends "detailed paragraphs of constraints" over the
    Midjourney / SD comma-stream pattern.

    We assemble a single coherent paragraph that names the subject, what
    they're doing, where, with character traits inline, framing/shot
    explicit, and style/mood at the end. Era is named inline as a period
    constraint instead of bracketed.
    """
    sub = _strip(subject)
    char = _strip(character)
    act = _strip(action)
    scn = _strip(scene)
    er = _strip(era)
    st = _strip(style)
    md = _strip(mood)
    sh = _strip(shot_type)
    parts: list[str] = []
    # Lead with shot type so framing dominates composition.
    if sh:
        parts.append(f"A {sh} of")
    # Subject + character description inline (Qwen handles parenthetical-
    # style descriptions inline cleanly).
    if char:
        parts.append(f"{sub} ({char})")
    else:
        parts.append(sub)
    if act:
        parts.append(act)
    if scn:
        parts.append(f"in {scn}")
    if er:
        parts.append(f"set during {er}")
    if st:
        parts.append(f"rendered as {st}")
    if md:
        parts.append(f"with a {md} atmosphere")
    body = ", ".join(p for p in parts if p)
    if not body.endswith("."):
        body = body + "."
    return body


def _z_image_compact(
    *,
    subject: str,
    action: str,
    style: str,
    scene: str,
    era: str,
    character: str,
    mood: str,
    shot_type: str,
    anti_text: str,
) -> str:
    """Z-Image-Turbo prompts work best concise + front-loaded. Same Tencent
    family as Qwen but distilled (CFG=0); model card recommends "concise
    English prompts work best" — we mirror klein's slot order but tighten
    the wording.
    """
    return _bfl_slot_order(
        subject=subject, action=action, style=style, scene=scene,
        era=era, character=character, mood=mood, shot_type=shot_type,
        anti_text=anti_text,
    )


def best_prompt_for(
    model: str,
    *,
    subject: str,
    scene: str,
    action: str,
    style: str,
    era: str,
    character: str,
    mood: str = "",
    shot_type: str = "medium shot",
) -> dict[str, Any]:
    """Return {prompt, negative_prompt, steps, guidance_scale, aspect}
    tuned for ``model``.

    Args:
        model: Must be ``"z_image_turbo"`` — the only remaining
            provider after the 2026-05-16 cost-optimization sweep.
            Other values raise ``ValueError``. flux2_klein, flux2_dev
            and qwen_image branches are preserved in git history.
        subject: The "who/what" — a concrete noun phrase ("ancient Indian
            warrior prince Arjun"). Goes into the BFL Subject slot.
        scene: Where/context ("standing on his royal chariot, smoke and
            distant warriors"). Composition / setting tokens.
        action: What the subject is doing ("drawing back his recurve bow").
            BFL Action slot.
        style: Illustration style ("Amar Chitra Katha comic-book
            illustration, flat saturated colors"). BFL Style slot — last.
        era: Period anchor — code-owned identity, prepended to every model's
            prompt. Per ``pipeline/era_anchor.py``.
        character: Canonical cast description — code-owned identity,
            prepended to every model's prompt. Per
            ``pipeline/images/images.py::build_full_prompt``.
        mood: Optional emotional / tone token ("heroic, climactic"). Appended
            to the Style tail.
        shot_type: Framing token ("medium shot", "wide cinematic shot"). BFL
            Technical slot.

    Returns:
        A dict with five fields, ready to splat onto a POST /generate body
        for ``cloud/image-z-image-turbo/server.py``::

            {
              "prompt": str,
              "negative_prompt": "",   # distilled — server ignores
              "steps": 8,
              "guidance_scale": 1.0,   # server clamps to 0.0 internally
              "aspect": "9:16"
            }

    Raises:
        ValueError: When ``model`` is not in :data:`VALID_MODELS`.
    """
    if model not in VALID_MODELS:
        raise ValueError(
            f"unknown model {model!r}; valid: {sorted(VALID_MODELS)}"
        )

    # z_image_turbo
    prompt = _z_image_compact(
        subject=subject, action=action, style=style, scene=scene,
        era=era, character=character, mood=mood, shot_type=shot_type,
        anti_text=ANTI_TEXT_PREFIX_POSITIVE,
    )
    return {
        "prompt": prompt,
        "negative_prompt": "",   # distilled — server ignores
        "steps": 8,
        # Z-Image-Turbo is CFG-distilled. The brief documents CFG=1.0 as the
        # cross-model "stable API" value; the Cloud Run server's
        # field_validator silently re-clamps to 0.0 internally (see
        # cloud/image-z-image-turbo/server.py field_validator). We send
        # 1.0 on the wire for parity with the brief's stated contract.
        "guidance_scale": 1.0,
        "aspect": "9:16",
    }


__all__ = [
    "best_prompt_for",
    "ANTI_TEXT_PREFIX_POSITIVE",
    "ANTI_TEXT_NEGATION",
    "VALID_MODELS",
]
