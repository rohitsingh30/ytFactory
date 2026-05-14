"""Engine integration golden — long engine end-to-end.

Same shape as the short engine golden (see test_short_engine_golden.py).
Two modes:

* **Fixture mode (CI default)** — fixture audio + visuals + timeline.
* **Cloud mode (YTFACTORY_GOLDEN_CLOUD=1)** — real cloudrun_chatterbox
  chunked + cloudrun_asr (anchors mode) + Flux longform_panels.

Tolerance assertions identical to the short golden (per plan.md Part D4).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

# Eager-import every plugin package so the registry is populated.
import pipeline.render.audio  # noqa: F401
import pipeline.render.compose  # noqa: F401
import pipeline.render.music  # noqa: F401
import pipeline.render.overlays  # noqa: F401
import pipeline.render.timeline  # noqa: F401
import pipeline.render.visualize  # noqa: F401
from pipeline.render.long_engine import render_long
from pipeline.render.spec import build_spec
from tests.render.golden_assertions import (
    assert_duration_within_pct,
    assert_structural,
)


def _make_fixture_wav(path: Path, duration_s: float = 12.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "sine=frequency=440:sample_rate=24000",
        "-ac", "1", "-c:a", "pcm_s16le",
        str(path),
    ], check=True, capture_output=True)


def _make_fixture_mp4_169(path: Path, duration_s: float = 12.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "color=c=0x0a1626:s=1920x1080:r=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-pix_fmt", "yuv420p",
        str(path),
    ], check=True, capture_output=True)


def _make_fixture_sections(path: Path, duration_s: float = 12.0) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    sec_len = duration_s / 2
    path.write_text(json.dumps([
        {"start_s": 0.0, "end_s": sec_len, "text": "section one body",
         "anchor_id": "intro", "kind": "section"},
        {"start_s": sec_len, "end_s": 2 * sec_len, "text": "section two body",
         "anchor_id": "outro", "kind": "section"},
    ]))


CLOUD_MODE = os.environ.get("YTFACTORY_GOLDEN_CLOUD") == "1"


class LongEngineGoldenTest(unittest.TestCase):
    """End-to-end golden for the long engine."""

    DURATION_S = 12.0

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-long-golden-"))
        if not CLOUD_MODE:
            self.audio_fixture = self.tmp / "fixtures" / "narration.wav"
            self.visuals_fixture = self.tmp / "fixtures" / "visuals.mp4"
            self.timeline_fixture = self.tmp / "fixtures" / "sections.json"
            _make_fixture_wav(self.audio_fixture, duration_s=self.DURATION_S)
            _make_fixture_mp4_169(self.visuals_fixture, duration_s=self.DURATION_S)
            _make_fixture_sections(self.timeline_fixture, duration_s=self.DURATION_S)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build_spec(self):
        if CLOUD_MODE:
            return build_spec({
                "channel": "test_channel",
                "channel_overrides": {
                    "kind": "long_form",  # bigbang collapses to "long"
                    "captions_enabled": False,
                    "music_policy": "none",
                    "voice_provider": "cloudrun_chatterbox",
                },
            }, channel_yaml_path=None, variant_yaml_path=None)
        return build_spec({
            "channel": "test_channel",
            "channel_overrides": {
                "kind": "long_form",
                "captions_enabled": False,
                "music_policy": "none",
                "audio_plugin": "audio_from_fixture",
                "timeline_plugin": "timeline_from_fixture",
                "visualize_plugin": "visuals_from_fixture",
                "audio_fixture_path": str(self.audio_fixture),
                "visuals_fixture_path": str(self.visuals_fixture),
                "timeline_fixture_path": str(self.timeline_fixture),
            },
        }, channel_yaml_path=None, variant_yaml_path=None)

    def test_long_engine_produces_structurally_valid_mp4(self):
        spec = self._build_spec()
        out = self.tmp / "out" / "long.mp4"
        result = render_long(
            spec,
            script={
                "narration": "long-form smoke test",
                "sections": [
                    {"id": "intro", "title": "Intro", "body": "section one body"},
                    {"id": "outro", "title": "Outro", "body": "section two body"},
                ],
            },
            work_dir=self.tmp / "work",
            out_path=out,
        )

        self.assertEqual(result, out)

        # Long engine defaults to 16:9 / 1920x1080.
        assert_structural(
            out,
            codec_video="h264",
            codec_audio="aac",
            sample_rate=24000,
            resolution=spec.output_resolution,
        )

        if not CLOUD_MODE:
            assert_duration_within_pct(out, expected_s=self.DURATION_S, pct=2.0)


if __name__ == "__main__":
    unittest.main()
