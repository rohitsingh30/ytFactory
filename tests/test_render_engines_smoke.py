"""End-to-end engine smoke test using fixture plugins.

Wires :mod:`pipeline.render.short_engine` end-to-end with the
fixture-loading variants of every plugin:

* audio    — :mod:`pipeline.render.audio.audio_from_fixture`
* timeline — :mod:`pipeline.render.timeline.timeline_from_fixture`
* visualize — :mod:`pipeline.render.visualize.visuals_from_fixture`
* overlays  — :mod:`pipeline.render.overlays.noop`
* music     — :mod:`pipeline.render.music.silent`
* compose   — :mod:`pipeline.render.compose.beat_slideshow_mux` (REAL ffmpeg)

Pins:

- Engine dispatcher picks the right engine for spec.kind
- Every plugin Protocol contract is satisfied
- The end-to-end call produces a structurally-valid mp4
- Output duration matches the audio fixture (within 1% tolerance)

This test is the foundation for the future engine golden tests
(2-engine x 1 canonical-real-plugin per slot) — those run with
real cloud TTS / cloud whisper / Flux instead of fixtures.
Tolerance assertions match the design in plan.md Part D4.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline.render.contracts import list_plugins
from pipeline.render.engine import pick_engine
from pipeline.render.short_engine import render_short
from pipeline.render.spec import (
    AudioMode,
    CaptionsLayout,
    MusicPolicy,
    RenderKind,
    VisualMode,
    build_spec,
)


def _make_fixture_wav(out: Path, duration_s: float = 4.0) -> None:
    """Generate a deterministic sine-wave wav via ffmpeg lavfi."""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", f"sine=frequency=440:sample_rate=24000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(out),
    ], check=True, capture_output=True)


def _make_fixture_mp4(out: Path, duration_s: float = 4.0) -> None:
    """Generate a deterministic solid-color mp4 via ffmpeg lavfi.

    1080x1920 (9:16, short shape), 30 fps, dark grey #141414, yuv420p.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "color=c=0x141414:s=1080x1920:r=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        str(out),
    ], check=True, capture_output=True)


def _make_fixture_beats(out: Path, duration_s: float = 4.0) -> None:
    """Generate a small 3-segment timeline JSON."""
    out.parent.mkdir(parents=True, exist_ok=True)
    seg_len = duration_s / 3
    out.write_text(json.dumps([
        {"start_s": 0.0, "end_s": seg_len, "text": "first beat",
         "anchor_id": "beat_000", "kind": "beat"},
        {"start_s": seg_len, "end_s": 2 * seg_len, "text": "middle beat",
         "anchor_id": "beat_001", "kind": "beat"},
        {"start_s": 2 * seg_len, "end_s": 3 * seg_len, "text": "last beat",
         "anchor_id": "beat_002", "kind": "beat"},
    ]))


def _ffprobe(path: Path, *probe_args: str) -> str:
    """Run ffprobe with given args and return stripped stdout."""
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        *probe_args,
        str(path),
    ]).decode().strip()
    return out


class PluginRegistrationTest(unittest.TestCase):
    """Pin that every expected plugin registers via package-import.

    Catches a regression where a plugin module is added but forgotten
    in the package ``__init__`` (would silently never register and
    the engine would PluginNotFound at first use)."""

    def setUp(self):
        # Force a fresh import side-effect so the registry is populated.
        import pipeline.render.audio  # noqa: F401, PLC0415
        import pipeline.render.timeline  # noqa: F401, PLC0415
        import pipeline.render.visualize  # noqa: F401, PLC0415
        import pipeline.render.overlays  # noqa: F401, PLC0415
        import pipeline.render.music  # noqa: F401, PLC0415
        import pipeline.render.compose  # noqa: F401, PLC0415

    def test_audio_plugins(self):
        plugins = list_plugins("audio")
        # tts_single + tts_chunked + audio_from_fixture
        self.assertIn("tts_single", plugins)
        self.assertIn("tts_chunked", plugins)
        self.assertIn("audio_from_fixture", plugins)

    def test_timeline_plugins(self):
        plugins = list_plugins("timeline")
        self.assertIn("asr_beats", plugins)
        self.assertIn("asr_anchors", plugins)
        self.assertIn("timeline_from_fixture", plugins)

    def test_visualize_plugins(self):
        plugins = list_plugins("visualize")
        self.assertIn("visuals_from_fixture", plugins)

    def test_overlays_plugins(self):
        plugins = list_plugins("overlays")
        self.assertIn("noop", plugins)

    def test_music_plugins(self):
        plugins = list_plugins("music")
        self.assertIn("none", plugins)
        self.assertIn("single_bed", plugins)

    def test_compose_plugins(self):
        plugins = list_plugins("compose")
        self.assertIn("beat_slideshow", plugins)
        self.assertIn("section_video", plugins)


class EngineDispatcherTest(unittest.TestCase):
    def _spec(self, **overrides):
        return build_spec(
            {"channel": "x", "channel_overrides": overrides},
            channel_yaml_path=None, variant_yaml_path=None,
        )

    def test_short_dispatches_to_short_engine(self):
        spec = self._spec(length_s=60)
        fn = pick_engine(spec)
        self.assertEqual(fn.__name__, "render_short")

    def test_long_dispatches_to_long_engine(self):
        spec = self._spec(length_s=1800)
        fn = pick_engine(spec)
        self.assertEqual(fn.__name__, "render_long")

    def test_sports_doc_kind_routes_to_long_engine(self):
        # sports_doc collapses into long with visual_mode=overlay_timeline
        # in the bigbang PR. Until then, the engine dispatcher already
        # routes the SPORTS_DOC enum value to the long engine so plugin
        # behavior is consistent.
        spec = self._spec(kind="sports_doc")
        fn = pick_engine(spec)
        self.assertEqual(fn.__name__, "render_long")

    def test_footage_only_kind_routes_to_long_engine(self):
        spec = self._spec(kind="footage_only")
        fn = pick_engine(spec)
        self.assertEqual(fn.__name__, "render_long")


class ShortEngineEndToEndFixturePluginsTest(unittest.TestCase):
    """Wires every plugin slot with the fixture variant + runs the
    engine end-to-end. Pins that the dispatch + Protocol contract +
    final ffmpeg mux all work as a unit.

    Real cloud-driven plugins (cloudrun_chatterbox / cloudrun_whisper /
    Flux ai_beat_slideshow) are NOT exercised here — they have their
    own per-plugin unit tests + the future engine goldens. This test
    is the cheap fast smoke that runs on every PR.
    """

    def setUp(self):
        # Eager-import all plugin packages so registry is populated.
        import pipeline.render.audio  # noqa: F401, PLC0415
        import pipeline.render.timeline  # noqa: F401, PLC0415
        import pipeline.render.visualize  # noqa: F401, PLC0415
        import pipeline.render.overlays  # noqa: F401, PLC0415
        import pipeline.render.music  # noqa: F401, PLC0415
        import pipeline.render.compose  # noqa: F401, PLC0415

        self.tmp = Path(tempfile.mkdtemp(prefix=".test-engine-smoke-"))
        self.fixture_audio = self.tmp / "fixtures" / "narration.wav"
        self.fixture_visuals = self.tmp / "fixtures" / "visuals.mp4"
        self.fixture_timeline = self.tmp / "fixtures" / "beats.json"
        _make_fixture_wav(self.fixture_audio, duration_s=4.0)
        _make_fixture_mp4(self.fixture_visuals, duration_s=4.0)
        _make_fixture_beats(self.fixture_timeline, duration_s=4.0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_end_to_end_short_with_fixture_plugins(self):
        # Build spec with explicit plugin overrides via spec.extra so
        # the engine picks the fixture variants for audio/timeline/
        # visualize. captions disabled (overlays.noop) + music = silent
        # so we don't need real overlay / music plugins yet.
        spec = build_spec({
            "channel": "test_channel",
            "channel_overrides": {
                "kind": "short",
                "captions_enabled": False,
                "music_policy": "none",
                # spec.extra fields — engine reads these to override
                # the default plugin selection.
                "audio_plugin": "audio_from_fixture",
                "timeline_plugin": "timeline_from_fixture",
                "visualize_plugin": "visuals_from_fixture",
                "audio_fixture_path": str(self.fixture_audio),
                "visuals_fixture_path": str(self.fixture_visuals),
                "timeline_fixture_path": str(self.fixture_timeline),
            },
        }, channel_yaml_path=None, variant_yaml_path=None)

        # Sanity: spec resolved correctly.
        self.assertEqual(spec.kind, RenderKind.SHORT)
        self.assertFalse(spec.captions_enabled)
        self.assertEqual(spec.music_policy, MusicPolicy.NONE)

        out_path = self.tmp / "out" / "smoke.mp4"
        work_dir = self.tmp / "work"
        result = render_short(spec, script={"narration": "smoke test"},
                              work_dir=work_dir, out_path=out_path)

        # Output exists and is the path we asked for.
        self.assertEqual(result, out_path)
        self.assertTrue(out_path.exists(), f"output mp4 missing at {out_path}")
        self.assertGreater(out_path.stat().st_size, 0, "output mp4 is empty")

    def test_output_mp4_structural_invariants(self):
        # Same setup as above + verify ffprobe-reported structural
        # properties match the spec — tolerance-band assertions per
        # plan.md Part D4.
        spec = build_spec({
            "channel": "test_channel",
            "channel_overrides": {
                "kind": "short",
                "captions_enabled": False,
                "music_policy": "none",
                "audio_plugin": "audio_from_fixture",
                "timeline_plugin": "timeline_from_fixture",
                "visualize_plugin": "visuals_from_fixture",
                "audio_fixture_path": str(self.fixture_audio),
                "visuals_fixture_path": str(self.fixture_visuals),
                "timeline_fixture_path": str(self.fixture_timeline),
            },
        }, channel_yaml_path=None, variant_yaml_path=None)

        out_path = self.tmp / "out" / "structural.mp4"
        render_short(spec, script={"narration": "smoke test"},
                     work_dir=self.tmp / "work", out_path=out_path)

        # Codec + sample rate (exact match — these can't drift).
        self.assertEqual(
            _ffprobe(out_path, "-select_streams", "v:0",
                     "-show_entries", "stream=codec_name",
                     "-of", "default=nokey=1:noprint_wrappers=1"),
            "h264",
        )
        self.assertEqual(
            _ffprobe(out_path, "-select_streams", "a:0",
                     "-show_entries", "stream=codec_name",
                     "-of", "default=nokey=1:noprint_wrappers=1"),
            "aac",
        )
        # Resolution = spec.output_resolution (1080x1920 for short default).
        wxh = _ffprobe(out_path, "-select_streams", "v:0",
                       "-show_entries", "stream=width,height",
                       "-of", "csv=p=0").replace(",", "x")
        self.assertEqual(wxh, "1080x1920")

        # Duration within ±1% of audio fixture (4.0s).
        dur = float(_ffprobe(out_path, "-show_entries", "format=duration",
                             "-of", "default=nokey=1:noprint_wrappers=1"))
        self.assertAlmostEqual(dur, 4.0, delta=0.04,
                               msg=f"duration {dur} drifted >1% from 4.0s")


if __name__ == "__main__":
    unittest.main()
