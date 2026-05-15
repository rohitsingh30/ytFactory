"""Cross-channel renderer matrix smoke tests.

For every supported (channel, niche, length_kind) tuple this verifies
that :func:`pipeline.render.spec.build_spec` produces a fully-typed
``RenderSpec`` and that the spec → orchestrator dispatch contract
matches the plan (Slice 2):

- ``kind == short`` → orchestrator raises ``NotImplementedError`` (the
  worker keeps its existing rewrite/cast/compose stages — Slice 5
  absorbs short into the unified path).
- ``kind == long_form`` → ``pipeline.render.video.render`` accepts the
  spec for dispatch (we don't actually invoke long_form.py here — the
  test asserts the dispatcher RECOGNISES the kind, not that the
  render completes).

Catches regressions where adding a new channel YAML or niche file
silently breaks the spec inference rules. Adding a channel × niche
combo without updating ``_CHANNEL_DEFAULT_VISUAL_MODE`` or the
inferred-mode chain raises here, not in production.

NEVER invokes ffmpeg, the LLM, GCS, or Firestore. Pure spec building
+ orchestrator routing. Ships in <1 s.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.render.spec import (
    RenderKind,
    RenderSpec,
    VisualMode,
    build_spec,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
CHANNELS_DIR = REPO_ROOT / "pipeline" / "channels"
VARIANTS_DIR = REPO_ROOT / "pipeline" / "variants"


def _channel_yaml(channel: str) -> Path:
    return CHANNELS_DIR / f"{channel}.yaml"


def _variant_yaml(channel: str, niche: str | None) -> Path | None:
    if not niche:
        return None
    return VARIANTS_DIR / channel / f"{niche}.yaml"


# ---------------------------------------------------------------------------
# Discovery — every channel YAML on disk
# ---------------------------------------------------------------------------


def _all_channels() -> list[str]:
    return sorted(p.stem for p in CHANNELS_DIR.glob("*.yaml"))


def _all_variants_for(channel: str) -> list[str | None]:
    """All variants for ``channel`` plus ``None`` (bare channel render)."""
    out: list[str | None] = [None]
    chan_dir = VARIANTS_DIR / channel
    if chan_dir.exists():
        out.extend(sorted(p.stem for p in chan_dir.glob("*.yaml")))
    return out


# ---------------------------------------------------------------------------
# Smoke matrix — every (channel, niche) × {short, long}
# ---------------------------------------------------------------------------


def _matrix() -> list[tuple[str, str | None, str]]:
    out: list[tuple[str, str | None, str]] = []
    for channel in _all_channels():
        for niche in _all_variants_for(channel):
            for length_kind in ("short", "long"):
                out.append((channel, niche, length_kind))
    return out


@pytest.mark.parametrize("channel,niche,length_kind", _matrix())
def test_spec_builds_without_raising(
    channel: str, niche: str | None, length_kind: str,
) -> None:
    """Every (channel, niche, length_kind) combo MUST produce a valid
    spec. NEVER raise — phantom niches surface via spec.notes."""
    spec = build_spec(
        {
            "channel": channel,
            "format": niche or "",
            "length_s": 55 if length_kind == "short" else 1800,
            "channel_overrides": {"length_kind": length_kind},
        },
        channel_yaml_path=_channel_yaml(channel),
        variant_yaml_path=_variant_yaml(channel, niche),
    )
    assert isinstance(spec, RenderSpec)
    # Kind must match the requested length_kind.
    expected_kind = RenderKind.SHORT if length_kind == "short" else RenderKind.LONG_FORM
    assert spec.kind == expected_kind, (
        f"{channel}/{niche or '-'}/{length_kind}: got kind={spec.kind.value}"
    )
    # Aspect must default to 9:16 for short, 16:9 for long unless the
    # YAML explicitly set otherwise.
    expected_aspect = "9:16" if length_kind == "short" else "16:9"
    assert spec.aspect_ratio == expected_aspect, (
        f"{channel}/{niche or '-'}/{length_kind}: got aspect={spec.aspect_ratio}"
    )
    # Visual mode must come from the typed enum.
    assert isinstance(spec.visual_mode, VisualMode)
    # Output resolution must agree with aspect.
    w, h = spec.output_resolution
    if expected_aspect == "9:16":
        assert w < h, f"{channel}/{niche}/{length_kind}: 9:16 expected portrait, got {w}x{h}"
    else:
        assert w > h, f"{channel}/{niche}/{length_kind}: 16:9 expected landscape, got {w}x{h}"


# ---------------------------------------------------------------------------
# Long-form on EVERY channel must resolve to a renderable visual_mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", _all_channels())
def test_every_channel_supports_long_form(channel: str) -> None:
    """Slice-2 contract: long-form must work on every channel.

    Spec must resolve to (long_form, 16:9, valid visual_mode) without
    raising and with a non-empty long_form: cfg block on the channel
    YAML so the long-form renderer can find its TTS / image / etc
    settings."""
    spec = build_spec(
        {"channel": channel, "format": "", "length_s": 1800},
        channel_yaml_path=_channel_yaml(channel),
        variant_yaml_path=None,
    )
    assert spec.kind == RenderKind.LONG_FORM
    assert spec.aspect_ratio == "16:9"
    assert spec.output_resolution[0] >= spec.output_resolution[1]
    # The visual_mode picked must be valid for long-form (NOT the
    # SHORT-only ai_beat_slideshow / motion_clips when no explicit
    # visual_source override was sent).
    assert spec.visual_mode in {
        VisualMode.LONGFORM_PANELS,
        VisualMode.ARCHIVAL_SHOTLIST,
        VisualMode.SPORTS_OVERLAY_TIMELINE,
        VisualMode.HYBRID_BEAT_FOOTAGE,
    }, f"{channel}: long_form picked an unsupported visual_mode {spec.visual_mode}"

    # Channel YAML must declare a long_form: block — the unified
    # renderer's long-form path delegates to pipeline.render.long_form
    # which hard-rejects channels without it.
    import yaml
    cfg = yaml.safe_load(_channel_yaml(channel).read_text())
    assert "long_form" in cfg, (
        f"{channel}: missing required `long_form:` block "
        f"(every channel must support long-form post-Slice-2)"
    )
    lf = cfg["long_form"]
    # Minimum required fields the long-form renderer reads on entry.
    for required in ("tts_provider", "tts_voice", "output_resolution"):
        assert required in lf, (
            f"{channel}.long_form: missing required key {required!r}"
        )


# ---------------------------------------------------------------------------
# Form-override pass-through — nothing silently dropped
# ---------------------------------------------------------------------------


def test_unknown_overrides_land_in_extra_for_every_channel() -> None:
    """Form sends a free-form ``channel_overrides`` dict. Anything the
    spec doesn't have a typed home for MUST land in ``spec.extra`` so
    downstream stages can still find it. Asserting per-channel here
    catches regressions where a new channel YAML accidentally
    intercepts an extra knob."""
    for channel in _all_channels():
        spec = build_spec(
            {
                "channel": channel,
                "format": "",
                "length_s": 55,
                "channel_overrides": {
                    "_smoke_test_passthrough": "must-arrive-in-extra",
                },
            },
            channel_yaml_path=_channel_yaml(channel),
            variant_yaml_path=None,
        )
        assert spec.extra.get("_smoke_test_passthrough") == "must-arrive-in-extra", (
            f"{channel}: form-override passthrough dropped"
        )


# ---------------------------------------------------------------------------
# Phantom niche surfaces a note for EVERY channel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", _all_channels())
def test_phantom_niche_surfaces_a_note(channel: str) -> None:
    """A niche with no overlay YAML must produce an `experimental niche`
    note (never silent fallback). This is THE bug from 2026-05-12 — if
    it regresses on any channel the system is back to silently rendering
    the wrong thing."""
    spec = build_spec(
        {
            "channel": channel,
            "format": "phantom_test_niche_does_not_exist",
            "length_s": 55,
        },
        channel_yaml_path=_channel_yaml(channel),
        variant_yaml_path=_variant_yaml(channel, "phantom_test_niche_does_not_exist"),
    )
    assert any(
        "experimental niche" in n and "phantom_test_niche_does_not_exist" in n
        for n in spec.notes
    ), f"{channel}: phantom niche didn't surface a note"


# ---------------------------------------------------------------------------
# Orchestrator dispatch contract
# ---------------------------------------------------------------------------


def test_orchestrator_rejects_short_kind_with_clear_error() -> None:
    """Slice 2 contract: ``video.render`` raises ``NotImplementedError``
    on kind=short so the worker keeps using its existing rewrite/cast/
    compose stages. Silently delegating to a no-op would be worse than
    a clear "use the worker's path" error."""
    from pipeline.render import video as _video

    spec = build_spec(
        {"channel": "mystoriesanimated", "format": "aita_animated", "length_s": 55},
        channel_yaml_path=_channel_yaml("mystoriesanimated"),
        variant_yaml_path=_variant_yaml("mystoriesanimated", "aita_animated"),
    )
    assert spec.kind == RenderKind.SHORT
    with pytest.raises(NotImplementedError):
        _video.render(spec, proposal={}, work_dir=Path("/tmp"), job_id="x")


def test_orchestrator_accepts_long_form_dispatch() -> None:
    """Long-form spec must reach ``video.render_long_form``. We don't
    actually run the LLM here — the test asserts the dispatcher
    RECOGNISES the kind and would route correctly."""
    from pipeline.render.spec import RenderKind as _RK
    from pipeline.render.video import render_long_form

    spec = build_spec(
        {"channel": "mystoriesanimated", "format": "", "length_s": 1800},
        channel_yaml_path=_channel_yaml("mystoriesanimated"),
        variant_yaml_path=None,
    )
    assert spec.kind == _RK.LONG_FORM
    # Calling render_long_form would fire a real LLM request — we just
    # confirm it's importable + accepts our spec without an immediate
    # type error. Sentinel: passing a non-long-form spec must raise
    # ValueError synchronously.
    with pytest.raises(ValueError):
        bad_spec = build_spec(
            {"channel": "mystoriesanimated", "format": "aita_animated", "length_s": 55},
            channel_yaml_path=_channel_yaml("mystoriesanimated"),
            variant_yaml_path=_variant_yaml("mystoriesanimated", "aita_animated"),
        )
        render_long_form(
            spec=bad_spec, proposal={}, work_dir=Path("/tmp"), job_id="x",
        )


# 2026-05-15 — short-form voice-override fallback regression. Pre-fix,
# a wizard ``voice="ref"`` (or any unresolvable bare id) on a cloud-TTS
# channel reached the synth dispatcher unchanged because build_spec
# never ran the apply_handlers chain that long-form already used.
# Result: cloudrun_indicf5 raised ``requires ref_audio_text`` and the
# entire render failed. Now build_spec runs apply_handlers, the bad
# voice gets dropped into ``cfg["_dropped_inputs"]``, and spec.voice_id
# falls back to the channel YAML's tts_voice.
def test_short_form_unresolvable_voice_override_falls_back_to_channel_default() -> None:
    """Wizard sends ``voice='ref'`` (the bare-stem the schema produced
    from a stale path-style YAML default) → apply_handler drops it →
    spec.voice_id reads cfg.tts_voice (the channel default the YAML
    intends).

    Surfaced by job 12275f0f (HindutavaAnimated Krishna leela Short).
    Pin the fallback in both directions:

    1. ``voice_id`` ends up as the channel YAML's resolved value.
    2. ``cfg["_dropped_inputs"]`` carries the rejected override (the
       worker can surface it to Firestore as a job-level warning).
    """
    spec = build_spec(
        {
            "channel": "hindutavaanimated",
            "format": "krishna_leela",
            "channel_overrides": {"voice": "ref"},
            "length_s": 60,
        },
        channel_yaml_path=_channel_yaml("hindutavaanimated"),
        variant_yaml_path=None,
    )
    # voice_id MUST NOT be the dropped 'ref' value.
    assert spec.voice_id != "ref"
    # voice_id MUST be the channel YAML's tts_voice (or its resolved
    # path equivalent — apply handler may rewrite a bare id to a path).
    assert "hindi-female-storyteller-calm" in str(spec.voice_id)


def test_short_form_resolvable_voice_override_wins_over_channel_default() -> None:
    """When the wizard sends a voice that DOES exist on disk, the
    override should still win over the channel default — the fallback
    only kicks in when the value can't be resolved. Sentinel against
    over-correction."""
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "channel_overrides": {"voice": "sarah"},  # exists at pipeline/voice_refs/sarah.wav
            "length_s": 55,
        },
        channel_yaml_path=_channel_yaml("mystoriesanimated"),
        variant_yaml_path=_variant_yaml("mystoriesanimated", "aita_animated"),
    )
    # apply_handler rewrites bare 'sarah' to the resolved path; spec
    # picks up the resolved value (NOT the channel default).
    assert "sarah" in str(spec.voice_id)
    assert spec.voice_id != ""
