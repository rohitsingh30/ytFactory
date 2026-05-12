"""Unit tests for ``pipeline.render.spec.build_spec`` — the Slice-1
foundation of the unified renderer.

These tests pin the inference rules so future changes to the spec
builder don't silently regress the resolved (kind, aspect, visual_mode,
…) for any (channel, niche, form-input) combo.

The cases here mirror the live combos we've seen in the wild — including
the original bug-report combo (``mystoriesanimated``, ``length_s=1800``,
phantom niche) — so a regression that would re-introduce silent 9:16
on a long-form request blows up here, not in production.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.render.spec import (
    AudioMode,
    CaptionsDensity,
    RenderKind,
    VisualMode,
    build_spec,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Long-form on a Shorts-only channel + phantom niche
# ---------------------------------------------------------------------------


def test_long_form_on_shorts_only_channel_with_phantom_niche():
    """The original 2026-05-12 bug. Form picked length_s=1800 +
    format=unresolved_mysteries (niche YAML doesn't exist). Pre-fix
    the worker silently produced a 30-min 9:16 Short. Post-fix the
    spec resolves to (long_form, 16:9, longform_panels) and surfaces
    the phantom-niche fallback in spec.notes."""
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "unresolved_mysteries",
            "length_s": 1800,
            "channel_overrides": {
                "voice": "lv-alex-foster",
                "visibility": "unlisted",
                "audio_mode": "voice",
                "music_bed": "ambient_low",
            },
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/unresolved_mysteries.yaml",
    )

    assert spec.kind == RenderKind.LONG_FORM
    assert spec.aspect_ratio == "16:9"
    assert spec.visual_mode == VisualMode.LONGFORM_PANELS
    assert spec.output_resolution == (1920, 1080)
    assert spec.duration_target_s == 1800
    # Phantom niche surfaced — never silent.
    assert any("experimental niche" in n for n in spec.notes)
    assert spec.source_variant_yaml is None  # no overlay file existed


# ---------------------------------------------------------------------------
# Standard Shorts request — should resolve cleanly to ai_beat_slideshow
# ---------------------------------------------------------------------------


def test_standard_aita_animated_short():
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 55,
            "channel_overrides": {},
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )

    assert spec.kind == RenderKind.SHORT
    assert spec.aspect_ratio == "9:16"
    assert spec.visual_mode == VisualMode.AI_BEAT_SLIDESHOW
    assert spec.output_resolution == (1080, 1920)
    assert spec.notes == []  # no warnings — clean path


# ---------------------------------------------------------------------------
# Long-form on a channel that DOES have a long_form: block (historyrecapped)
# ---------------------------------------------------------------------------


def test_long_form_on_channel_with_long_form_block():
    spec = build_spec(
        {
            "channel": "historyrecapped",
            "format": "",
            "length_s": 3600,
            "channel_overrides": {},
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/historyrecapped.yaml",
        variant_yaml_path=None,
    )

    assert spec.kind == RenderKind.LONG_FORM
    # historyrecapped's long_form: block declares output_resolution
    # [1920, 1080]; the spec MUST pick that up rather than the
    # channel-level Shorts default.
    assert spec.aspect_ratio == "16:9"
    assert spec.output_resolution == (1920, 1080)
    # historyrecapped is footage-bearing — long-form default visual mode
    # is archival_shotlist.
    assert spec.visual_mode == VisualMode.ARCHIVAL_SHOTLIST


# ---------------------------------------------------------------------------
# Form-override fields not silently dropped — unknown keys land in extra
# ---------------------------------------------------------------------------


def test_unknown_form_override_lands_in_extra():
    """Pre-2026-05-12 unknown form fields were silently ignored. The
    spec preserves them in ``spec.extra`` so downstream stages can
    still find them (or surface a clear "no stage consumes <key>"
    error if nothing does)."""
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 55,
            "channel_overrides": {
                "experiment_xyz": "try-this",
                "another_knob": 42,
            },
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )

    assert spec.extra == {"experiment_xyz": "try-this", "another_knob": 42}


# ---------------------------------------------------------------------------
# Sports — long_form on a channel without a long_form: block today
# ---------------------------------------------------------------------------


def test_sports_long_form_inference():
    """sportsrecapped doesn't have a long_form: block yet (Slice 2
    lands it). The spec still resolves to long_form/16:9/archival_shotlist
    via inferred defaults — so the worker can REJECT this with a clear
    error instead of silently rendering 9:16."""
    spec = build_spec(
        {
            "channel": "sportsrecapped",
            "format": "",
            "length_s": 600,
            "channel_overrides": {},
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/sportsrecapped.yaml",
        variant_yaml_path=None,
    )

    assert spec.kind == RenderKind.LONG_FORM
    assert spec.aspect_ratio == "16:9"
    assert spec.visual_mode == VisualMode.ARCHIVAL_SHOTLIST


# ---------------------------------------------------------------------------
# Form-driven length_kind override beats length_s threshold
# ---------------------------------------------------------------------------


def test_explicit_length_kind_short_overrides_long_length_s():
    """User explicitly picks length_kind=short with a length_s that
    would otherwise infer long. The explicit pick wins."""
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 200,  # >120 default-infers to long
            "channel_overrides": {"length_kind": "short"},
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )

    assert spec.kind == RenderKind.SHORT
    assert spec.aspect_ratio == "9:16"


# ---------------------------------------------------------------------------
# audio_mode=song threads through
# ---------------------------------------------------------------------------


def test_audio_mode_song_with_song_style():
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 55,
            "channel_overrides": {
                "audio_mode": "song",
                "song_style": "uplifting cinematic pop, female lead",
                "song_vocal_gender": "f",
                "song_model": "V4_5",
            },
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )

    assert spec.audio_mode == AudioMode.SONG
    assert spec.song_style == "uplifting cinematic pop, female lead"
    assert spec.song_vocal_gender == "f"
    assert spec.song_model == "V4_5"


# ---------------------------------------------------------------------------
# Resolution explicit override beats kind-inferred default
# ---------------------------------------------------------------------------


def test_explicit_resolution_override_wins():
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 55,
            "channel_overrides": {
                "output_resolution": [720, 1280],
                "aspect_ratio": "9:16",
            },
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )

    assert spec.output_resolution == (720, 1280)
    assert spec.aspect_ratio == "9:16"


# ---------------------------------------------------------------------------
# Missing channel YAML → spec still builds, with a note
# ---------------------------------------------------------------------------


def test_missing_channel_yaml_does_not_raise():
    spec = build_spec(
        {
            "channel": "no_such_channel",
            "format": "",
            "length_s": 55,
            "channel_overrides": {},
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/no_such_channel.yaml",
        variant_yaml_path=None,
    )
    # Defaults fired — short, 9:16.
    assert spec.kind == RenderKind.SHORT
    assert spec.aspect_ratio == "9:16"
    assert spec.visual_mode == VisualMode.AI_BEAT_SLIDESHOW
    # Note surfaced.
    assert any("channel YAML not found" in n for n in spec.notes)


# ---------------------------------------------------------------------------
# Regression: Short spec must not leak ``duration_max_s`` from the
# channel's ``long_form:`` block. Caught 2026-05-12 in prod when a 55-s
# Shorts test render hit the worker gate with duration_max_s=1800 and
# was rejected with "kind=short, duration_max_s=1800" — wrong, the long-
# form block had set the global cap.
# ---------------------------------------------------------------------------


def test_short_does_not_inherit_long_form_duration_cap():
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 55,
            "channel_overrides": {},
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )
    assert spec.kind == RenderKind.SHORT
    # The mystoriesanimated long_form: block has duration_max_s: 1800.
    # That MUST NOT leak into the SHORT spec — it should equal length_s
    # (55) or, at most, the channel-level shorts duration_max_s (which
    # mystoriesanimated.yaml doesn't set, so length_s wins).
    assert spec.duration_max_s == 55, (
        f"Short inherited long-form duration cap: got {spec.duration_max_s}"
    )


def test_to_dict_serialisation():
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 55,
            "channel_overrides": {"audio_mode": "voice"},
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )
    d = spec.to_dict()
    # Enums become bare strings.
    assert d["kind"] == "short"
    assert d["visual_mode"] == "ai_beat_slideshow"
    assert d["audio_mode"] == "voice"
    assert d["captions_density"] == "standard"
    # Tuple becomes list (Firestore-friendly).
    assert isinstance(d["output_resolution"], list)
    assert d["output_resolution"] == [1080, 1920]
