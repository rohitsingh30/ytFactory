"""Stage 6 — image generation. Two backends share one ``generate()``:

* ``sdxl_lightning`` (default) — distilled SDXL, 4-step inference, MPS.
  ~7 GB total, ~5–15 s/image. Supports IP-Adapter image conditioning
  for character lock.
* ``mflux`` — Flux Schnell 4-bit via Apple's MLX. ~24 GB to download
  on first run, ~7 GB on disk after quantization, ~10–20 s/image. Much
  stronger prompt adherence (renders specific objects, respects "no
  character in frame", handles multi-subject). Conditioning via Flux
  Redux is not wired yet — the channel ``character_description``
  prepend is the only character lock on this path.

One image per beat, locked seed + style prefix for consistency.

Principles enforced (DESIGN.md §14):

* **#3 Forbidden text-bait phrases** — `lint_prompt()` warns on words
  that ask for legible text in the image.
* **#11 Token-budget warning** — `lint_prompt()` warns when the scene
  portion exceeds ~50 tokens (attention dilutes; later tokens drop on
  CLIP-style encoders. Flux uses T5 with a much longer context, so this
  matters less on the mflux path but the warning is still useful).
* **#12 Multi-subject warning** — `lint_prompt()` flags plural-subject
  patterns ("three friends", "several characters").
* **#13 Key-visual weighting** — when prompts are passed in object form
  with a `key_visual` field, the SDXL builder emits
  `(key_visual:1.4), scene` so attention concentrates on the punchline.
  Flux does not parse compel-style weighting, so on the mflux path the
  builder emits the key_visual first as plain text instead.
* **#14 IP-Adapter character lock** — `generate(ip_adapter_image=...)`
  conditions the SDXL diffusion run on a reference image. The
  orchestrator passes the channel's reference (or auto-bootstraps from
  img_00). Not used on the mflux path.
"""

from __future__ import annotations

import json
import os as _os
import re
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]

if TYPE_CHECKING:
    pass


def _torch():
    """Lazy import — torch is cloud-only since 2026-05-09."""
    import torch  # noqa: PLC0415
    return torch


def _diffusers():
    """Lazy import — diffusers is cloud-only since 2026-05-09."""
    from diffusers import AutoPipelineForText2Image, EulerDiscreteScheduler  # noqa: PLC0415
    return AutoPipelineForText2Image, EulerDiscreteScheduler


def _hf_hub_download(*args, **kwargs):
    from huggingface_hub import hf_hub_download as _real  # noqa: PLC0415
    return _real(*args, **kwargs)


def _load_file(*args, **kwargs):
    from safetensors.torch import load_file as _real  # noqa: PLC0415
    return _real(*args, **kwargs)


_BASE_REPO = "stabilityai/stable-diffusion-xl-base-1.0"
_LIGHTNING_REPO = "ByteDance/SDXL-Lightning"
_LIGHTNING_CKPT = "sdxl_lightning_4step_lora.safetensors"  # 4-step LoRA

_IP_ADAPTER_REPO = "h94/IP-Adapter"
_IP_ADAPTER_SUBFOLDER = "sdxl_models"
_IP_ADAPTER_WEIGHT = "ip-adapter_sdxl.bin"  # ~700 MB

_PIPE = None
_IP_ADAPTER_LOADED = False
_FLUX_PIPE = None  # mflux Flux1 instance, lazy-loaded on first mflux call
_ZIMAGE_PIPE = None  # mflux ZImage (Turbo) instance, lazy-loaded on first z_image_turbo call


# --- Linter ------------------------------------------------------------

# #3: words/phrases that ask diffusion to render legible text in-frame.
_TEXT_BAIT = (
    "label", "labels", "labeled", "labelled",
    "sign", "signs", "signage",
    "text", "texts", "writing", "written", "letters", "lettering",
    "logo", "logos", "brand", "branded",
    "dollar sign", "dollar signs", "dollar symbol",
    "words", "title", "title card", "subtitle", "caption",
    "menu", "newspaper", "headline", "billboard", "poster",
    "tattoo with text", "engraved text",
    # Critic 2026-05-03: ALL speech-bubble variants render garbled text
    # or weirdly-embedded objects (e.g. a Nike-branded American football
    # rendered INSIDE a speech bubble). Drop the qualified forms
    # ("…with words") and ban the bare nouns — the LLM can describe
    # emotion via posture/face, no bubble needed.
    "speech bubble", "speech bubbles", "speech-bubble",
    "thought bubble", "thought bubbles", "thought-bubble",
    "text balloon", "word balloon", "dialogue bubble",
    "phone screen with text", "phone screen showing messages",
    "text message", "text messages", "messages with",
    "notification", "notifications",
    "store name", "shop name", "storefront sign",
    "license plate", "name tag", "wristband",
    # Critic 2026-05-03: clock with numerals renders garbled / wrong
    # numbers ("0 1 2 3 4 5 6 7 8 9" misordered or replaced with
    # "11.4" gibberish). Use "stadium clock" carefully — describe the
    # POSITION of the hands ("hands trembling near the twelve") rather
    # than asking for "number 90" or "ninety-third".
    "stadium clock", "clock face with numbers", "clock showing 90",
    "wristwatch face", "watch face glinting", "watch reading",
    # Prop categories that almost always render with garbled inscribed
    # text (Principle #14, "wedding TO invitating" class-of-bug).
    # Describe these props by shape/color/context only.
    "invitation", "invitations", "wedding invitation",
    "certificate", "certificates", "diploma", "diplomas",
    "contract", "contracts", "prescription", "prescriptions",
    "receipt", "receipts", "boarding pass", "court order",
    "greeting card", "thank you card", "business card",
    "ticket stub", "movie ticket", "plane ticket",
    # Numeric-display props — diffusion can't render legible digits on
    # clock faces / scoreboards / timers. The 88th-minute clock on the
    # v15 Aguero render came out as "I.4" gibberish. Describe these as
    # shapes (an analog clock face, a stadium scoreboard pole) without
    # asking for the number, OR show the SHAPE of late-game tension via
    # raised wristwatch / hands-on-knees / floodlit angle.
    "digital clock", "digital clocks", "clock face", "clock face showing",
    "stadium clock", "stadium clock showing", "scoreboard clock",
    "scoreboard", "scoreboards", "scoreboard showing",
    "led display", "led screen", "scoreboard display", "score display",
    "timer", "stopwatch", "match clock", "minute marker",
)
_TEXT_BAIT_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _TEXT_BAIT) + r")\b",
    flags=re.IGNORECASE,
)

# Inscribed-text patterns: "<prop> reading X", "<prop> that says X",
# "<prop> displaying X" — diffusion can't render the inscribed string,
# only the prop. The fix is always to drop the inscribed clause.
_PROP_TEXT_PATTERN_RE = re.compile(
    r"\b\w+\s+"
    r"(?:that\s+(?:reads?|says?|states?|shows?)|"
    r"reading|saying|stating|displaying|inscribed\s+with|"
    r"with\s+the\s+words?|with\s+text\s+(?:saying|reading)|"
    r"that\s+spells)\b",
    flags=re.IGNORECASE,
)

# #12: plural-subject patterns that diffusion struggles to render correctly.
# Allows up to 3 adjectives between the count and the subject noun
# ("three chibi-style friends", "five very angry kids").
_PLURAL_SUBJ_RE = re.compile(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|"
    r"several|multiple|many|a\s+group\s+of|a\s+crowd\s+of|"
    r"\d+)\s+"
    r"(?:\w+(?:-\w+)?\s+){0,3}"  # optional adjectives
    r"(?:friends?|people|persons?|figures?|characters?|kids?|children|"
    r"adults?|customers?|workers?|family\s+members?)\b",
    flags=re.IGNORECASE,
)


# Quoted text fragments inside a scene are the actual bug behind the
# 2026-05-02 hook gibberish: beat 0's scene contained `Caption stays
# 'asshole'` and SDXL/Z-Image-Turbo rendered "asshlash" as garbled
# letters across the top of the frame. Even a 4-character quoted token
# is enough to trigger this — the model treats *anything* in quotes as
# a literal text-render request.
_QUOTED_TEXT_RE = re.compile(r"['\"]([^'\"]{1,80})['\"]")

# Critic / author meta-instructions that have leaked into scene fields.
# When a scene starts (or contains a clause starting) with one of these
# verb-imperatives, the diffusion model interprets it as scene content
# and renders something nonsensical. Same root cause as `_META_INSTRUCTION_
# PATTERNS` in critic.py — kept here so prompts.py can sanitise on the
# author side too, before the scene ever reaches the cache.
_META_IMPERATIVE_RE = re.compile(
    r"\b("
    # Verb-imperatives. Critic patches leak edit instructions like
    # "Replace X with Y", "Reposition X", "Rewrite narration to: Y",
    # "Image must depict X", "Scene: X" — all directives intended for
    # the next prompt-author pass that instead end up concatenated
    # INTO the scene field and rendered by SDXL as gibberish text.
    r"Replace\s+[^.]{1,120}?(?=\.|$)|"
    r"Reposition\s+[^.]{1,120}?(?=\.|$)|"
    r"Rewrite\s+\w+\s+to\s*:[^.]*?(?=\.|$)|"
    r"Caption\s+(?:stays|reads|shows|says)\b[^.]*?(?=\.|$)|"
    r"Keep\s+[^.]*?\bas[- ]is\b[^.]*?(?=\.|$)|"
    r"No\s+\w+(?:[- ]\w+){1,3}\s+pose\b[^.]*?(?=\.|$)|"
    # Image-generation-pipeline meta references. Any clause that
    # mentions "drawn by compose", "rendered by compose", "model-
    # rendered", "PIL", "SVG", or "the image model" is a critic
    # instruction about the rendering layer, not a scene description.
    # Strip the whole clause — diffusion can't action these and tries
    # to render the words as text (the "M DISGUSTE!" failure mode).
    r"[^.]*\b(?:drawn|rendered|generated)\s+by\s+(?:compose|the\s+image\s+model|PIL|SVG)\b[^.]*?(?=\.|$)|"
    r"[^.]*\bmodel[- ]rendered\b[^.]*?(?=\.|$)|"
    r"[^.]*\b(?:NO|no)\s+model[- ]rendered\s+\w+[^.]*?(?=\.|$)|"
    # "X overlay …must be drawn …" / "X overlay …rendered by …"
    r"\w+\s+overlay\b[^.]*?(?=\.|$)|"
    # Critic-syntax: "Hook X must / Image must / Scene must …"
    r"(?:Hook|Image|Scene|Frame|Title|Banner)\s+(?:overlay\s+)?[^.]{0,80}?\bmust\b[^.]*?(?=\.|$)|"
    # Closer-panel directives (kept from previous pass).
    r"(?:Closer\s+panel|Panel|Closer)\s+(?:must\s+render|render(?:s)?|shows?|displays?)"
    r"[^.]*?(?=\.|$)|"
    # OP / line-is / tag-near terminology that critics use.
    r"\bOP\b[^.]{0,80}?\b(?:reading|alone|holding)\b[^.]*?(?=\.|$)|"
    r"The\s+line\s+is\b[^.]*?(?=\.|$)|"
    r"Reposition\s+the\s+word\b[^.]*?(?=\.|$)"
    r")",
    flags=re.IGNORECASE | re.DOTALL,
)


def strip_text_bait(scene: str) -> tuple[str, list[str]]:
    """Remove text-bait phrases, quoted text fragments, and leaked
    meta-instructions from a scene-prompt. Returns ``(cleaned, removed)``.

    Class-of-bug fix (2026-05-02): the per-beat lint emits warnings but
    doesn't actually clean the scene, so SDXL/Z-Image-Turbo still saw
    "Caption stays 'asshole'" / "Replace hook visual with..." etc.
    inside the scene field and rendered them. This function is the
    enforcing companion to ``lint_prompt``: callers (prompts.py author,
    critic.py patcher) must invoke it before the scene is persisted.
    """
    if not scene:
        return scene, []
    removed: list[str] = []

    cleaned = scene
    # 1. Pull out any meta-instruction clauses leaked into the scene.
    for m in _META_IMPERATIVE_RE.finditer(cleaned):
        removed.append(m.group(0).strip())
    cleaned = _META_IMPERATIVE_RE.sub(" ", cleaned)

    # 2. Drop quoted strings entirely — diffusion can't render arbitrary
    #    text and CJK-glyph fallback ("asshlash", "夫倭吧?") is the
    #    typical failure mode. The narration captions handle real text;
    #    the image must not.
    for m in _QUOTED_TEXT_RE.finditer(cleaned):
        removed.append(f"quoted: {m.group(0)}")
    cleaned = _QUOTED_TEXT_RE.sub(" ", cleaned)

    # 3. Strip the most damaging text-bait single tokens (caption, sign,
    #    label, text, words). The full _TEXT_BAIT list is broader and
    #    contains props like "invitation"/"receipt" that we treat as
    #    warn-only — those at least produce *something* visual, just
    #    with mangled inscribed text. The hard list below is for
    #    tokens that always produce pure gibberish.
    hard_tokens = (
        r"caption", r"captions",
        r"label", r"labels", r"labeled", r"labelled",
        r"sign", r"signs", r"signage",
        r"writing", r"written", r"letters", r"lettering",
        r"text", r"texts", r"in[- ]image\s+text",
        r"title\s+card", r"subtitle",
    )
    hard_re = re.compile(
        r"\b(" + "|".join(hard_tokens) + r")\b",
        flags=re.IGNORECASE,
    )
    hard_hits = [m.group(0) for m in hard_re.finditer(cleaned)]
    if hard_hits:
        removed.extend(f"hard-bait: {h}" for h in hard_hits)
        cleaned = hard_re.sub(" ", cleaned)

    # Collapse whitespace + dangling punctuation left by deletions.
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:")
    return cleaned, removed


def lint_prompt(scene: str, *, scene_token_budget: int = 50) -> list[str]:
    """Return warnings about a scene-prompt string. Empty list = clean."""
    warnings: list[str] = []

    text_bait_hits = [m.group(0) for m in _TEXT_BAIT_RE.finditer(scene)]
    if text_bait_hits:
        warnings.append(
            f"text-bait phrases produce AI gibberish: "
            f"{', '.join(sorted(set(text_bait_hits)))}"
        )

    inscribed_hits = [m.group(0) for m in _PROP_TEXT_PATTERN_RE.finditer(scene)]
    if inscribed_hits:
        warnings.append(
            f"inscribed-text-on-prop phrasing renders as garbled letters: "
            f"{', '.join(sorted(set(inscribed_hits)))}. "
            f"Drop the inscribed clause; describe the prop by shape/color only."
        )

    multi_subj_hits = [m.group(0) for m in _PLURAL_SUBJ_RE.finditer(scene)]
    if multi_subj_hits:
        warnings.append(
            f"multi-subject phrasing is unreliable below Flux quality: "
            f"{', '.join(sorted(set(multi_subj_hits)))}. "
            f"Split across beats — one subject per beat."
        )

    # #11: token budget. Word count × 1.3 ≈ CLIP tokens (rough).
    n_words = len(scene.split())
    est_tokens = int(n_words * 1.3)
    if est_tokens > scene_token_budget:
        warnings.append(
            f"scene is ~{est_tokens} tokens (>{scene_token_budget}). "
            f"Attention dilutes past this — trim or move secondary "
            f"detail into the next beat."
        )

    return warnings


# --- Prompt builder ----------------------------------------------------


def build_full_prompt(
    *,
    style_prefix: str,
    character_description: str | None,
    key_visual: str | None,
    scene: str,
    key_visual_weight: float = 1.4,
    weighted: bool = True,
) -> str:
    """Compose the final prompt string fed to the diffusion model.

    Order matters — earlier tokens get more attention. Layout:

        {character_description}, ({key_visual}:1.4), {scene}. {style_prefix}

    `character_description` first establishes identity (Principle #2);
    `key_visual` is weighted up so the punchline survives token dropoff
    (#13); `scene` provides supporting detail; style prefix at the end
    sets aesthetic without competing with subject for attention.

    Set ``weighted=False`` for backends that don't parse compel-style
    `(text:weight)` syntax (Flux). The key_visual is then emitted as
    plain text — still first, so it still gets attention priority via
    position alone.
    """
    parts: list[str] = []
    if character_description and character_description.strip():
        parts.append(character_description.strip())
    if key_visual and key_visual.strip():
        kv = key_visual.strip().rstrip(".")
        parts.append(f"({kv}:{key_visual_weight})" if weighted else kv)
    if scene and scene.strip():
        parts.append(scene.strip())
    if style_prefix and style_prefix.strip():
        parts.append(style_prefix.strip())
    return ". ".join(parts)


# --- Pipeline lazy-load ------------------------------------------------


def _pipe(*, want_ip_adapter: bool = False) -> AutoPipelineForText2Image:
    """Load (and cache) the SDXL-Lightning pipeline. Optionally attach IP-Adapter."""
    global _PIPE, _IP_ADAPTER_LOADED
    if _PIPE is None:
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        dtype = torch.float16 if device == "mps" else torch.float32
        pipe = AutoPipelineForText2Image.from_pretrained(
            _BASE_REPO,
            torch_dtype=dtype,
            variant="fp16" if dtype == torch.float16 else None,
        )
        # Load and fuse the 4-step Lightning LoRA.
        lora_path = hf_hub_download(_LIGHTNING_REPO, _LIGHTNING_CKPT)
        pipe.load_lora_weights(lora_path)
        pipe.fuse_lora()
        # Lightning requires Euler with trailing timestep spacing.
        pipe.scheduler = EulerDiscreteScheduler.from_config(
            pipe.scheduler.config, timestep_spacing="trailing"
        )
        pipe = pipe.to(device)
        pipe.set_progress_bar_config(disable=True)
        _PIPE = pipe

    if want_ip_adapter and not _IP_ADAPTER_LOADED:
        _PIPE.load_ip_adapter(
            _IP_ADAPTER_REPO,
            subfolder=_IP_ADAPTER_SUBFOLDER,
            weight_name=_IP_ADAPTER_WEIGHT,
        )
        _IP_ADAPTER_LOADED = True

    return _PIPE


_NEGATIVE = (
    "anime, manga, comic book, polished illustration, fine-art sketch, "
    "professional rendering, complex shading, gradient, glow, "
    "photorealistic, 3d render, detailed background, noisy textures, "
    "monochrome, grayscale, black and white, "
    "malformed, deformed, extra fingers, extra limbs, blurry, ugly, "
    # No in-image text or numerals. Critic finding 2026-05: a beat
    # rendered with a gibberish "a" letter on the character's hoodie,
    # and another with a floating "1.4" above her head. Adding explicit
    # negative tokens stops the model from hallucinating text/labels —
    # only effective on providers that respect the negative prompt
    # (sdxl_lightning, sd_turbo). mflux schnell / z_image_turbo are
    # guidance-distilled and ignore negative prompts; a stronger fix
    # for those is the lint_prompt() filter on the positive side.
    "letters, numbers, digits, decimals, monogram, emblem, logo, "
    "label, sign, lettering, calligraphy, "
    "watermark, signature, low quality"
)


# --- Provider capabilities --------------------------------------------
#
# Channel YAML can pick provider, dims, and steps independently. Without
# a capability table, mismatches (e.g. sd_turbo asked to render 768x1344
# vertical when it was trained for 512x512 only) fail SILENTLY with
# smudgy output that looks "OK enough" to ship but is actually broken.
#
# Each entry declares:
#   native_dim       — the dim the model was trained on (peak quality)
#   max_dim          — soft cap; beyond this, output degrades visibly
#   step_range       — (lo, hi) inclusive; outside this is wrong-tool-for-the-job
#   vertical_9_16_safe — True if the model produces coherent 9:16 vertical;
#                        False for 1:1-only models like SD-Turbo
#   description      — one-line summary surfaced in error messages
_PROVIDER_CAPABILITIES: dict[str, dict] = {
    "sd_turbo": {
        "native_dim": (512, 512),
        "max_dim": (768, 768),
        "step_range": (1, 4),
        "vertical_9_16_safe": False,
        "description": (
            "SD-Turbo (Stability, Nov 2023). Trained for 512x512 only — "
            "severely degrades at vertical 9:16 or any dim > 768."
        ),
    },
    "sdxl_lightning": {
        "native_dim": (1024, 1024),
        "max_dim": (1024, 1536),
        "step_range": (1, 8),
        "vertical_9_16_safe": True,
        "description": (
            "SDXL-Lightning (ByteDance, Feb 2024). 4-step LoRA on SDXL. "
            "Good prompt adherence; supports IP-Adapter for char-lock."
        ),
    },
    "mflux": {
        "native_dim": (1024, 1024),
        "max_dim": (1344, 1344),
        "step_range": (1, 8),
        "vertical_9_16_safe": True,
        "description": (
            "Flux Schnell 12B 4-bit via mflux (Black Forest Labs, 2024). "
            "Top prompt adherence at 12B; ~40GB peak RAM during gen — "
            "can swap-thrash on machines with <32GB unified memory."
        ),
    },
    "z_image_turbo": {
        "native_dim": (1024, 1024),
        "max_dim": (1344, 1344),
        "step_range": (4, 12),
        "vertical_9_16_safe": True,
        "description": (
            "Z-Image-Turbo 6B via mflux (Tongyi/Alibaba, Nov 2025). "
            "Top open-source on Arena leaderboard 2026-02; ~half the "
            "RAM of Flux Schnell."
        ),
    },
    "z_image_turbo_fal": {
        "native_dim": (1024, 1024),
        "max_dim": (1344, 1344),
        "step_range": (1, 8),
        "vertical_9_16_safe": True,
        "description": (
            "Z-Image-Turbo via fal.ai hosted API. Same model as "
            "z_image_turbo, $0.005/MP (~$0.005/image at 768x1344). "
            "Requires FAL_KEY env var. No local GPU; ~2-4s/image network."
        ),
    },
    "cloudrun_flux2_klein": {
        "native_dim": (1024, 1024),
        "max_dim": (1664, 1664),
        "step_range": (2, 8),
        "vertical_9_16_safe": True,
        "description": (
            "FLUX.2 [klein] 4B (Apache 2.0, BFL Jan 2026) on our "
            "Cloud Run NVIDIA L4 in asia-southeast1. Distilled to 4 "
            "inference steps, ~3-4 s warm /generate at 768x1344. "
            "T2I + multi-reference editing in one model. The new "
            "default per docs/research/image_gen_2026.md. Falls back "
            "to local z_image_turbo (mflux) on cloud failure via "
            "render-level circuit breaker."
        ),
    },
    "cloudrun_z_image_turbo": {
        "native_dim": (1024, 1024),
        "max_dim": (1344, 1344),
        "step_range": (4, 12),
        "vertical_9_16_safe": True,
        "description": (
            "Z-Image-Turbo 6B via Cloud Run (Apache 2.0). Same "
            "checkpoint as the local z_image_turbo (mflux) path, "
            "diffusers runtime on NVIDIA L4. Parity / risk-insurance "
            "lane next to FLUX.2 klein. Falls back to local mflux on "
            "cloud failure. NOTE 2026-05-07: cold-load reliability "
            "still WIP — see P3.5 todo."
        ),
    },
}


def validate_provider_config(
    provider: str, *, width: int, height: int, steps: int,
) -> list[str]:
    """Return a list of error strings if the (provider, width, height,
    steps) combo will produce degraded output. Empty list = config OK.

    The caller (make_shorts.py) should fail fast on any error rather
    than render a broken Short — silent garbage output is worse than
    a clear "your channel YAML picked the wrong provider for these dims"
    crash, because the broken Short will just get shipped.

    Class-of-bug guarded: a channel author picking a provider whose
    training dim doesn't match the channel's output_resolution. Past
    incident: wiki_oddities + today_in_history + aita_text all ran
    sd_turbo at 768x1344, producing smudgy charcoal-wash output.
    """
    if provider not in _PROVIDER_CAPABILITIES:
        return [
            f"unknown image_provider: {provider!r}. "
            f"Valid providers: {sorted(_PROVIDER_CAPABILITIES)}"
        ]
    cap = _PROVIDER_CAPABILITIES[provider]
    errors: list[str] = []

    # 1. Aspect-ratio check. We treat any aspect outside ~[0.66, 1.5]
    # as "non-square"; sd_turbo-class models can't handle these.
    aspect = max(width, height) / max(1, min(width, height))
    is_non_square = aspect > 1.4
    if is_non_square and not cap["vertical_9_16_safe"]:
        errors.append(
            f"provider={provider!r} cannot render non-square ({width}x{height}, "
            f"aspect {aspect:.2f}:1). {cap['description']} "
            f"Switch to z_image_turbo (recommended) or sdxl_lightning."
        )

    # 2. Hard dim cap. Past max_dim, output degrades visibly even on
    # otherwise-capable models.
    max_w, max_h = cap["max_dim"]
    if width > max_w or height > max_h:
        errors.append(
            f"provider={provider!r} max recommended dim is "
            f"{max_w}x{max_h}; channel YAML asks for {width}x{height}. "
            f"Either lower the dims or switch provider."
        )

    # 3. Step-range check. Most distilled models have a tight sweet
    # spot — asking sd_turbo for 8 steps wastes compute; asking
    # z_image_turbo for 1 step produces noise.
    lo, hi = cap["step_range"]
    if not lo <= steps <= hi:
        errors.append(
            f"provider={provider!r} expects steps in [{lo}, {hi}]; "
            f"channel YAML asks for {steps}. {cap['description']}"
        )

    return errors


def _flux_pipe():
    """Lazy-load the mflux Flux1 instance into ``_FLUX_PIPE``.

    First call downloads ~24 GB of original weights and quantizes to
    ~7 GB on disk. Idempotent: returns the cached instance on later
    calls. Pulled out of ``_generate_mflux`` so ``warmup()`` can drive
    the load on a background thread while TTS/ASR run.
    """
    global _FLUX_PIPE
    if _FLUX_PIPE is None:
        from mflux.models.common.config.model_config import ModelConfig
        from mflux.models.flux.variants.txt2img.flux import Flux1
        _FLUX_PIPE = Flux1(quantize=4, model_config=ModelConfig.schnell())
    return _FLUX_PIPE


def _z_image_pipe():
    """Lazy-load the mflux Z-Image-Turbo instance into ``_ZIMAGE_PIPE``.

    Z-Image-Turbo is a 6B-param distilled model (vs Flux Schnell's 12B)
    with comparable prompt adherence and ~half the peak RAM footprint.
    On Apple Silicon this is the difference between fitting in unified
    memory and swapping to disk — i.e. the difference between 30 s/img
    and 5 min/img. Generates at 8 NFEs (Number of Function Evaluations).

    Same MLX thread-local-streams gotcha as Flux1: load on the same
    thread that calls ``generate()``. ``warmup()`` is a no-op for this
    provider for that reason.

    quantize=4: smaller weights, less per-step memory traffic. On the
    M1/M2 path mflux disables ``mx.compile`` (z_image.py L211), so
    every step pays full Python-overhead-per-MLX-op cost and bandwidth
    is the bottleneck — 4-bit halves that vs 8-bit. Quality drop is
    negligible at this distillation grade. Re-bump to 8 if you ever
    move to M3+ where compile re-enables and bandwidth stops biting.
    """
    global _ZIMAGE_PIPE
    if _ZIMAGE_PIPE is None:
        from mflux.models.common.config.model_config import ModelConfig
        from mflux.models.z_image.variants.z_image import ZImage
        _ZIMAGE_PIPE = ZImage(quantize=4, model_config=ModelConfig.z_image_turbo())
    return _ZIMAGE_PIPE


def reset_image_state() -> None:
    """Drop the diffusion-pipe singletons + the SDXL pipe + IP-Adapter flag.

    Mirrors :func:`pipeline.audio.reset_f5_state`. Call this when an
    image-gen stage finishes and the next stages don't need diffusion
    — frees ~3-4 GB of MLX state for the Flux/Z-Image case and ~6-7 GB
    for the SDXL case, so the next Metal-using stage starts on a
    clean unified-memory heap.

    Wired into :func:`pipeline.preflight.reset_mlx_state` via the
    ``drop_image=True`` kwarg, so renderers can request the drop in
    one line:

    .. code-block:: python

        from pipeline.preflight import reset_mlx_state
        reset_mlx_state(drop_f5=False, drop_image=True,
                        label="long-form image-panel stage")

    Best-effort — never raises. Safe to call when no diffusion pipe
    has been loaded yet (the singletons stay None).
    """
    global _PIPE, _IP_ADAPTER_LOADED, _FLUX_PIPE, _ZIMAGE_PIPE
    _PIPE = None
    _IP_ADAPTER_LOADED = False
    _FLUX_PIPE = None
    _ZIMAGE_PIPE = None
    try:
        import mlx.core as _mx  # type: ignore  # noqa: PLC0415
        if hasattr(_mx, "clear_cache"):
            _mx.clear_cache()
        elif hasattr(_mx, "metal") and hasattr(_mx.metal, "clear_cache"):
            _mx.metal.clear_cache()
    except Exception:  # noqa: BLE001
        pass


def warmup(provider: str, *, want_ip_adapter: bool = False) -> None:
    """Eagerly load the diffusion pipeline so the first ``generate()`` call
    doesn't pay the ~15–30 s cold-load tax.

    SAFE TO CALL FROM A BACKGROUND THREAD ONLY for SDXL providers.
    mflux uses Apple MLX, which binds device streams (and the buffers
    that reference them) to the thread that creates them — so loading
    Flux1 on one thread and generating on another crashes with
    "There is no Stream(gpu, 1) in current thread". For ``provider=mflux``
    this function is a no-op; mflux's lazy-load on the first
    ``generate()`` (which runs on the main thread) handles it correctly.

    Designed to run on a background thread at the top of make_shorts.py
    so TTS + ASR + beat-prompt authoring (~60–90 s of mostly-CPU work
    where the GPU is idle) happens concurrently with the model load.
    Idempotent and cheap to call when already warm — guarded by the
    ``_PIPE`` module-global the generator reads.

    Catches and logs any error: a warmup failure should not crash the
    job (the lazy-load on the actual ``generate()`` will surface the
    real error path; this is just an optimisation).
    """
    import time as _time
    t0 = _time.time()
    try:
        if provider in ("mflux", "z_image_turbo"):
            # Cross-thread MLX is unsafe — caller in make_shorts.py
            # already gates on this, but defend in depth here too.
            print(
                f"[warmup] {provider} uses MLX (thread-local streams); skipping "
                "threaded warmup. Lazy-load on first generate() instead."
            )
            return
        if provider in ("cloudrun_flux2_klein", "cloudrun_z_image_turbo"):
            # Cloud providers: fire-and-forget /readyz on a background
            # thread so the cloud service starts cold-loading (~5-7 min
            # for FLUX, longer for Z-Image) BEFORE the first /generate
            # call hits. Hides the cold-load behind concurrent TTS/ASR
            # work in make_shorts.py. The thread doesn't block this
            # function — caller doesn't need to .join() it; first
            # /generate will benefit from the warm container.
            from pipeline.images_cloudrun import warmup as _cloud_warmup
            model = provider.removeprefix("cloudrun_")
            _cloud_warmup(model)
            print(f"[warmup] {provider} /readyz fired on background thread")
            return
        if provider in ("sdxl_lightning", "sd_turbo"):
            _pipe(want_ip_adapter=want_ip_adapter)
        else:
            print(f"[warmup] unknown provider {provider!r}; skipping")
            return
        print(f"[warmup] {provider} pipeline ready in {_time.time() - t0:.1f}s")
    except Exception as e:  # pragma: no cover — defensive
        print(f"[warmup] {provider} preload failed (will lazy-load on first generate): {e}")


# --- Generation --------------------------------------------------------


def _generate_via_worker(worker_url: str, kwargs: dict) -> Path:
    """POST one render to the long-lived server-side worker and block
    until the image is on disk. Used when YTFACTORY_IMAGE_WORKER_URL is
    set (opt-in via YTFACTORY_PERSIST_IMAGE_PIPE=1 on the server).

    The worker runs the same `generate()` body in a single, warm
    process — saving the 15-30s cold load every time make_shorts.py
    forks. We send Path objects as strings; the worker reconstitutes.
    """
    import urllib.request
    import urllib.error
    payload = {"kwargs": {
        **{k: v for k, v in kwargs.items()
           if k not in ("out_path", "ip_adapter_image")},
        "out_path": str(kwargs["out_path"]),
        "ip_adapter_image": (
            str(kwargs["ip_adapter_image"])
            if kwargs.get("ip_adapter_image") else None
        ),
    }}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        worker_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Generous timeout — render can be 30-60s on a slow first call.
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return Path(body["path"])


def generate(
    prompt: str,
    style_prefix: str,
    seed: int,
    out_path: Path,
    width: int = 768,
    height: int = 1344,
    steps: int = 4,
    *,
    provider: str = "sdxl_lightning",
    ip_adapter_image: Path | None = None,
    ip_adapter_scale: float = 0.6,
    extra_negative: str | list[str] | None = None,
    force_positive: str | list[str] | None = None,
) -> Path:
    """Generate one image. Dispatches on ``provider``.

    ``ip_adapter_image`` / ``ip_adapter_scale`` apply only on the
    ``sdxl_lightning`` path. Flux conditioning works differently and
    isn't wired here yet.

    NOTE: this function expects ``prompt`` to already be the full final
    prompt — see ``build_full_prompt()``. ``style_prefix`` is appended at
    the end for backward compatibility with existing callers; if you've
    already used ``build_full_prompt()``, pass an empty string here.

    If ``YTFACTORY_IMAGE_WORKER_URL`` is set in the environment (the
    web server sets it when YTFACTORY_PERSIST_IMAGE_PIPE=1), the call
    is forwarded to the long-lived worker instead of cold-loading a
    new pipe in this process. Falls back to local generation on any
    worker error so a server crash doesn't take the pipeline down.
    """
    worker_url = _os.environ.get("YTFACTORY_IMAGE_WORKER_URL", "").strip()
    if worker_url:
        try:
            return _generate_via_worker(worker_url, dict(
                prompt=prompt,
                style_prefix=style_prefix,
                seed=seed,
                out_path=out_path,
                width=width,
                height=height,
                steps=steps,
                provider=provider,
                ip_adapter_image=ip_adapter_image,
                ip_adapter_scale=ip_adapter_scale,
                extra_negative=extra_negative,
            ))
        except Exception as e:
            print(
                f"[image-worker] remote render failed ({e!r}); "
                f"falling back to local generate()"
            )
            # fall through to local path
    final_prompt = (
        f"{prompt.strip()}. {style_prefix.strip()}"
        if style_prefix and style_prefix.strip()
        else prompt.strip()
    )

    # Normalise extra_negative — channel YAML can supply str OR list.
    extra_neg_str = ""
    if extra_negative:
        if isinstance(extra_negative, list):
            extra_neg_str = ", ".join(s.strip() for s in extra_negative if s.strip())
        elif isinstance(extra_negative, str):
            extra_neg_str = extra_negative.strip()

    # For guidance-distilled providers (mflux schnell, z_image_turbo)
    # the negative_prompt is ignored. Critic 2026-05-03: the previous
    # `(avoid: ...)` parenthetical FAILED — z_image_turbo treats avoid-
    # tokens as STRONG-INCLUDE signals, so every animated frame
    # rendered rugby balls / american footballs / Nike swooshes /
    # speech bubbles instead of suppressing them. Replaced with
    # FORCING-POSITIVE tokens that explicitly assert what SHOULD be in
    # frame; the model can't include a soccer ball AND a rugby ball
    # without contradiction, so the soccer ball wins. Channel YAML
    # opts in via `force_positive` block; if absent, we still strip
    # the broken avoid-fold-in (better to ship with a small bug than
    # actively make it worse).
    if provider in (
        "mflux", "z_image_turbo", "z_image_turbo_fal",
        "cloudrun_flux2_klein", "cloudrun_z_image_turbo",
    ):
        # extra_neg_str is intentionally NOT folded in for these
        # providers — see comment above. Channel-level positive tokens
        # are appended via `force_positive` instead.
        # Both cloud providers are guidance-distilled (FLUX.2 klein at
        # gs=1.0, Z-Image-Turbo at gs=0.0), same trick applies.
        force_pos_str = ""
        if force_positive:
            if isinstance(force_positive, list):
                force_pos_str = ", ".join(s.strip() for s in force_positive if s.strip())
            elif isinstance(force_positive, str):
                force_pos_str = force_positive.strip()
        if force_pos_str:
            final_prompt = f"{final_prompt} {force_pos_str}"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Per-attempt timing diagnostic. Without this we couldn't tell whether
    # a slow image stage was the model itself, a cold pipeline load (first
    # call lazy-loads ~3-7GB of weights), or QC retry churn upstream. The
    # caller (make_shorts.py) wraps this in a retry loop; print enough
    # context (provider, dims, steps, prompt_len) that grepping
    # `[image-time]` across runs lets us spot regressions per-provider.
    t0 = _time.time()
    pipe_was_loaded = _is_pipe_loaded(provider)

    if provider == "mflux":
        result = _generate_mflux(
            prompt=final_prompt,
            seed=seed,
            out_path=out_path,
            width=width,
            height=height,
            steps=steps,
        )
    elif provider == "z_image_turbo":
        result = _generate_z_image_turbo(
            prompt=final_prompt,
            seed=seed,
            out_path=out_path,
            width=width,
            height=height,
            steps=steps,
        )
    elif provider == "z_image_turbo_fal":
        result = _generate_z_image_turbo_fal(
            prompt=final_prompt,
            seed=seed,
            out_path=out_path,
            width=width,
            height=height,
            steps=steps,
        )
    elif provider == "cloudrun_flux2_klein":
        from pipeline.images_cloudrun import _generate_cloudrun_flux2_klein
        result = _generate_cloudrun_flux2_klein(
            prompt=final_prompt,
            seed=seed,
            out_path=out_path,
            width=width,
            height=height,
            steps=steps,
        )
    elif provider == "cloudrun_z_image_turbo":
        from pipeline.images_cloudrun import _generate_cloudrun_z_image_turbo
        result = _generate_cloudrun_z_image_turbo(
            prompt=final_prompt,
            seed=seed,
            out_path=out_path,
            width=width,
            height=height,
            steps=steps,
        )
    elif provider in ("sdxl_lightning", "sd_turbo"):
        result = _generate_sdxl(
            prompt=final_prompt,
            seed=seed,
            out_path=out_path,
            width=width,
            height=height,
            steps=steps,
            ip_adapter_image=ip_adapter_image,
            ip_adapter_scale=ip_adapter_scale,
            extra_negative=extra_neg_str or None,
        )
    else:
        raise ValueError(f"unknown image_provider: {provider!r}")

    dt = _time.time() - t0
    cold = "" if pipe_was_loaded else " cold-load"
    per_step = (dt / steps) if steps > 0 else dt
    print(
        f"[image-time] provider={provider} {width}x{height} steps={steps} "
        f"prompt_len={len(final_prompt)} wall={dt:.1f}s "
        f"per_step={per_step:.1f}s{cold}"
    )
    return result


def _is_pipe_loaded(provider: str) -> bool:
    """Best-effort guess at whether the diffusion pipe is already in
    memory. Used only to tag image-time logs with cold/warm so the
    first-image timing isn't compared against subsequent ones.
    """
    if provider == "mflux":
        return _FLUX_PIPE is not None
    if provider == "z_image_turbo":
        return _ZIMAGE_PIPE is not None
    if provider == "z_image_turbo_fal":
        return True  # hosted; no local pipe to cold-load
    if provider in ("cloudrun_flux2_klein", "cloudrun_z_image_turbo"):
        # The cloud service holds its own pipe state; from the laptop
        # we have no way to know if it's warm without an extra round-
        # trip, so just always tag as warm. Cold-loads on the cloud
        # side are visible in the per-call response's `cold_loaded`
        # field, which `pipeline.images_cloudrun` logs separately.
        return True
    if provider in ("sdxl_lightning", "sd_turbo"):
        return _PIPE is not None
    return False


def _generate_sdxl_with_negative(neg: str | None = None) -> str:
    """Build the final SDXL negative prompt — base + per-channel extras."""
    if neg and neg.strip():
        return f"{_NEGATIVE}, {neg.strip()}"
    return _NEGATIVE


_IP_REF_CACHE: dict[tuple[str, int], "Image.Image"] = {}


def _load_ip_ref(p: Path) -> "Image.Image":
    """Cache the decoded IP-adapter reference. Same path is passed on
    every beat × every retry within a job — we were paying the
    Image.open + RGB-convert cost ~30-120× per job. The cache key
    includes the file's mtime so a re-rendered reference invalidates."""
    try:
        st_mtime_ns = p.stat().st_mtime_ns
    except OSError:
        st_mtime_ns = 0
    key = (str(p.resolve()), st_mtime_ns)
    img = _IP_REF_CACHE.get(key)
    if img is None:
        img = Image.open(p).convert("RGB")
        _IP_REF_CACHE[key] = img
    return img


def _generate_sdxl(
    *,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
    ip_adapter_image: Path | None,
    ip_adapter_scale: float,
    extra_negative: str | None = None,
) -> Path:
    """SDXL-Lightning + optional IP-Adapter (Principle #14)."""
    # NOTE: the caller is responsible for linting (see lint_prompt()).
    use_ip = ip_adapter_image is not None
    pipe = _pipe(want_ip_adapter=use_ip)

    if use_ip:
        pipe.set_ip_adapter_scale(ip_adapter_scale)
        ref_img = _load_ip_ref(ip_adapter_image)
    elif _IP_ADAPTER_LOADED:
        pipe.set_ip_adapter_scale(0.0)
        ref_img = None
    else:
        ref_img = None

    generator = torch.Generator(device="cpu").manual_seed(seed)
    pipe_kwargs = dict(
        prompt=prompt,
        negative_prompt=_generate_sdxl_with_negative(extra_negative),
        num_inference_steps=steps,
        guidance_scale=0.0,
        width=width,
        height=height,
        generator=generator,
    )
    if ref_img is not None:
        pipe_kwargs["ip_adapter_image"] = ref_img

    image = pipe(**pipe_kwargs).images[0]
    image.save(out_path)
    return out_path


def _generate_mflux(
    *,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """Flux Schnell 4-bit via mflux/MLX. First call downloads ~24 GB
    of original weights and quantizes to ~7 GB on disk (after that
    the load is the disk-→MPS transfer, ~15–30 s cold). Use
    ``warmup("mflux")`` from a background thread to hide the load
    behind concurrent TTS/ASR work.

    Schnell is guidance-distilled (no CFG) — the API still takes a
    `guidance` arg but it's a no-op at this model config.

    Flux dimensions must be multiples of 16. We round to the nearest
    multiple to avoid surprising the user with a different aspect ratio.
    """
    _flux_pipe()  # populates _FLUX_PIPE on first call (idempotent)

    w = max(16, (width // 16) * 16)
    h = max(16, (height // 16) * 16)

    generated = _FLUX_PIPE.generate_image(
        seed=seed,
        prompt=prompt,
        num_inference_steps=steps,
        width=w,
        height=h,
    )
    generated.save(path=str(out_path), overwrite=True)
    return out_path


def _generate_z_image_turbo(
    *,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """Z-Image-Turbo (6B) via mflux/MLX. Drop-in replacement for the
    Schnell path with ~half the RAM footprint — first call downloads
    weights to the HF cache and quantizes; subsequent calls are
    interactive on Apple Silicon.

    Z-Image is guidance-distilled like Schnell (no CFG); the API still
    accepts ``guidance`` but it's clamped to 0 internally for the Turbo
    config. 8 NFEs is the recommended sweet spot for full quality.

    Dimensions don't have a hard multiple-of-16 requirement like Flux
    but the latent packer still expects even dims — round defensively.
    """
    _z_image_pipe()  # populates _ZIMAGE_PIPE on first call (idempotent)

    w = max(16, (width // 16) * 16)
    h = max(16, (height // 16) * 16)

    generated = _ZIMAGE_PIPE.generate_image(
        seed=seed,
        prompt=prompt,
        num_inference_steps=steps,
        width=w,
        height=h,
    )
    generated.save(path=str(out_path), overwrite=True)
    return out_path


def _generate_z_image_turbo_fal(
    *,
    prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> Path:
    """Z-Image-Turbo via fal.ai hosted API. Same model as the local
    mflux path; trades ~10-15s of M2 Max GPU work for a 2-4s network
    round trip + $0.005 per 1MP image.

    Requires FAL_KEY in env. fal_client.subscribe() blocks until the
    queued job finishes and returns a JSON dict with image URLs.
    """
    import urllib.request

    import fal_client

    if not _os.environ.get("FAL_KEY"):
        raise RuntimeError(
            "z_image_turbo_fal requires FAL_KEY env var. "
            "Get one from https://fal.ai/dashboard/keys."
        )

    w = max(16, (width // 16) * 16)
    h = max(16, (height // 16) * 16)

    result = fal_client.subscribe(
        "fal-ai/z-image/turbo",
        arguments={
            "prompt": prompt,
            "image_size": {"width": w, "height": h},
            "num_inference_steps": steps,
            "seed": seed,
            "num_images": 1,
            "output_format": "png",
            "enable_safety_checker": False,
        },
        with_logs=False,
    )
    images = result.get("images") or []
    if not images:
        raise RuntimeError(
            f"fal-ai/z-image/turbo returned no images: {result!r}"
        )
    url = images[0].get("url")
    if not url:
        raise RuntimeError(
            f"fal-ai/z-image/turbo response missing url: {images[0]!r}"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as resp:
        out_path.write_bytes(resp.read())
    return out_path


# --- Prompts file ------------------------------------------------------


def beat_to_prompt(beat_text: str) -> str:
    """Heuristic fallback for v0; real prompts go in prompts.json."""
    return f"a single character {beat_text.strip().rstrip('.,!?;:').lower()}"


def _tokenise_for_match(s: str) -> set[str]:
    """Cheap word-bag for prompt-to-beat anchoring (Phase-1 sync fix)."""
    import re as _re
    return {t for t in _re.findall(r"[a-z0-9']+", s.lower()) if len(t) > 1}


def _align_prompts_to_beats(
    raw: list[dict],
    beat_texts: list[str],
) -> list[dict] | None:
    """Bind each beat to a prompt by token-overlap with a monotonic
    constraint, instead of zipping by index.

    Two failure modes the index zip lets through and this fixes:

    * The LLM author drifts (puts beat-3's content into prompt 2),
      so image[i] illustrates the wrong spoken moment.
    * The beat splitter merges/splits sub-sentences differently from
      what the LLM was shown, breaking 1:1 ordering downstream.

    Algorithm: dynamic programming over (beat_i, prompt_j) maximising
    total Jaccard overlap subject to ORDER PRESERVATION — beat[i] may
    only pair with a prompt index >= the prompt picked for beat[i-1].
    Prompts can be skipped (more prompts than beats); beats cannot be
    (every beat must get an image). Cost ≈ O(N·M); both are < 20.

    Returns None if not all prompts carry a ``narration_line`` anchor
    (caller falls back to index-zip after a length check).
    """
    if not all(isinstance(p, dict) and (p.get("narration_line") or "").strip() for p in raw):
        return None

    n = len(beat_texts)
    m = len(raw)
    if n == 0 or m == 0:
        return None
    # Class-of-bug guard (2026-05-03 hathi-raja render): the DP below
    # assumes m >= n so every beat can claim a unique-and-monotonic
    # prompt index. When the cached prompts.json is from a PRIOR
    # narration with fewer beats (e.g. the rhyme channel re-rendered
    # with sung audio that splits into 20 beats vs the spoken 12),
    # range(n-1, m) is empty and max() crashes. Return None here so
    # the caller falls back to heuristic / LLM re-author rather than
    # crashing the whole render.
    if m < n:
        print(
            f"[images] cached prompts.json has {m} entries but the "
            f"current narration produces {n} beats — falling back to "
            f"re-author (delete prompts.json to silence this warning)"
        )
        return None

    beat_tokens = [_tokenise_for_match(t) for t in beat_texts]
    prompt_tokens = [_tokenise_for_match(p["narration_line"]) for p in raw]

    def overlap(bi: int, pi: int) -> float:
        bt, pt = beat_tokens[bi], prompt_tokens[pi]
        if not bt or not pt:
            return 0.0
        inter = len(bt & pt)
        union = len(bt | pt)
        return inter / union if union else 0.0

    # f[i][j] = best total score pairing beats[0..i] with prompts in
    # [0..j], where beat i is bound to prompt j. Sentinel -inf for
    # unreachable states.
    NEG = float("-inf")
    f = [[NEG] * m for _ in range(n)]
    back = [[-1] * m for _ in range(n)]
    for j in range(m):
        f[0][j] = overlap(0, j)
    for i in range(1, n):
        for j in range(i, m):  # need at least i prior prompts available
            # best predecessor: any j' < j on row i-1
            best_prev = NEG
            best_prev_j = -1
            for jp in range(i - 1, j):
                if f[i - 1][jp] > best_prev:
                    best_prev = f[i - 1][jp]
                    best_prev_j = jp
            if best_prev > NEG:
                f[i][j] = best_prev + overlap(i, j)
                back[i][j] = best_prev_j

    # Pick the j that maximises f[n-1][*].
    last_j = max(range(n - 1, m), key=lambda j: f[n - 1][j])

    # Reconstruct the chosen prompt index per beat.
    picks: list[int] = [0] * n
    j = last_j
    for i in range(n - 1, -1, -1):
        picks[i] = j
        j = back[i][j] if i > 0 else -1

    aligned: list[dict] = []
    for bi, pi in enumerate(picks):
        score = overlap(bi, pi)
        if score < 0.15:
            print(
                f"[images] WARNING: beat {bi} ({beat_texts[bi][:40]!r}) "
                f"weak prompt anchor (score={score:.2f}); kept order anyway"
            )
        aligned.append(raw[pi])
        print(
            f"[images] anchor: beat {bi} ← prompt {pi} "
            f"(score={score:.2f}) {raw[pi]['narration_line'][:50]!r}"
        )
    return aligned


def load_prompts(
    path: Path, n_beats: int, *, beat_texts: list[str] | None = None,
) -> list[dict[str, str]] | None:
    """Load per-beat prompts from JSON if present. Returns a list of
    ``{"key_visual": str | None, "scene": str}`` dicts, normalising the
    legacy string-list form transparently.

    Two alignment modes:

    1. **Anchor mode** (recommended). Each prompt entry carries a
       ``narration_line`` field declaring which words it's meant to
       illustrate. Prompts are bound to beats by highest token-overlap
       (Jaccard), not by index — robust to splitter drift. Triggered
       when ``beat_texts`` is provided AND every prompt has an anchor.

    2. **Index mode** (legacy). Prompt[i] pairs with beat[i] by
       position. Returns None on length mismatch (caller falls back to
       heuristic / LLM-author).
    """
    if not path.exists():
        return None
    with path.open() as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        print(f"[images] WARNING: {path} is not a JSON list; using heuristic prompts")
        return None

    # Phase-1: anchor-based alignment. Lets us survive prompt-vs-beat
    # count mismatches AND splitter drift.
    if beat_texts is not None and isinstance(raw, list) and raw:
        anchored = _align_prompts_to_beats(raw, beat_texts)
        if anchored is not None:
            raw = anchored

    if len(raw) != n_beats:
        print(
            f"[images] WARNING: {path} has {len(raw)} prompts but the "
            f"narration produced {n_beats} beats; using heuristic prompts"
        )
        return None

    normalised: list[dict[str, str]] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            normalised.append({"key_visual": "", "scene": item})
        elif isinstance(item, dict):
            kv = item.get("key_visual", "") or ""
            sc = item.get("scene", "") or ""
            if not sc:
                print(
                    f"[images] WARNING: {path}[{i}] has no 'scene' field; "
                    f"using empty string"
                )
            normalised.append({"key_visual": kv, "scene": sc})
        else:
            print(
                f"[images] WARNING: {path}[{i}] is neither string nor dict; "
                f"using heuristic prompts"
            )
            return None
    return normalised
