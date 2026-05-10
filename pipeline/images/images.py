"""Stage 6 — image generation (cloud-only post nuclear-cleanup 2026-05-09).

All image generation runs on Cloud Run NVIDIA L4 (or Azure AKS A10).
Local providers (sdxl_lightning, mflux Flux Schnell, mflux Z-Image-Turbo,
fal.ai hosted Z-Image-Turbo) were removed 2026-05-09 — every channel's
production stack has been on ``cloudrun_flux2_klein`` since 2026-05-07.
The render-worker (``cloud/render-worker-v2/``) doesn't have torch /
diffusers / mflux installed; this module dispatches HTTP requests to
the cloud GPU services only.

Principles enforced (DESIGN.md §14):

* **#3 Forbidden text-bait phrases** — ``lint_prompt()`` warns on words
  that ask for legible text in the image.
* **#11 Token-budget warning** — ``lint_prompt()`` warns when the
  scene portion exceeds ~50 tokens.
* **#12 Multi-subject warning** — ``lint_prompt()`` flags plural-subject
  patterns ("three friends", "several characters").
* **#13 Key-visual weighting** — emitted as plain leading text on the
  cloud guidance-distilled providers (FLUX.2 klein, Z-Image-Turbo).
* **#14 IP-Adapter character lock** — was wired only on the SDXL
  laptop path, which is gone. Multi-reference editing in FLUX.2 klein
  is the cloud-side replacement (not yet wired into ``generate()``).
"""

from __future__ import annotations

import json
import os as _os
import re
import time as _time
from pathlib import Path
from threading import Thread as _Thread

# Module-state stubs — kept as None forever post-cleanup so any leftover
# `_FLUX_PIPE is not None` truthiness check evaluates correctly. Their
# only remaining purpose is back-compat for code that mutates them.
_PIPE = None
_IP_ADAPTER_LOADED = False
_FLUX_PIPE = None
_ZIMAGE_PIPE = None


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
    # ---- Azure AKS GPU mirrors (australiaeast) -------------------------
    # Same dims/step ranges as cloudrun_*; opt-in via image_provider=azure_*.
    # On AzureImageUnavailable the wrapper in pipeline.images.images_azure falls
    # through to cloudrun_<model> which itself falls through to local mflux.
    "azure_flux2_klein": {
        "native_dim": (1024, 1024),
        "max_dim": (1664, 1664),
        "step_range": (2, 8),
        "vertical_9_16_safe": True,
        "description": (
            "FLUX.2 [klein] 4B on Azure AKS NVIDIA A10 in australiaeast. "
            "Mirror of cloudrun_flux2_klein; falls through to GCP then "
            "local mflux on Azure outage. Render-level circuit breaker."
        ),
    },
    "azure_z_image_turbo": {
        "native_dim": (1024, 1024),
        "max_dim": (1344, 1344),
        "step_range": (4, 12),
        "vertical_9_16_safe": True,
        "description": (
            "Z-Image-Turbo 6B on Azure AKS NVIDIA A10. Mirror of "
            "cloudrun_z_image_turbo; same cold-load WIP caveat."
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


def reset_image_state() -> None:
    """No-op as of 2026-05-09 (laptop nuclear cleanup).

    Used to drop the local diffusion-pipe singletons (SDXL / mflux Flux /
    mflux Z-Image-Turbo). All cloud providers hold their own pipe state
    inside the Cloud Run service container, so there's nothing to reset
    on the laptop side. Kept as a callable so existing renderer call
    sites and ``pipeline.quality.preflight.reset_mlx_state(drop_image=True)``
    still compile.
    """
    global _PIPE, _IP_ADAPTER_LOADED, _FLUX_PIPE, _ZIMAGE_PIPE
    _PIPE = None
    _IP_ADAPTER_LOADED = False
    _FLUX_PIPE = None
    _ZIMAGE_PIPE = None


def warmup(
    provider: str, *, want_ip_adapter: bool = False
) -> _Thread | None:
    """Pre-warm the cloud image service via /readyz before generate().

    Cloud-only as of 2026-05-09 (laptop nuclear cleanup). Fires
    /readyz on a background thread so the cloud service starts cold-
    loading (~5-7 min for FLUX, longer for Z-Image) BEFORE the first
    /generate call hits.

    ``want_ip_adapter`` is accepted for back-compat (old SDXL warmup
    signature) but ignored — IP-Adapter only applied on the deleted
    SDXL laptop path.

    Returns the background ``threading.Thread`` so callers can
    ``.join(timeout=...)`` it right before the first /generate, to
    guarantee the cold-load actually completed instead of racing
    against the real request. Returns ``None`` when the provider is
    unknown or doesn't support warmup (so callers can ``if t: t.join``).

    Catches and logs any error: a warmup failure should not crash the
    job; the first /generate will pay the cold-load instead.
    """
    t0 = _time.time()
    try:
        if provider in ("cloudrun_flux2_klein", "cloudrun_z_image_turbo"):
            from pipeline.images.images_cloudrun import warmup as _cloud_warmup
            model = provider.removeprefix("cloudrun_")
            t = _cloud_warmup(model)
            print(f"[warmup] {provider} /readyz fired on background thread")
            return t
        if provider in ("azure_flux2_klein", "azure_z_image_turbo"):
            from pipeline.images.images_azure import warmup as _azure_warmup
            model = provider.removeprefix("azure_")
            t = _azure_warmup(model)
            print(f"[warmup] {provider} /readyz fired on background thread")
            return t
        print(f"[warmup] unknown provider {provider!r}; skipping (only cloudrun_*/azure_* are supported post 2026-05-09)")
        return None
    except Exception as e:  # pragma: no cover — defensive
        print(f"[warmup] {provider} preload failed (will lazy-load on first generate): {e}")
        return None


# --- Generation --------------------------------------------------------


def generate(
    prompt: str,
    style_prefix: str,
    seed: int,
    out_path: Path,
    width: int = 768,
    height: int = 1344,
    steps: int = 4,
    *,
    provider: str = "cloudrun_flux2_klein",
    ip_adapter_image: Path | None = None,  # accepted for back-compat; ignored
    ip_adapter_scale: float = 0.6,         # accepted for back-compat; ignored
    extra_negative: str | list[str] | None = None,  # accepted; cloud providers ignore neg
    force_positive: str | list[str] | None = None,
) -> Path:
    """Generate one image. Dispatches on ``provider``.

    All providers are cloud GPU services as of 2026-05-09 (laptop
    nuclear cleanup). Local providers (sdxl_lightning, mflux,
    z_image_turbo, z_image_turbo_fal) were removed — restore from
    git history if revival is needed.

    ``ip_adapter_image`` / ``ip_adapter_scale`` / ``extra_negative``
    are accepted for back-compat with old call sites but ignored —
    they only applied on the deleted SDXL/laptop paths.

    NOTE: ``prompt`` is expected to already be the full final prompt —
    see ``build_full_prompt()``. ``style_prefix`` is appended at the
    end for back-compat.
    """
    final_prompt = (
        f"{prompt.strip()}. {style_prefix.strip()}"
        if style_prefix and style_prefix.strip()
        else prompt.strip()
    )

    # Cloud guidance-distilled providers (FLUX.2 klein at gs=1.0,
    # Z-Image-Turbo at gs=0.0) ignore negative prompts. Channel YAML
    # opts in via ``force_positive`` to assert what SHOULD be in frame
    # (the only knob that actually shifts cloud diffusion output).
    if provider in (
        "cloudrun_flux2_klein", "cloudrun_z_image_turbo",
        "azure_flux2_klein", "azure_z_image_turbo",
    ):
        force_pos_str = ""
        if force_positive:
            if isinstance(force_positive, list):
                force_pos_str = ", ".join(s.strip() for s in force_positive if s.strip())
            elif isinstance(force_positive, str):
                force_pos_str = force_positive.strip()
        if force_pos_str:
            final_prompt = f"{final_prompt} {force_pos_str}"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Per-attempt timing diagnostic. The cloud service holds its own
    # pipe state; from the laptop / render-worker we have no way to
    # know if it's warm without an extra round-trip, so always tag as
    # warm. Cold-loads on the cloud side surface in the per-call
    # response's ``cold_loaded`` field, logged separately by the
    # cloudrun/azure modules.
    t0 = _time.time()

    if provider == "cloudrun_flux2_klein":
        from pipeline.images.images_cloudrun import _generate_cloudrun_flux2_klein
        result = _generate_cloudrun_flux2_klein(
            prompt=final_prompt, seed=seed, out_path=out_path,
            width=width, height=height, steps=steps,
        )
    elif provider == "cloudrun_z_image_turbo":
        from pipeline.images.images_cloudrun import _generate_cloudrun_z_image_turbo
        result = _generate_cloudrun_z_image_turbo(
            prompt=final_prompt, seed=seed, out_path=out_path,
            width=width, height=height, steps=steps,
        )
    elif provider == "azure_flux2_klein":
        from pipeline.images.images_azure import _generate_azure_flux2_klein
        result = _generate_azure_flux2_klein(
            prompt=final_prompt, seed=seed, out_path=out_path,
            width=width, height=height, steps=steps,
        )
    elif provider == "azure_z_image_turbo":
        from pipeline.images.images_azure import _generate_azure_z_image_turbo
        result = _generate_azure_z_image_turbo(
            prompt=final_prompt, seed=seed, out_path=out_path,
            width=width, height=height, steps=steps,
        )
    else:
        raise ValueError(
            f"unknown image_provider: {provider!r} "
            "(choices: cloudrun_flux2_klein, cloudrun_z_image_turbo, "
            "azure_flux2_klein, azure_z_image_turbo). Local providers "
            "were removed 2026-05-09; see pipeline/images.py."
        )

    dt = _time.time() - t0
    per_step = (dt / steps) if steps > 0 else dt
    print(
        f"[image-time] provider={provider} {width}x{height} steps={steps} "
        f"prompt_len={len(final_prompt)} wall={dt:.1f}s "
        f"per_step={per_step:.1f}s"
    )
    return result
# --- Prompts file ------------------------------------------------------


def beat_to_prompt(beat_text: str) -> str:
    """Heuristic fallback for v0; real prompts go in prompts.json.

    The 'a single character' prefix was dropped 2026-05-07 after the
    pompeii-79 first-light render: when cast_router classified ash /
    wind / mountain / city beats as object-only, this heuristic STILL
    forced a Roman figure into the frame because the prefix was applied
    upstream of the cast-router branch. Bare beat text + the channel's
    style_prefix + per-slug era_lock now carry the visual direction;
    cast.character_description is left to the cast-router to decide
    whether to inject (per-character beats) or drop (object-only).
    """
    return beat_text.strip().rstrip('.,!?;:').lower()


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

