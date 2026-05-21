"""Tests for new overlay + music + visualize plugin impls.

Pin Protocol contracts + minimal behavior. Real wrap-target behavior
(captions look right, music ducks correctly, panels render) is
exercised by the engine goldens (which run with cloud TTS / Flux /
real ffmpeg) — these unit tests only confirm the plugin-side contract.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.render.contracts import (
    AudioResult,
    MusicComposer,
    OverlayElement,
    OverlayProducer,
    Segment,
    Timeline,
    VisualProducer,
    VisualTrack,
)
from pipeline.render.music.ducked_loop import DuckedLoop
from pipeline.render.overlays.sentence_caption_ass import SentenceCaptionAss
from pipeline.render.overlays.word_caption_pngs import WordCaptionPngs
from pipeline.render.spec import build_spec
from pipeline.render.visualize.longform_panels import LongformPanels


def _make_wav(path: Path, duration_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "sine=frequency=440:sample_rate=24000",
        "-ac", "1", "-c:a", "pcm_s16le",
        str(path),
    ], check=True, capture_output=True)


class SentenceCaptionAssProtocolTest(unittest.TestCase):
    def test_satisfies_overlay_producer_protocol(self):
        self.assertIsInstance(SentenceCaptionAss(), OverlayProducer)

    def test_empty_timeline_returns_empty(self):
        spec = build_spec({"channel": "x", "channel_overrides": {}},
                          channel_yaml_path=None, variant_yaml_path=None)
        audio = AudioResult(narration_path=Path("/tmp/n.wav"), duration_s=1.0)
        result = SentenceCaptionAss().produce(spec, timeline=[], audio=audio)
        self.assertEqual(result, [])

    def test_timeline_without_text_returns_empty(self):
        # Segments with empty text get skipped.
        spec = build_spec({"channel": "x", "channel_overrides": {}},
                          channel_yaml_path=None, variant_yaml_path=None)
        audio = AudioResult(narration_path=Path("/tmp/n.wav"), duration_s=1.0)
        timeline: Timeline = [Segment(start_s=0, end_s=1, text="", anchor_id="x")]
        result = SentenceCaptionAss().produce(spec, timeline=timeline, audio=audio)
        self.assertEqual(result, [])


class WordCaptionPngsProtocolTest(unittest.TestCase):
    def test_satisfies_overlay_producer_protocol(self):
        self.assertIsInstance(WordCaptionPngs(), OverlayProducer)

    def test_empty_timeline_returns_empty(self):
        spec = build_spec({"channel": "x", "channel_overrides": {}},
                          channel_yaml_path=None, variant_yaml_path=None)
        audio = AudioResult(narration_path=Path("/tmp/n.wav"), duration_s=1.0)
        result = WordCaptionPngs().produce(spec, timeline=[], audio=audio)
        self.assertEqual(result, [])

    def test_font_size_picks_minimal_for_minimal_density(self):
        # Pin the density-→-font-size lookup. Important because the
        # values match historical shorts.py defaults.
        spec = build_spec({
            "channel": "x",
            "channel_overrides": {"captions_density": "minimal"},
        }, channel_yaml_path=None, variant_yaml_path=None)
        size = WordCaptionPngs()._font_size_for_density(spec)
        self.assertEqual(size, spec.caption_style.font_size_minimal)

    def test_font_size_picks_dense_for_dense_density(self):
        spec = build_spec({
            "channel": "x",
            "channel_overrides": {"captions_density": "dense"},
        }, channel_yaml_path=None, variant_yaml_path=None)
        size = WordCaptionPngs()._font_size_for_density(spec)
        self.assertEqual(size, spec.caption_style.font_size_dense)

    def test_font_size_picks_standard_default(self):
        spec = build_spec({"channel": "x", "channel_overrides": {}},
                          channel_yaml_path=None, variant_yaml_path=None)
        size = WordCaptionPngs()._font_size_for_density(spec)
        self.assertEqual(size, spec.caption_style.font_size_standard)

    def test_import_failure_logs_warning_and_returns_empty(self):
        # 2026-05-15 fail-loud audit: when captions_enabled=False the
        # ImportError path still returns [] silently — that's the
        # user-explicit-opt-out lane. When captions_enabled=True the
        # plugin raises RenderFailedError (pinned in
        # tests/render/test_fail_loud_fallbacks.py).
        #
        # This test exercises the captions-disabled lane: WARN +
        # return [], same shape as the pre-fix behaviour minus the
        # silent-when-enabled regression.
        import logging
        import sys
        spec = build_spec({"channel": "x", "channel_overrides": {
            "captions_enabled": False,
        }}, channel_yaml_path=None, variant_yaml_path=None)
        audio = AudioResult(narration_path=Path("/tmp/n.wav"), duration_s=1.0)
        timeline: Timeline = [Segment(start_s=0, end_s=1, text="hi", anchor_id="x")]

        # Force the import to fail by hiding the captions module.
        original = sys.modules.get("pipeline.captions")
        sys.modules["pipeline.captions"] = None  # triggers ImportError
        try:
            with self.assertLogs(level=logging.WARNING) as logs:
                result = WordCaptionPngs().produce(spec, timeline, audio)
        finally:
            if original is not None:
                sys.modules["pipeline.captions"] = original
            else:
                sys.modules.pop("pipeline.captions", None)
        self.assertEqual(result, [])
        # Warning must mention the missing module so the operator can
        # debug the Docker COPY / requirements regression.
        self.assertTrue(
            any("captions" in msg.lower() for msg in logs.output),
            f"expected captions-related warning; got: {logs.output}",
        )


class DuckedLoopProtocolTest(unittest.TestCase):
    def test_satisfies_music_composer_protocol(self):
        self.assertIsInstance(DuckedLoop(), MusicComposer)


class DuckedLoopBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-ducked-loop-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_off_bed_falls_back_to_silent(self):
        # When music.default_bed is "off", DuckedLoop falls back to
        # SilentMusic. Verify the result is a real silent wav.
        spec = build_spec(
            {"channel": "x", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )
        spec.music.default_bed = "off"
        result = DuckedLoop().compose(spec, narration_duration_s=0.5)
        try:
            self.assertTrue(result.exists())
            # Probe — should be ~0.5s of silence.
            dur = float(subprocess.check_output([
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nokey=1:noprint_wrappers=1",
                str(result),
            ]).decode().strip())
            self.assertAlmostEqual(dur, 0.5, delta=0.1)
        finally:
            result.unlink(missing_ok=True)

    def test_empty_bed_falls_back_to_silent(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )
        spec.music.default_bed = ""
        result = DuckedLoop().compose(spec, narration_duration_s=0.5)
        try:
            self.assertTrue(result.exists())
        finally:
            result.unlink(missing_ok=True)


class LongformPanelsProtocolTest(unittest.TestCase):
    def test_satisfies_visual_producer_protocol(self):
        self.assertIsInstance(LongformPanels(), VisualProducer)


class LongformPanelsBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-longform-panels-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_timeline_returns_solid_color_fallback(self):
        # 2026-05-15 fail-loud audit: solid-color is now opt-in via
        # YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1. With the override
        # set we still pin the legacy "empty timeline = 1s solid color"
        # behaviour so emergency renders ship.
        import os
        from unittest.mock import patch

        spec = build_spec(
            {"channel": "x", "channel_overrides": {"length_s": 1800}},  # long
            channel_yaml_path=None, variant_yaml_path=None,
        )
        with patch.dict(
            os.environ, {"YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK": "1"},
        ):
            result = LongformPanels().produce(spec, timeline=[], work_dir=self.tmp)
        self.assertIsInstance(result, VisualTrack)
        self.assertTrue(result.video_path.exists())
        self.assertGreater(result.duration_s, 0)
        # Fallback path has the expected extras marker.
        self.assertEqual(result.extras["source"], "longform_panels_fallback")


if __name__ == "__main__":
    unittest.main()
