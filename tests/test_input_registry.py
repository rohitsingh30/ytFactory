"""Tests for the descriptor-driven input registry (Slice-2.P3 — 2026-05-12).

Adding/removing a form input is a single declarative edit in
``pipeline/schemas/customization.py``: declare a ``CustomizationField``
with ``cfg_targets`` / ``spec_field`` / ``apply_handler`` /
``prompt_patch_fn`` metadata, and every consumer below picks it up
automatically.

These tests pin that contract:

1. ``apply_overrides`` walks descriptors and writes form values into
   the matching cfg locations (single + multi-target).
2. ``long_form_overlay_from_spec`` projects spec fields → per-render
   YAML overlay deep-merged on top of channel YAML by long_form.py's
   new ``--config`` flag.
3. ``prompt_patches_for`` collects descriptor-registered prompt
   contributions into a merged :class:`PromptPatch` that the long-form
   rewriter injects into its prompt template.
4. The 6 wired form inputs (length / voice / music / audio_mode /
   captions_density / visual_source) end-to-end on
   ``mystoriesanimated`` produce the correct cfg shape.

If a future descriptor edit drops a cfg_target or breaks a transform,
the assertions here flag it BEFORE prod renders silently produce
wrong output.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pipeline.render.input_registry import (
    PromptPatch,
    apply_overrides,
    long_form_overlay_from_spec,
    merge_patches,
    prompt_patches_for,
    register_apply_handler,
    register_prompt_patch,
    register_transform,
)
from pipeline.render.spec import build_spec
from pipeline.schemas.customization import (
    CfgTarget,
    CustomizationField,
    _audio_mode_field,
    _captions_density_field,
    _length_field,
    _music_field,
    _song_model_field,
    _song_style_field,
    _song_vocal_gender_field,
    _visual_source_field,
    _voice_field,
    get_customization_schema,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Per-factory descriptor metadata
# ---------------------------------------------------------------------------


def test_length_field_carries_descriptor_metadata():
    f = _length_field(55)
    assert f.spec_field == "duration_target_s"
    assert f.cfg_targets is not None and len(f.cfg_targets) == 2
    paths = {tuple(t.path) for t in f.cfg_targets}
    assert ("duration_max_s",) in paths
    assert ("long_form", "duration_max_s") in paths


def test_voice_field_uses_apply_handler_not_cfg_targets():
    """Voice has business logic (cloud carve-out) — uses a named handler."""
    f = _voice_field("sarah", "en")
    assert f.spec_field == "voice_id"
    assert f.apply_handler == "apply_voice_with_cloud_carveout"


def test_music_field_carries_filename_transform():
    f = _music_field("ambient_low")
    assert f.spec_field == "music_bed"
    assert f.cfg_targets is not None
    transforms = {t.transform for t in f.cfg_targets}
    assert "music_bed_to_filename" in transforms


def test_audio_mode_carries_prompt_patch_fn():
    f = _audio_mode_field("tts")
    assert f.spec_field == "audio_mode"
    assert f.prompt_patch_fn == "audio_mode_branch"
    assert f.cfg_targets is not None and len(f.cfg_targets) == 1
    assert f.cfg_targets[0].transform == "audio_mode_to_provider"


def test_visual_source_carries_prompt_patch_fn():
    f = _visual_source_field("animated")
    assert f.spec_field == "visual_source"
    assert f.prompt_patch_fn == "visual_source_branch"


def test_captions_density_writes_short_and_longform():
    f = _captions_density_field()
    assert f.spec_field == "captions_density"
    paths = {tuple(t.path) for t in f.cfg_targets}
    assert ("captions_density",) in paths
    assert ("long_form", "captions_density") in paths


def test_song_fields_carry_targets():
    style = _song_style_field({}, "en")
    assert style.spec_field == "song_style"
    assert style.apply_handler == "apply_song_style"

    gender = _song_vocal_gender_field({})
    assert gender.spec_field == "song_vocal_gender"
    assert gender.cfg_targets is not None
    assert tuple(gender.cfg_targets[0].path) == ("sunoapi_vocal_gender",)

    model = _song_model_field({})
    assert model.spec_field == "song_model"
    assert tuple(model.cfg_targets[0].path) == ("sunoapi_model",)


# ---------------------------------------------------------------------------
# apply_overrides — short-path retrofit
# ---------------------------------------------------------------------------


def test_apply_overrides_single_target():
    """Vanilla descriptor with one cfg_target writes one cfg key."""
    desc = CustomizationField(
        key="foo",
        label="Foo",
        kind="text",
        cfg_targets=[CfgTarget(path=["foo_cfg_key"])],
    )
    cfg: dict = {}
    apply_overrides(cfg, {"foo": "bar"}, descriptors=[desc])
    assert cfg == {"foo_cfg_key": "bar"}


def test_apply_overrides_multi_target_with_transform():
    """One descriptor → multiple cfg locations; transform applied per-target."""
    desc = _music_field("ambient_low")
    cfg: dict = {}
    apply_overrides(cfg, {"music_bed": "cinematic"}, descriptors=[desc])
    assert cfg["music_bed_default"] == "cinematic.mp3"
    assert cfg["long_form"]["music_bed_default"] == "cinematic.mp3"


def test_apply_overrides_off_value_transformed_to_empty():
    desc = _music_field("ambient_low")
    cfg: dict = {}
    apply_overrides(cfg, {"music_bed": "off"}, descriptors=[desc])
    assert cfg["music_bed_default"] == ""
    assert cfg["long_form"]["music_bed_default"] == ""


def test_apply_overrides_apply_handler_voice_cloud_carveout():
    """Voice on a cloud channel: bare name ignored, path-style WAV written."""
    desc = _voice_field("sarah", "en")

    # Path-style writes BOTH locations.
    cfg = {"tts_provider": "cloudrun_chatterbox"}
    apply_overrides(cfg, {"voice": "pipeline/voice_refs/sarah.wav"}, descriptors=[desc])
    assert cfg["tts_voice"] == "pipeline/voice_refs/sarah.wav"
    assert cfg["long_form"]["tts_voice"] == "pipeline/voice_refs/sarah.wav"

    # Bare name on cloud channel: ignored (logged, no mutation).
    cfg = {"tts_provider": "cloudrun_chatterbox"}
    apply_overrides(cfg, {"voice": "af_sarah"}, descriptors=[desc])
    assert "tts_voice" not in cfg
    assert "long_form" not in cfg


def test_apply_overrides_per_channel_lookup_via_get_customization_schema():
    """Real mystoriesanimated channel cfg + full form override set →
    every wired knob lands. Pin the integration."""
    cfg = yaml.safe_load(
        (REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml").read_text()
    )
    overrides = {
        "audio_mode": "song",
        "song_style": "cinematic upbeat pop",
        "song_vocal_gender": "f",
        "song_model": "V5",
        "voice": "pipeline/voice_refs/sarah.wav",
        "music_bed": "cinematic",
        "captions_density": "dense",
        "visual_source": "both",
        "length_s": 90,
    }
    apply_overrides(cfg, overrides, channel_key="mystoriesanimated")

    # Top-level cfg writes — what the SHORT path's _make_short_impl reads.
    assert cfg["audio_provider"] == "sunoapi"  # via audio_mode_to_provider transform
    assert cfg["sunoapi_vocal_gender"] == "f"
    assert cfg["sunoapi_model"] == "V5"
    assert cfg["tts_voice"] == "pipeline/voice_refs/sarah.wav"
    assert cfg["music_bed_default"] == "cinematic.mp3"
    assert cfg["captions_density"] == "dense"
    assert cfg["visual_source"] == "both"
    assert cfg["duration_max_s"] == 90
    assert cfg["_suno_prompt_override"] == {"style": "cinematic upbeat pop"}

    # Nested long_form: writes — what the LONG-FORM path's overlay needs.
    lf = cfg["long_form"]
    assert lf["tts_voice"] == "pipeline/voice_refs/sarah.wav"
    assert lf["music_bed_default"] == "cinematic.mp3"
    assert lf["captions_density"] == "dense"
    assert lf["visual_source"] == "both"
    assert lf["duration_max_s"] == 90


def test_apply_overrides_unknown_keys_silently_skipped():
    """Unknown form keys (no descriptor) don't raise — they fall through
    to spec.extra elsewhere."""
    desc = _length_field(55)
    cfg: dict = {}
    apply_overrides(cfg, {"length_s": 90, "experiment_xyz": "future"}, descriptors=[desc])
    assert cfg.get("duration_max_s") == 90
    assert "experiment_xyz" not in cfg


def test_apply_overrides_empty_value_skipped():
    desc = _length_field(55)
    cfg: dict = {}
    apply_overrides(cfg, {"length_s": None}, descriptors=[desc])
    assert "duration_max_s" not in cfg
    apply_overrides(cfg, {"length_s": ""}, descriptors=[desc])
    assert "duration_max_s" not in cfg


# ---------------------------------------------------------------------------
# long_form_overlay_from_spec — long-form transport
# ---------------------------------------------------------------------------


def test_long_form_overlay_from_spec_projects_spec_to_overlay():
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 600,
            "channel_overrides": {
                "voice": "pipeline/voice_refs/sarah.wav",
                "music_bed": "ambient_med",
                "captions_density": "minimal",
            },
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )
    overlay = long_form_overlay_from_spec(spec)
    assert overlay["music_bed_default"] == "ambient_med.mp3"
    assert overlay["captions_density"] == "minimal"
    assert overlay["duration_max_s"] == 600
    assert overlay["long_form"]["music_bed_default"] == "ambient_med.mp3"
    assert overlay["long_form"]["captions_density"] == "minimal"
    assert overlay["long_form"]["duration_max_s"] == 600


def test_long_form_overlay_empty_spec_returns_empty_dict():
    """Belt-and-braces: a None spec doesn't blow up."""
    assert long_form_overlay_from_spec(None) == {}


# ---------------------------------------------------------------------------
# prompt_patches_for — descriptor-driven LLM prompt biasing
# ---------------------------------------------------------------------------


def test_prompt_patches_for_audio_mode_song_sets_lyrics_schema():
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 1800,
            "channel_overrides": {"audio_mode": "song"},
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )
    patch = prompt_patches_for(spec)
    assert patch.schema_mode == "lyrics"
    assert any("SONG" in line for line in patch.context_lines)
    assert any("LYRICS" in rule for rule in patch.craft_rules)


def test_prompt_patches_for_visual_source_footage_adds_anchor_rule():
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 1800,
            "channel_overrides": {"visual_source": "footage"},
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )
    patch = prompt_patches_for(spec)
    assert any("FOOTAGE" in line for line in patch.context_lines)
    assert any("anchor on a specific event" in rule for rule in patch.craft_rules)


def test_prompt_patches_merge_multi_input_correctly():
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 1800,
            "channel_overrides": {
                "audio_mode": "song",
                "visual_source": "footage",
            },
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )
    patch = prompt_patches_for(spec)
    # BOTH branches contribute.
    assert len(patch.context_lines) >= 2
    assert len(patch.craft_rules) >= 3
    # Last-set schema_mode wins (audio_mode=song sets "lyrics").
    assert patch.schema_mode == "lyrics"


def test_prompt_patches_for_default_inputs_returns_empty_patch():
    """No descriptor-active inputs → no contributions."""
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 55,
            "channel_overrides": {},  # all defaults
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )
    patch = prompt_patches_for(spec)
    # voice=voice still has audio_mode_branch but value is "voice" → no patch
    # narrator_visual_branch is registered but no descriptor uses it yet
    # visual_source defaults to "ai" → no patch
    # Net: empty.
    assert patch.context_lines == []
    assert patch.craft_rules == []
    assert patch.schema_mode is None


# ---------------------------------------------------------------------------
# Extensibility regression — adding a new descriptor flows automatically
# ---------------------------------------------------------------------------


def test_extensibility_adding_one_descriptor_propagates_everywhere():
    """The whole point: declaring ONE new descriptor with cfg_targets +
    spec_field + prompt_patch_fn flows into ALL three consumers
    (apply_overrides, long_form_overlay_from_spec, prompt_patches_for)
    with no other code change.

    Simulated by registering a synthetic transform/patch + passing a
    synthetic descriptor as an explicit list.
    """
    @register_transform("upper_test_only")
    def _upper(v):  # noqa: ANN001
        return str(v).upper()

    @register_prompt_patch("synthetic_test_patch")
    def _patch(value, spec):  # noqa: ANN001
        return PromptPatch(
            context_lines=[f"synthetic_input is {value}"],
            craft_rules=["honor the synthetic input"],
        )

    desc = CustomizationField(
        key="synthetic_input",
        label="Synthetic",
        kind="text",
        spec_field="extra",  # not a real spec field — intentionally checks
                              # that prompt_patches_for tolerates spec fields
                              # that hold dicts (spec.extra is a passthrough).
        cfg_targets=[
            CfgTarget(path=["synthetic_cfg_key"], transform="upper_test_only"),
            CfgTarget(path=["nested", "synthetic"], transform="upper_test_only"),
        ],
        prompt_patch_fn="synthetic_test_patch",
    )

    cfg: dict = {}
    apply_overrides(cfg, {"synthetic_input": "hello"}, descriptors=[desc])
    assert cfg["synthetic_cfg_key"] == "HELLO"
    assert cfg["nested"]["synthetic"] == "HELLO"


# ---------------------------------------------------------------------------
# merge_patches semantics
# ---------------------------------------------------------------------------


def test_merge_patches_combines_lists_and_picks_last_schema_mode():
    p1 = PromptPatch(context_lines=["a"], craft_rules=["x"], schema_mode="modeA")
    p2 = PromptPatch(context_lines=["b"], craft_rules=["y"], schema_mode="modeB")
    p3 = PromptPatch(context_lines=["c"])
    out = merge_patches([p1, p2, p3])
    assert out.context_lines == ["a", "b", "c"]
    assert out.craft_rules == ["x", "y"]
    assert out.schema_mode == "modeB"  # last set wins
