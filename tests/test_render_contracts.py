"""Tests for pipeline/render/contracts.py.

Pins the seven Protocol shapes + dataclass invariants + registry
behavior. These tests are the engine's safety net — if a future PR
breaks the contracts module, every plugin breaks loudly here before
shipping.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from pipeline.render.contracts import (
    AudioResult,
    AudioSynthesizer,
    FinalMux,
    MusicComposer,
    OverlayElement,
    OverlayProducer,
    PluginNotFound,
    Section,
    Segment,
    TimelineBuilder,
    VisualProducer,
    VisualTrack,
    get_plugin,
    list_plugins,
    register_plugin,
)

# Eagerly import every plugin package so their register_plugin calls
# populate the registry BEFORE any test snapshots it. Without these,
# RegistryTest.setUp would snapshot an empty registry then restore an
# empty registry on tearDown — leaving downstream tests in the same
# pytest run with no registered plugins (the source of the test
# pollution observed on 2026-05-14 when the full sweep ran).
import pipeline.render.audio  # noqa: F401, E402
import pipeline.render.timeline  # noqa: F401, E402
import pipeline.render.visualize  # noqa: F401, E402
import pipeline.render.overlays  # noqa: F401, E402
import pipeline.render.music  # noqa: F401, E402
import pipeline.render.compose  # noqa: F401, E402


# ---------------------------------------------------------------------------
# Dataclass invariants
# ---------------------------------------------------------------------------


class AudioResultTest(unittest.TestCase):
    def test_minimal_construction(self):
        r = AudioResult(narration_path=Path("/tmp/n.wav"), duration_s=12.5)
        self.assertEqual(r.narration_path, Path("/tmp/n.wav"))
        self.assertEqual(r.duration_s, 12.5)
        self.assertEqual(r.voice_fingerprint, {})
        self.assertIsNone(r.chunk_timings)

    def test_full_construction(self):
        r = AudioResult(
            narration_path=Path("/tmp/n.wav"),
            duration_s=120.0,
            voice_fingerprint={"provider": "cloudrun_chatterbox"},
            chunk_timings=[(0.0, 30.0), (30.0, 60.0), (60.0, 120.0)],
        )
        self.assertEqual(len(r.chunk_timings), 3)
        self.assertEqual(r.voice_fingerprint["provider"], "cloudrun_chatterbox")


class SegmentTest(unittest.TestCase):
    def test_default_kind_is_beat(self):
        s = Segment(start_s=0.0, end_s=1.5, text="Hello", anchor_id="beat_001")
        self.assertEqual(s.kind, "beat")

    def test_explicit_kind(self):
        s = Segment(start_s=0.0, end_s=180.0, text="Intro section",
                    anchor_id="intro", kind="section")
        self.assertEqual(s.kind, "section")


class VisualTrackTest(unittest.TestCase):
    def test_extras_default_empty(self):
        v = VisualTrack(video_path=Path("/tmp/v.mp4"), duration_s=60.0)
        self.assertEqual(v.extras, {})

    def test_extras_populated_by_producer(self):
        v = VisualTrack(
            video_path=Path("/tmp/v.mp4"),
            duration_s=60.0,
            extras={"images": [Path("/tmp/im_001.png"), Path("/tmp/im_002.png")]},
        )
        self.assertEqual(len(v.extras["images"]), 2)


class OverlayElementTest(unittest.TestCase):
    def test_default_layer_blend(self):
        o = OverlayElement(start_s=0.0, end_s=2.0, layer=40,
                           asset_path=Path("/tmp/cap.png"))
        self.assertEqual(o.blend, "over")
        self.assertIsNone(o.region)
        self.assertEqual(o.extras, {})

    def test_layer_convention_values(self):
        # Layer convention from contracts.py docstring: 10/20/30/40/50.
        # Pin the values so producers using the convention stay aligned.
        for layer in (10, 20, 30, 40, 50):
            o = OverlayElement(start_s=0.0, end_s=1.0, layer=layer,
                               asset_path=Path("/tmp/o.png"))
            self.assertEqual(o.layer, layer)


class SectionTest(unittest.TestCase):
    def test_construction(self):
        s = Section(id="intro", title="The setup", start_s=0.0, end_s=90.0,
                    body="Once upon a time...")
        self.assertEqual(s.id, "intro")
        self.assertEqual(s.end_s, 90.0)


# ---------------------------------------------------------------------------
# Protocol shapes (runtime_checkable lets us isinstance-check)
# ---------------------------------------------------------------------------


class _StubAudio:
    def synth(self, spec, script, work_dir):
        return AudioResult(narration_path=Path("/x"), duration_s=1.0)


class _StubTimeline:
    def build(self, spec, script, audio):
        return []


class _StubVisuals:
    def produce(self, spec, timeline, work_dir):
        return VisualTrack(video_path=Path("/x"), duration_s=1.0)


class _StubOverlay:
    def produce(self, spec, timeline, audio):
        return []


class _StubMusic:
    def compose(self, spec, narration_duration_s, sections=None):
        return Path("/x.wav")


class _StubMux:
    def mux(self, visuals, audio, overlays, music, spec, out_path):
        return out_path


class ProtocolStructuralCheckTest(unittest.TestCase):
    """isinstance(impl, ProtocolClass) works because Protocols are
    @runtime_checkable. Pinning these so a regression that drops the
    decorator gets caught immediately."""

    def test_audio_synthesizer(self):
        self.assertIsInstance(_StubAudio(), AudioSynthesizer)

    def test_timeline_builder(self):
        self.assertIsInstance(_StubTimeline(), TimelineBuilder)

    def test_visual_producer(self):
        self.assertIsInstance(_StubVisuals(), VisualProducer)

    def test_overlay_producer(self):
        self.assertIsInstance(_StubOverlay(), OverlayProducer)

    def test_music_composer(self):
        self.assertIsInstance(_StubMusic(), MusicComposer)

    def test_final_mux(self):
        self.assertIsInstance(_StubMux(), FinalMux)


class ProtocolRejectsMissingMethod(unittest.TestCase):
    """Things that lack the Protocol method are NOT instances. Catches
    a regression where someone replaces the Protocol with a vanilla
    class."""

    def test_audio_rejected_when_method_missing(self):
        class Empty: ...
        self.assertNotIsInstance(Empty(), AudioSynthesizer)


# ---------------------------------------------------------------------------
# Registry behavior
# ---------------------------------------------------------------------------


class RegistryTest(unittest.TestCase):
    def setUp(self):
        # Swap the module-level _REGISTRY for a fresh dict via unittest.mock.patch,
        # which auto-restores the original on tearDown via addCleanup. This is
        # bulletproof against the snapshot-empty-then-restore-empty pollution
        # path: even if the plugin packages haven't loaded yet, the production
        # registry is never mutated — we work entirely on the fresh dict.
        from unittest.mock import patch
        from pipeline.render import contracts
        self._patcher = patch.object(contracts, "_REGISTRY", new={})
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_register_and_get_roundtrip(self):
        impl = _StubAudio()
        register_plugin("audio", "stub", impl)
        self.assertIs(get_plugin("audio", "stub"), impl)

    def test_re_register_replaces(self):
        impl_v1 = _StubAudio()
        impl_v2 = _StubAudio()
        register_plugin("audio", "stub", impl_v1)
        register_plugin("audio", "stub", impl_v2)
        self.assertIs(get_plugin("audio", "stub"), impl_v2)

    def test_get_unknown_slot_raises_with_helpful_message(self):
        with self.assertRaises(PluginNotFound) as ctx:
            get_plugin("visualize", "nonexistent")
        self.assertIn("nonexistent", str(ctx.exception))
        self.assertIn("visualize", str(ctx.exception))
        self.assertIn("none registered", str(ctx.exception))

    def test_get_unknown_name_lists_available(self):
        register_plugin("visualize", "ai_beat_slideshow", _StubVisuals())
        register_plugin("visualize", "motion_clips", _StubVisuals())
        with self.assertRaises(PluginNotFound) as ctx:
            get_plugin("visualize", "typo_name")
        msg = str(ctx.exception)
        self.assertIn("ai_beat_slideshow", msg)
        self.assertIn("motion_clips", msg)
        self.assertIn("typo_name", msg)

    def test_list_plugins_sorted(self):
        register_plugin("music", "z_last", _StubMusic())
        register_plugin("music", "a_first", _StubMusic())
        register_plugin("music", "m_middle", _StubMusic())
        self.assertEqual(list_plugins("music"), ["a_first", "m_middle", "z_last"])

    def test_list_plugins_empty_slot(self):
        self.assertEqual(list_plugins("never_registered"), [])

    def test_independent_slots(self):
        # Same name in different slots = different impls.
        a = _StubAudio()
        v = _StubVisuals()
        register_plugin("audio", "default", a)
        register_plugin("visualize", "default", v)
        self.assertIs(get_plugin("audio", "default"), a)
        self.assertIs(get_plugin("visualize", "default"), v)


if __name__ == "__main__":
    unittest.main()
