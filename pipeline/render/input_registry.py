"""Descriptor-driven input → spec → cfg → prompt registry.

Single source of truth for HOW each form input flows through the
pipeline. Adding (or removing) a form input is a single-file edit in
``pipeline/schemas/customization.py``: declare the ``CustomizationField``
with its ``cfg_targets`` / ``spec_field`` / ``apply_handler`` /
``prompt_patch_fn`` metadata, and every consumer below picks it up
automatically.

Pre-2026-05-12 this layer was a hand-coded if/elif chain in
``pipeline/render/shorts.py:_apply_form_overrides`` (962-1122) that:
1. Was **never called** from ``_make_short_impl`` — silently dead code.
2. Was completely missing on the long-form path — every form pick except
   ``length_s`` was dropped on long-form renders.
3. Couldn't be extended without editing 4-5 unrelated files.

The descriptor pattern fixes all three: one decl per input, exactly
one call into this module per consumer.

## Public API

- :func:`apply_overrides` — replaces the body of
  ``pipeline/render/shorts.py:_apply_form_overrides``. Walks the
  registered descriptors and writes form-supplied values into the
  channel cfg dict at every declared target path.

- :func:`long_form_overlay_from_spec` — used by
  ``pipeline/render/video.py::render_long_form`` to build a per-render
  YAML overlay deep-merged on top of the channel YAML by
  ``pipeline/render/long_form.py``'s new ``--config <path>`` flag.

- :func:`prompt_patches_for` — used by long-form / short rewriters to
  collect mode-changing prompt fragments contributed by descriptors
  with ``prompt_patch_fn`` set (e.g. ``audio_mode=song`` switches the
  rewriter to a lyrics output contract).

- :func:`register_transform` / :func:`register_apply_handler` /
  :func:`register_prompt_patch` — extension points for descriptors
  whose value handling is non-trivial. Keeps complex business logic
  (voice cloud-carve-out, suno style threading) as named, testable
  functions instead of being smuggled into descriptor lambdas.

The registry intentionally does NOT enumerate "which inputs exist" —
that's owned by ``pipeline/schemas/customization.py``. This module
just walks whatever ``CustomizationField`` instances are returned by
``schema_for_channel(channel_key)`` and applies their renderer-side
metadata.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Named-handler registries
#
# Descriptors reference handlers BY NAME (string) rather than carrying
# Python callables directly. This keeps:
#   - descriptor data JSON-serialisable (so the form schema endpoint
#     can return them without a custom encoder),
#   - business logic discoverable (one place to register / look up),
#   - testing focused (each handler is a unit-testable function).
#
# Adding a new handler is two steps:
#   1. Define the function below (or in a sibling module).
#   2. Register it via the @register_* decorator.
# Then any descriptor can reference it by name.
# ---------------------------------------------------------------------------


_TRANSFORMS: dict[str, Callable[[Any], Any]] = {}
_APPLY_HANDLERS: dict[str, Callable[[dict, Any, dict], None]] = {}
_PROMPT_PATCHES: dict[str, Callable[[Any, Any], "PromptPatch"]] = {}


def register_transform(name: str) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
    """Decorator: register a value-transform under ``name``."""
    def deco(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        _TRANSFORMS[name] = fn
        return fn
    return deco


def register_apply_handler(
    name: str,
) -> Callable[[Callable[[dict, Any, dict], None]], Callable[[dict, Any, dict], None]]:
    """Decorator: register a custom apply handler under ``name``.

    Handler signature: ``handler(cfg, value, channel_meta) -> None``.
    Mutates ``cfg`` in place. ``channel_meta`` is ``{channel, niche, …}``
    so handlers like the cloud-voice carve-out can branch on
    ``cfg["tts_provider"]``."""
    def deco(fn: Callable[[dict, Any, dict], None]) -> Callable[[dict, Any, dict], None]:
        _APPLY_HANDLERS[name] = fn
        return fn
    return deco


def register_prompt_patch(
    name: str,
) -> Callable[[Callable[[Any, Any], "PromptPatch"]], Callable[[Any, Any], "PromptPatch"]]:
    """Decorator: register a prompt-patch contributor under ``name``.

    Handler signature: ``patch(value, spec) -> PromptPatch``."""
    def deco(fn: Callable[[Any, Any], "PromptPatch"]) -> Callable[[Any, Any], "PromptPatch"]:
        _PROMPT_PATCHES[name] = fn
        return fn
    return deco


# ---------------------------------------------------------------------------
# Prompt patches — structured contributions to LLM prompts
# ---------------------------------------------------------------------------


@dataclass
class PromptPatch:
    """Structured prompt contribution from one form-input descriptor.

    Per the rubber-duck critique on the descriptor design (2026-05-12):
    additive sentence fragments are the easy case (just append to the
    prompt's "channel context" block); MODE CHANGES need to be
    surfaced explicitly so the rewriter can pick a different output
    contract / schema.

    Fields:
    - ``context_lines``: appended to the per-channel context block.
    - ``craft_rules``: appended to the craft-rules section.
    - ``schema_mode``: when set, signals the rewriter to switch its
      output contract (e.g. ``"lyrics"`` for ``audio_mode=song``).
      The rewriter is responsible for honouring this — descriptors
      can request a mode but not redefine the contract.
    - ``forbidden_phrases``: per-input bans the rewriter must avoid.
    """
    context_lines: list[str] = field(default_factory=list)
    craft_rules: list[str] = field(default_factory=list)
    schema_mode: str | None = None
    forbidden_phrases: list[str] = field(default_factory=list)


def merge_patches(patches: Iterable[PromptPatch]) -> PromptPatch:
    """Combine per-descriptor patches into one. Last-set ``schema_mode``
    wins — caller can detect conflicts by passing patches one at a time."""
    out = PromptPatch()
    for p in patches:
        out.context_lines.extend(p.context_lines)
        out.craft_rules.extend(p.craft_rules)
        out.forbidden_phrases.extend(p.forbidden_phrases)
        if p.schema_mode:
            out.schema_mode = p.schema_mode
    return out


# ---------------------------------------------------------------------------
# Built-in transforms (referenced by CustomizationField.cfg_targets[*].transform)
# ---------------------------------------------------------------------------


@register_transform("identity")
def _identity(v: Any) -> Any:
    return v


@register_transform("music_bed_to_filename")
def _music_bed_to_filename(v: Any) -> str:
    """``"ambient_low"`` → ``"ambient_low.mp3"``; ``"off"`` → ``""``.

    Pre-2026-05-12 this transform was inlined into the if/elif chain in
    ``_apply_form_overrides``; centralising it here keeps the form
    value (``"ambient_low"``) canonical on the spec while letting the
    cfg consumer (``cfg["music_bed_default"]``) get the filename it
    expects.
    """
    s = str(v or "").strip()
    if not s or s == "off":
        return ""
    if s.endswith(".mp3"):
        return s
    return f"{s}.mp3"


@register_transform("audio_mode_to_provider")
def _audio_mode_to_provider(v: Any) -> str:
    """``"song"`` → ``"sunoapi"``; ``"voice"`` → ``"tts"``."""
    return "sunoapi" if v == "song" else "tts"


# ---------------------------------------------------------------------------
# Built-in apply handlers (referenced by CustomizationField.apply_handler)
# ---------------------------------------------------------------------------


@register_apply_handler("apply_voice_with_cloud_carveout")
def _apply_voice(cfg: dict, value: Any, channel_meta: dict) -> None:
    """Voice override with the long-standing cloud-carve-out:

    - ``"/" in value`` (path-style ref WAV) → write to ``cfg["tts_voice"]``
      AND ``cfg["long_form"]["tts_voice"]``; provider untouched.
    - bare voice id on a cloud channel (``cloudrun_*``/``azure_*``) →
      try to resolve to a path (catalog / clones / web / kokoro);
      fall through to the laptop branch if found, else drop loudly.
    - bare voice id on a laptop channel → write voice id, flip provider
      to ``"kokoro"``, drop ``tts_ref_text``.

    Mirrors the carve-out previously hand-coded at
    ``pipeline/render/shorts.py:1044-1062``. Centralised here so a
    future TTS-provider change is one edit, not five.

    2026-05-13 — added bare-id → path resolution so wizard picks like
    ``lv-alex-foster`` (LibriVox web voice) actually flow through to
    the cloud worker. Pre-fix the carve-out silently dropped every
    bare id on cloud channels and Sarah default kicked in — every
    user voice pick on a cloud channel (which is all 16 channel
    YAMLs except ``tifu``) was a lie.
    """
    v = str(value or "").strip()
    if not v:
        return
    current_provider = str(cfg.get("tts_provider") or "")
    is_cloud = current_provider.startswith("cloudrun_") or current_provider.startswith("azure_")
    if "/" in v:
        cfg["tts_voice"] = v
        cfg.setdefault("long_form", {})["tts_voice"] = v
        return
    # Bare voice id: try to resolve to a path under pipeline/voice_refs/
    # Mirrors voices_routes._voice_path's candidate list so the worker
    # accepts every key the wizard catalog can return.
    resolved = _resolve_voice_id_to_path(v)
    if resolved is not None:
        # Treat as path-style ref — both providers (cloud + laptop kokoro)
        # accept a path argument; cloud TTS uses it as the speaker ref.
        cfg["tts_voice"] = resolved
        cfg.setdefault("long_form", {})["tts_voice"] = resolved
        return
    if is_cloud:
        msg = (
            f"voice={v!r} ignored — channel uses cloud provider "
            f"{current_provider!r}, no ref WAV found at any of "
            f"pipeline/voice_refs/{{{v}/ref.wav, clones/{v}/ref.wav, "
            f"web/{v}/ref.wav, {v}.wav, web/static/voice_samples/{v}.wav}} "
            f"— channel default kept"
        )
        _logger.warning("[input_registry] %s", msg)
        # Surface the dropped-input warning to the worker via cfg so the
        # entrypoint can publish it to Firestore as a job-level warning.
        cfg.setdefault("_dropped_inputs", []).append({
            "field": "voice", "value": v, "reason": msg,
        })
        return
    cfg["tts_voice"] = v
    cfg["tts_provider"] = "kokoro"
    cfg.pop("tts_ref_text", None)
    cfg.setdefault("long_form", {})["tts_voice"] = v


def _resolve_voice_id_to_path(voice_key: str) -> str | None:
    """Resolve a bare voice id to a relative path under the repo root.

    Mirrors ``control/routes/voices_routes.py::_voice_path`` so the
    worker accepts every key the wizard's voice catalog can return.
    Returns a *relative* path (string) so the same value works on
    both laptop and cloud worker (where the repo lives at
    ``/workspace/`` per the Dockerfile COPY).

    Order matches the route handler's lookup order. Returns ``None``
    if no candidate exists on disk.
    """
    import os  # noqa: PLC0415
    # Repo root: this file is pipeline/render/input_registry.py.
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    candidates = [
        f"pipeline/voice_refs/{voice_key}/ref.wav",
        f"pipeline/voice_refs/clones/{voice_key}/ref.wav",
        f"pipeline/voice_refs/web/{voice_key}/ref.wav",
        f"pipeline/voice_refs/{voice_key}.wav",
        f"web/static/voice_samples/{voice_key}.wav",
    ]
    for rel in candidates:
        if os.path.exists(os.path.join(root, rel)):
            return rel
    return None


@register_apply_handler("apply_song_style")
def _apply_song_style(cfg: dict, value: Any, channel_meta: dict) -> None:
    """Thread Suno style override into ``cfg["_suno_prompt_override"]``."""
    v = str(value or "").strip()
    if not v:
        return
    suno = dict(cfg.get("_suno_prompt_override") or {})
    suno["style"] = v
    cfg["_suno_prompt_override"] = suno


# ---------------------------------------------------------------------------
# Built-in prompt patches (referenced by CustomizationField.prompt_patch_fn)
# ---------------------------------------------------------------------------


@register_prompt_patch("audio_mode_branch")
def _audio_mode_patch(value: Any, spec: Any) -> PromptPatch:
    """When the user picks ``audio_mode=song``, signal the rewriter to
    output LYRICS instead of narration. Descriptor-driven so we don't
    smuggle song logic into the narration prompt template."""
    if value == "song":
        return PromptPatch(
            context_lines=[
                "Audio mode is SONG — the output will be sung by Suno, "
                "not read by a TTS narrator.",
            ],
            craft_rules=[
                "Author LYRICS (chorus + verse structure), not prose. "
                "Each line is a singable phrase, ≤ 8 words. Use repeated "
                "hooks. NO prose paragraphs. NO TTS-style sentence-as-image "
                "shape — image cuts ride the song's beat structure later.",
                "Hook = the chorus line, ≤ 6 words, repeated 3+ times.",
            ],
            schema_mode="lyrics",
        )
    return PromptPatch()


@register_prompt_patch("narrator_visual_branch")
def _narrator_visual_patch(value: Any, spec: Any) -> PromptPatch:
    """``narrator_visual_mode=voice_only`` keeps the narrator off-camera —
    the prompt-author + image-gen stages skip narrator-presence prompts."""
    if value == "voice_only":
        return PromptPatch(
            context_lines=[
                "Narrator visual mode is VOICE_ONLY — the narrator is "
                "never on screen. Author scenes that show the SUBJECTS "
                "of the story, not a generic narrator character.",
            ],
            craft_rules=[
                "Do NOT describe a narrator character anywhere. Every "
                "panel/scene must describe the actual people / objects / "
                "places in the story.",
            ],
        )
    return PromptPatch()


@register_prompt_patch("visual_source_branch")
def _visual_source_patch(value: Any, spec: Any) -> PromptPatch:
    """When ``visual_source=footage`` the narration must be footage-
    matchable — encourage named events, real people's specific actions
    so the footage matcher can find broadcast clips."""
    if value == "footage":
        return PromptPatch(
            context_lines=[
                "Visual source is FOOTAGE — every section will be matched "
                "to an archive clip (broadcast / archive.org / Wikimedia). "
                "Do NOT author scenes that need AI illustration.",
            ],
            craft_rules=[
                "Each section narration MUST anchor on a specific event "
                "the footage matcher can search for: a named person + "
                "specific action + named place + (where applicable) a "
                "year or date. Generic scenes ('the city was tense') are "
                "footage-unmatchable and will leave gaps in the timeline.",
            ],
        )
    if value == "both":
        return PromptPatch(
            context_lines=[
                "Visual source is HYBRID (footage + AI fallback). Author "
                "narration assuming SOME beats will get archival footage "
                "and the rest will be AI-generated panels. Lean toward "
                "footage-matchable concrete events where possible.",
            ],
        )
    return PromptPatch()


# ---------------------------------------------------------------------------
# Public API — apply / overlay / patches
# ---------------------------------------------------------------------------


def apply_overrides(
    cfg: dict,
    overrides: dict,
    *,
    channel_key: str | None = None,
    descriptors: Iterable[Any] | None = None,
) -> None:
    """Walk the descriptor registry and write each form override into
    the matching cfg locations.

    Replaces the dead ``pipeline/render/shorts.py:_apply_form_overrides``
    body with a descriptor-driven loop. Adding a new form input is a
    single ``CustomizationField(...)`` declaration — this function picks
    it up without any code change here.

    Args:
        cfg: channel config dict to mutate in-place.
        overrides: form-supplied ``{key: value}`` mapping.
        channel_key: the channel slug — used to look up the per-channel
            descriptor list. Required when ``descriptors`` isn't passed.
        descriptors: optional explicit list (skips the per-channel
            schema lookup). Used by tests + for future channel-agnostic
            applies.

    NEVER raises. A handler that errors out logs + returns; cfg may be
    partially mutated when one handler succeeds before another fails.
    """
    if descriptors is None:
        descriptors = _resolve_descriptors(channel_key)

    channel_meta = {"channel": channel_key}

    for desc in descriptors:
        # Use Pydantic .key etc; gracefully handle dict-shaped tests.
        key = _attr(desc, "key")
        if not key or key not in overrides:
            continue
        value = overrides.get(key)
        if value in (None, ""):
            continue

        # Custom apply handler wins over cfg_targets list.
        apply_handler = _attr(desc, "apply_handler")
        if apply_handler:
            handler = _APPLY_HANDLERS.get(apply_handler)
            if handler is None:
                _logger.warning(
                    "[input_registry] descriptor %r references unknown "
                    "apply_handler %r — skipping", key, apply_handler,
                )
                continue
            try:
                handler(cfg, value, channel_meta)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "[input_registry] apply_handler %r failed for %r: %s",
                    apply_handler, key, exc,
                )
            continue

        # Otherwise walk the cfg_targets list.
        targets = _attr(desc, "cfg_targets") or ()
        for tgt in targets:
            path = _attr(tgt, "path") or ()
            if not path:
                continue

            t_value = value
            transform_name = _attr(tgt, "transform")
            if transform_name:
                tfn = _TRANSFORMS.get(transform_name)
                if tfn is None:
                    _logger.warning(
                        "[input_registry] descriptor %r references "
                        "unknown transform %r — skipping target %s",
                        key, transform_name, ".".join(path),
                    )
                    continue
                try:
                    t_value = tfn(t_value)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "[input_registry] transform %r raised on %r: %s",
                        transform_name, key, exc,
                    )
                    continue

            condition_name = _attr(tgt, "condition")
            if condition_name:
                cfn = _APPLY_HANDLERS.get(condition_name) \
                    or _TRANSFORMS.get(condition_name)
                # Conditions are stored alongside handlers/transforms;
                # both registries support arbitrary callables. The
                # condition signature is `(value, cfg) -> bool`.
                if cfn is None:
                    _logger.warning(
                        "[input_registry] unknown condition %r for %r — "
                        "skipping target %s", condition_name, key,
                        ".".join(path),
                    )
                    continue
                try:
                    if not cfn(t_value, cfg):
                        continue
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "[input_registry] condition %r raised: %s",
                        condition_name, exc,
                    )
                    continue

            _set_path(cfg, list(path), t_value)


def long_form_overlay_from_spec(spec: Any) -> dict:
    """Build a per-render YAML overlay dict from a typed RenderSpec.

    Walks the descriptors and projects each ``spec_field`` value into
    the cfg target path the descriptor declares. Used by
    ``pipeline/render/video.py::render_long_form`` to write
    ``work_dir/long_form_overlay.yaml`` which long_form.py picks up via
    its new ``--config <path>`` flag.

    Returns an empty dict when no descriptors mirror to spec fields,
    which is harmless (long_form.py treats empty overlay as "use
    channel YAML alone").

    2026-05-13 — also dispatches ``apply_handler`` if a descriptor
    declares one. Pre-fix, descriptors with NO ``cfg_targets`` (the
    voice carve-out is the prime example — it uses
    ``apply_handler="apply_voice_with_cloud_carveout"`` because the
    cloud-vs-laptop logic doesn't fit the simple path-projection
    shape) were silently dropped from the long-form overlay. So
    every wizard voice pick on a long-form render was lost — the
    channel YAML default kicked in, regardless of what the user
    chose. Same bug class can hit any future apply_handler-driven
    descriptor (song style threading, narrator visual mode, …).

    The fix walks descriptors twice: once for ``cfg_targets`` (the
    pre-existing path), once for ``apply_handler`` against the same
    overlay dict. The handler signature is ``(cfg, value,
    channel_meta)`` — the overlay IS the cfg here, and channel_meta
    is taken from the channel YAML's top-level keys (provider, etc.)
    so the cloud-carve-out can branch on ``tts_provider``.
    """
    if spec is None:
        return {}

    channel_key = _attr(spec, "channel")
    descriptors = _resolve_descriptors(channel_key)
    channel_meta = _channel_meta_for_overlay(channel_key)

    overlay: dict = dict(channel_meta)  # seed with provider/etc so handlers can branch
    seeded_keys = set(channel_meta.keys())

    for desc in descriptors:
        spec_field = _attr(desc, "spec_field")
        if not spec_field:
            continue
        spec_val = _attr(spec, spec_field)
        if spec_val is None:
            continue
        # Skip default-equivalent values to keep the overlay sparse.
        if isinstance(spec_val, str) and not spec_val.strip():
            continue
        if hasattr(spec_val, "value"):  # Enum-shaped — emit the str value.
            spec_val = spec_val.value

        # Path 1 (pre-existing): walk cfg_targets if present.
        targets = _attr(desc, "cfg_targets") or ()
        for tgt in targets:
            path = _attr(tgt, "path") or ()
            if not path:
                continue
            t_value = spec_val
            transform_name = _attr(tgt, "transform")
            if transform_name:
                tfn = _TRANSFORMS.get(transform_name)
                if tfn is None:
                    continue
                try:
                    t_value = tfn(t_value)
                except Exception:  # noqa: BLE001
                    continue
            _set_path(overlay, list(path), t_value)

        # Path 2 (2026-05-13): dispatch apply_handler if present.
        # This is what makes the wizard's voice pick survive into
        # long-form renders. Without it, _apply_voice never runs on
        # the long-form path and the channel default kicks in.
        apply_handler = _attr(desc, "apply_handler")
        if apply_handler:
            handler = _APPLY_HANDLERS.get(apply_handler)
            if handler is None:
                _logger.warning(
                    "[input_registry] descriptor %r references unknown "
                    "apply_handler %r — long-form overlay will not honour "
                    "this input", _attr(desc, "key"), apply_handler,
                )
                continue
            try:
                handler(overlay, spec_val, channel_meta)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "[input_registry] apply_handler %r raised in long-form "
                    "overlay for %r: %s — input dropped",
                    apply_handler, _attr(desc, "key"), exc,
                )

    # Strip the seed channel_meta keys that the handler didn't touch,
    # so the overlay stays minimal (only what the user picked, not a
    # mirror of the channel YAML).
    for k in list(overlay.keys()):
        if k in seeded_keys and overlay[k] == channel_meta[k]:
            del overlay[k]

    return overlay


def _channel_meta_for_overlay(channel_key: str | None) -> dict:
    """Read the channel YAML's top-level provider keys so the
    apply_handlers can branch on ``tts_provider`` etc.

    Returns an empty dict if the channel YAML can't be loaded — the
    handler's fallback paths must tolerate missing context.
    """
    if not channel_key:
        return {}
    try:
        import os  # noqa: PLC0415
        import yaml  # noqa: PLC0415
        # Resolve relative to the repo root (this file is pipeline/render/input_registry.py).
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        path = os.path.join(root, "pipeline", "channels", f"{channel_key}.yaml")
        if not os.path.exists(path):
            return {}
        with open(path) as fp:
            data = yaml.safe_load(fp) or {}
        # Only carry the provider keys the handlers branch on. Don't seed
        # the entire YAML or the overlay sparseness logic breaks.
        return {
            k: data[k] for k in ("tts_provider", "image_provider", "asr_provider")
            if k in data and isinstance(data[k], str)
        }
    except Exception:  # noqa: BLE001
        return {}


def prompt_patches_for(
    spec: Any,
    *,
    channel_key: str | None = None,
) -> PromptPatch:
    """Collect every prompt-patch contribution that applies to ``spec``.

    Walks the descriptor registry, calls each descriptor's
    ``prompt_patch_fn`` with the spec's matching value, and merges
    the results. Used by long-form / short rewriters to inject form-
    driven prompt fragments without smuggling per-input branches into
    the prompt template.

    Returns an empty :class:`PromptPatch` when no descriptors apply.
    """
    descriptors = _resolve_descriptors(channel_key or _attr(spec, "channel"))
    patches: list[PromptPatch] = []
    for desc in descriptors:
        patch_name = _attr(desc, "prompt_patch_fn")
        if not patch_name:
            continue
        spec_field = _attr(desc, "spec_field")
        value = _attr(spec, spec_field) if spec_field else None
        if value is None:
            continue
        if hasattr(value, "value"):
            value = value.value
        fn = _PROMPT_PATCHES.get(patch_name)
        if fn is None:
            _logger.warning(
                "[input_registry] descriptor %r references unknown "
                "prompt_patch_fn %r — skipping", _attr(desc, "key"), patch_name,
            )
            continue
        try:
            patches.append(fn(value, spec))
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "[input_registry] prompt_patch %r raised on %r: %s",
                patch_name, _attr(desc, "key"), exc,
            )
    return merge_patches(patches)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str) -> Any:
    """Read a field off a Pydantic model, dataclass, or dict — uniformly."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _set_path(target: dict, path: list[str], value: Any) -> None:
    """Walk ``path`` into ``target`` (creating sub-dicts as needed) and
    set the final key to ``value``."""
    cur = target
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def _resolve_descriptors(channel_key: str | None) -> list[Any]:
    """Pull the per-channel ``CustomizationField`` list from
    ``pipeline.schemas.customization``. Caches the lookup per call site
    via the lazy import — repeated calls within one render don't pay
    the YAML-load cost more than once because customization.py itself
    caches its CHANNEL_REGISTRY at module load.
    """
    try:
        from pipeline.schemas.customization import (  # noqa: PLC0415
            get_customization_schema,
        )
    except ImportError:
        return []
    if not channel_key:
        return []
    try:
        schema = get_customization_schema(channel_key)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "[input_registry] get_customization_schema(%r) failed: %s",
            channel_key, exc,
        )
        return []
    if schema is None:
        return []
    return list(schema.fields or [])


__all__ = [
    "PromptPatch",
    "apply_overrides",
    "long_form_overlay_from_spec",
    "prompt_patches_for",
    "merge_patches",
    "register_transform",
    "register_apply_handler",
    "register_prompt_patch",
]
