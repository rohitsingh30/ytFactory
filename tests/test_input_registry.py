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
    """Voice on a cloud channel, full contract:

    - Path-style ref WAV → written to BOTH cfg locations.
    - Bare voice id that resolves to an existing ref WAV (e.g. Kokoro
      preset, LibriVox web voice, user clone) → resolved to a relative
      path and written to BOTH cfg locations.
    - Bare voice id that does NOT resolve to a file → dropped, but
      LOUDLY (warning logged + ``cfg["_dropped_inputs"]`` populated)
      so the worker can surface the lost input to Firestore.
    """
    desc = _voice_field("sarah", "en")

    # 1. Path-style ref writes BOTH locations.
    cfg = {"tts_provider": "cloudrun_chatterbox"}
    apply_overrides(cfg, {"voice": "pipeline/voice_refs/sarah.wav"}, descriptors=[desc])
    assert cfg["tts_voice"] == "pipeline/voice_refs/sarah.wav"
    assert cfg["long_form"]["tts_voice"] == "pipeline/voice_refs/sarah.wav"

    # 2. Bare voice id `af_sarah` (Kokoro preset shipped at
    # web/static/voice_samples/af_sarah.wav) → resolved to the
    # relative path and written to BOTH locations. Pre-fix this was
    # silently dropped and Sarah default kicked in.
    cfg = {"tts_provider": "cloudrun_chatterbox"}
    apply_overrides(cfg, {"voice": "af_sarah"}, descriptors=[desc])
    assert cfg["tts_voice"] == "web/static/voice_samples/af_sarah.wav"
    assert cfg["long_form"]["tts_voice"] == "web/static/voice_samples/af_sarah.wav"
    assert "_dropped_inputs" not in cfg, "resolvable voice id must not be dropped"

    # 3. Bare voice id that points at a real LibriVox web voice
    # (`pipeline/voice_refs/web/lv-alex-foster/ref.wav`) — the exact
    # case the user hit on 2026-05-13 when the wizard pick was lost.
    cfg = {"tts_provider": "cloudrun_chatterbox"}
    apply_overrides(cfg, {"voice": "lv-alex-foster"}, descriptors=[desc])
    assert cfg["tts_voice"] == "pipeline/voice_refs/web/lv-alex-foster/ref.wav"
    assert cfg["long_form"]["tts_voice"] == "pipeline/voice_refs/web/lv-alex-foster/ref.wav"

    # 4. Unresolvable bare voice id on cloud → DROPPED LOUDLY:
    #    cfg["tts_voice"] is NOT set, AND cfg["_dropped_inputs"] gets
    #    a structured entry the worker can surface to Firestore.
    cfg = {"tts_provider": "cloudrun_chatterbox"}
    apply_overrides(cfg, {"voice": "this-voice-id-does-not-exist"}, descriptors=[desc])
    assert "tts_voice" not in cfg
    assert "long_form" not in cfg
    assert "_dropped_inputs" in cfg
    assert len(cfg["_dropped_inputs"]) == 1
    assert cfg["_dropped_inputs"][0]["field"] == "voice"
    assert cfg["_dropped_inputs"][0]["value"] == "this-voice-id-does-not-exist"
    assert "no ref WAV" in cfg["_dropped_inputs"][0]["reason"]


def test_resolve_voice_id_to_path_covers_every_layout():
    """``_resolve_voice_id_to_path`` checks every voice-storage layout
    the dashboard's voice catalog endpoint can return — built-in
    catalog, user clones, web (LibriVox), flat-WAV, Kokoro presets.

    Mirrors ``control/routes/voices_routes.py::_voice_path``. If
    these two ever drift, the wizard will offer voice ids the worker
    can't resolve, and we'll regress the silent-drop bug.
    """
    from pipeline.render.input_registry import _resolve_voice_id_to_path  # noqa: PLC0415
    # Voice that must exist on disk for this test to be meaningful — pick
    # one we know ships in the repo (curated in pipeline/voice_refs/).
    sarah_flat = _resolve_voice_id_to_path("sarah")
    assert sarah_flat == "pipeline/voice_refs/sarah.wav"
    # Kokoro preset (lives in web/static/voice_samples/)
    af_sarah = _resolve_voice_id_to_path("af_sarah")
    assert af_sarah == "web/static/voice_samples/af_sarah.wav"
    # LibriVox web voice (lives in pipeline/voice_refs/web/<key>/ref.wav)
    alex = _resolve_voice_id_to_path("lv-alex-foster")
    assert alex == "pipeline/voice_refs/web/lv-alex-foster/ref.wav"
    # Unknown voice id returns None.
    assert _resolve_voice_id_to_path("not-a-real-voice-id-xyz") is None


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
        "captions_layout": "bottom_one_line",
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
    assert cfg["captions_layout"] == "bottom_one_line"
    assert cfg["visual_source"] == "both"
    assert cfg["duration_max_s"] == 90
    assert cfg["_suno_prompt_override"] == {"style": "cinematic upbeat pop"}

    # Nested long_form: writes — what the LONG-FORM path's overlay needs.
    lf = cfg["long_form"]
    assert lf["tts_voice"] == "pipeline/voice_refs/sarah.wav"
    assert lf["music_bed_default"] == "cinematic.mp3"
    assert lf["captions_layout"] == "bottom_one_line"
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
                "captions_layout": "bottom_one_line",
            },
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )
    overlay = long_form_overlay_from_spec(spec)
    assert overlay["music_bed_default"] == "ambient_med.mp3"
    assert overlay["captions_layout"] == "bottom_one_line"
    assert overlay["duration_max_s"] == 600
    assert overlay["long_form"]["music_bed_default"] == "ambient_med.mp3"
    assert overlay["long_form"]["captions_layout"] == "bottom_one_line"
    assert overlay["long_form"]["duration_max_s"] == 600
    # 2026-05-13: voice descriptor uses apply_handler, not cfg_targets.
    # Pre-fix the long-form overlay didn't dispatch apply_handlers, so
    # path-style voice picks were lost. Now both paths are honoured.
    assert overlay["tts_voice"] == "pipeline/voice_refs/sarah.wav"
    assert overlay["long_form"]["tts_voice"] == "pipeline/voice_refs/sarah.wav"


def test_long_form_overlay_resolves_bare_voice_id_via_apply_handler():
    """The exact case that broke job 1b5002eca4d84378a79ada87039fc05b
    on 2026-05-13: wizard sends voice='lv-alex-foster' (LibriVox web
    voice). Pre-fix the long-form overlay didn't dispatch
    apply_handlers, so the bare voice id was dropped before reaching
    _apply_voice — channel default kicked in, render produced Sarah's
    voice instead of Alex Foster's.

    With the fix, long_form_overlay_from_spec walks apply_handlers AND
    cfg_targets, so the bare id is resolved to its ref WAV path
    (pipeline/voice_refs/web/lv-alex-foster/ref.wav) and written into
    the overlay. long_form.py merges the overlay onto the channel
    YAML, so the worker uses Alex Foster's voice.
    """
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 1800,
            "channel_overrides": {"voice": "lv-alex-foster"},
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )
    overlay = long_form_overlay_from_spec(spec)
    # The bare id MUST be resolved to its actual ref WAV path.
    assert overlay["tts_voice"] == "pipeline/voice_refs/web/lv-alex-foster/ref.wav"
    assert overlay["long_form"]["tts_voice"] == "pipeline/voice_refs/web/lv-alex-foster/ref.wav"
    # The channel-meta seed (tts_provider) MUST NOT leak into the
    # overlay — overlay should only carry user-changed fields, not
    # mirror the channel YAML.
    assert "tts_provider" not in overlay


def test_long_form_overlay_unresolvable_voice_id_drops_loudly():
    """Bare voice id that doesn't resolve to any ref WAV — the
    apply_handler's _dropped_inputs marker MUST appear so the worker
    can surface it. Post-2026-05-15, build_spec ALSO runs apply_handlers
    so the bad voice gets dropped earlier in the chain — the overlay's
    tts_voice now carries the resolved CHANNEL DEFAULT (not the bad
    user-input). Verify both: the marker appears, AND the overlay's
    tts_voice is the channel default (not the unresolvable name).
    """
    spec = build_spec(
        {
            "channel": "mystoriesanimated",
            "format": "aita_animated",
            "length_s": 1800,
            "channel_overrides": {"voice": "this-voice-id-does-not-exist"},
        },
        channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
        variant_yaml_path=REPO_ROOT
        / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
    )
    overlay = long_form_overlay_from_spec(spec)
    # Overlay tts_voice MUST NOT contain the unresolvable name.
    assert overlay.get("tts_voice") != "this-voice-id-does-not-exist"
    # When tts_voice IS present, it's the channel default (resolvable).
    if "tts_voice" in overlay:
        assert "this-voice-id-does-not-exist" not in str(overlay["tts_voice"])
    # spec.voice_id MUST be the channel default (NOT the bad name) —
    # build_spec runs apply_handlers FIRST and records the drop on cfg
    # then falls voice_id back to cfg.tts_voice (the channel default).
    assert spec.voice_id != "this-voice-id-does-not-exist"


def test_long_form_overlay_unknown_apply_handler_logs_and_skips():
    """If a descriptor references an apply_handler name not in the
    registry, long_form_overlay_from_spec must log + skip without
    raising. Pre-fix this branch existed for cfg_targets but not
    apply_handlers; the new path needs the same defensiveness.
    """
    from pipeline.render.input_registry import (  # noqa: PLC0415
        long_form_overlay_from_spec as _build,
    )
    from dataclasses import dataclass, field
    from typing import Any as _Any

    @dataclass
    class _FakeDesc:
        key: str = "x"
        spec_field: str = "voice_id"
        apply_handler: str = "this_handler_does_not_exist"
        cfg_targets: tuple = ()
        prompt_patch_fn: _Any = None

    @dataclass
    class _FakeSpec:
        channel: str = "mystoriesanimated"
        voice_id: str = "lv-alex-foster"

    # Patch _resolve_descriptors to return our fake descriptor with the
    # unknown handler name. Use the public API as the entry point.
    import pipeline.render.input_registry as ir  # noqa: PLC0415

    def _fake_resolve(_):
        return [_FakeDesc()]

    orig = ir._resolve_descriptors
    ir._resolve_descriptors = _fake_resolve
    try:
        # Should not raise — just log + skip.
        out = _build(_FakeSpec())
        # The unknown-handler descriptor contributes nothing → overlay
        # should not have tts_voice.
        assert "tts_voice" not in out
    finally:
        ir._resolve_descriptors = orig


def test_long_form_overlay_apply_handler_raise_does_not_propagate():
    """If an apply_handler raises mid-dispatch, the overlay builder
    must catch + log + continue with the next descriptor — never
    propagate (would crash render_long_form before any subprocess
    fires).
    """
    from pipeline.render.input_registry import (  # noqa: PLC0415
        long_form_overlay_from_spec as _build,
        register_apply_handler,
    )
    from dataclasses import dataclass
    import pipeline.render.input_registry as ir  # noqa: PLC0415

    register_apply_handler("test_raises_handler")(
        lambda _cfg, _val, _meta: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    @dataclass
    class _FakeDesc:
        key: str = "x"
        spec_field: str = "voice_id"
        apply_handler: str = "test_raises_handler"
        cfg_targets: tuple = ()
        prompt_patch_fn: object = None

    @dataclass
    class _FakeSpec:
        channel: str = "mystoriesanimated"
        voice_id: str = "lv-alex-foster"

    def _fake_resolve(_):
        return [_FakeDesc()]

    orig = ir._resolve_descriptors
    ir._resolve_descriptors = _fake_resolve
    try:
        out = _build(_FakeSpec())
        # Handler raised → overlay didn't grow from this descriptor.
        assert "tts_voice" not in out
    finally:
        ir._resolve_descriptors = orig


def test_channel_meta_for_overlay_handles_missing_channel():
    """_channel_meta_for_overlay must return an empty dict when the
    channel key is None or the YAML doesn't exist — never raise.
    """
    from pipeline.render.input_registry import _channel_meta_for_overlay  # noqa: PLC0415
    assert _channel_meta_for_overlay(None) == {}
    assert _channel_meta_for_overlay("") == {}
    assert _channel_meta_for_overlay("nonexistent_channel_xyz") == {}


def test_channel_meta_for_overlay_handles_corrupt_yaml(tmp_path, monkeypatch):
    """If the channel YAML is corrupt (parse error), the helper must
    return {} not propagate the exception — the overlay path is on
    the critical render flow and one bad YAML shouldn't kill it.
    """
    # Make a fake channel YAML that's invalid YAML.
    import pipeline.render.input_registry as ir  # noqa: PLC0415
    fake_repo = tmp_path
    (fake_repo / "pipeline" / "channels").mkdir(parents=True)
    (fake_repo / "pipeline" / "channels" / "broken.yaml").write_text("not: [valid: yaml")
    # Monkeypatch the repo-root resolution by shimming os.path.exists+open.
    import os, builtins  # noqa: PLC0415, E401
    orig_dirname = os.path.dirname
    def _dn(p):
        if p == ir.__file__:
            return str(fake_repo / "pipeline" / "render")
        return orig_dirname(p)
    monkeypatch.setattr(os.path, "dirname", _dn)
    out = ir._channel_meta_for_overlay("broken")
    assert out == {}


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
