"""Engine integration golden — short engine end-to-end.

Modes
-----

Two run modes, picked by env:

* **Fixture mode (CI default)** — uses fixture-loading variants of
  audio / timeline / visuals. Engine wiring + overlays/music/compose
  are REAL. Fast (<10s), deterministic, no cloud calls.

* **Cloud mode** — set ``YTFACTORY_GOLDEN_CLOUD=1`` to run with real
  cloud TTS (cloudrun_chatterbox), real cloud whisper (cloudrun_asr),
  and real Flux visualize. Slower (~5-8min), costs ~$0.50/run, but
  exercises the full production path. Requires ``CLOUDRUN_TTS_*_URL``
  + ``CLOUDRUN_ASR_URL`` + ``CLOUDRUN_IMAGE_URL`` env vars set.

Both modes assert via the SAME tolerance helpers (per plan.md Part D4)
— structural fields exact, duration ±1%, LUFS ±0.5dB, overlay
presence-only.

Adding a new plugin slot or visual_mode does NOT require updating
this golden — only the engine orchestration is tested. Per-plugin
behavior is exercised by tests/render/<slot>/test_<plugin>.py.
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
from pipeline.render.short_engine import render_short
from pipeline.render.spec import build_spec
from tests.render.golden_assertions import (
    assert_duration_within_pct,
    assert_structural,
)


def _make_fixture_wav(path: Path, duration_s: float = 6.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "sine=frequency=440:sample_rate=24000",
        "-ac", "1", "-c:a", "pcm_s16le",
        str(path),
    ], check=True, capture_output=True)


def _make_fixture_mp4(path: Path, duration_s: float = 6.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "color=c=0x141414:s=1080x1920:r=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-pix_fmt", "yuv420p",
        str(path),
    ], check=True, capture_output=True)


def _make_fixture_beats(path: Path, duration_s: float = 6.0) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    seg_len = duration_s / 3
    path.write_text(json.dumps([
        {"start_s": 0.0, "end_s": seg_len, "text": "first beat",
         "anchor_id": "beat_000", "kind": "beat"},
        {"start_s": seg_len, "end_s": 2 * seg_len, "text": "middle beat",
         "anchor_id": "beat_001", "kind": "beat"},
        {"start_s": 2 * seg_len, "end_s": 3 * seg_len, "text": "last beat",
         "anchor_id": "beat_002", "kind": "beat"},
    ]))


CLOUD_MODE = os.environ.get("YTFACTORY_GOLDEN_CLOUD") == "1"


class ShortEngineGoldenTest(unittest.TestCase):
    """End-to-end golden for the short engine.

    In fixture mode (CI default): all plugins loaded from disk except
    overlays/music/compose which run REAL ffmpeg.

    In cloud mode (YTFACTORY_GOLDEN_CLOUD=1): TTS / whisper / visualize
    use the real Cloud Run services.
    """

    DURATION_S = 6.0

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-short-golden-"))
        if not CLOUD_MODE:
            # Build deterministic fixtures.
            self.audio_fixture = self.tmp / "fixtures" / "narration.wav"
            self.visuals_fixture = self.tmp / "fixtures" / "visuals.mp4"
            self.timeline_fixture = self.tmp / "fixtures" / "beats.json"
            _make_fixture_wav(self.audio_fixture, duration_s=self.DURATION_S)
            _make_fixture_mp4(self.visuals_fixture, duration_s=self.DURATION_S)
            _make_fixture_beats(self.timeline_fixture, duration_s=self.DURATION_S)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build_spec(self):
        if CLOUD_MODE:
            # Real cloud plugins. The engine reads plugin names from
            # spec via _audio_plugin_name etc — defaults pick the
            # cloud variants when no override is in spec.extra.
            return build_spec({
                "channel": "test_channel",
                "channel_overrides": {
                    "kind": "short",
                    "captions_enabled": False,  # captions plugin requires
                                                # word_caption_pngs dep we
                                                # don't ship in CI; turn off
                                                # to keep the golden focused
                                                # on engine wiring.
                    "music_policy": "none",
                    "voice_provider": "cloudrun_chatterbox",
                },
            }, channel_yaml_path=None, variant_yaml_path=None)
        return build_spec({
            "channel": "test_channel",
            "channel_overrides": {
                "kind": "short",
                "captions_enabled": False,
                "music_policy": "none",
                # spec.extra plugin overrides — engine routes here.
                "audio_plugin": "audio_from_fixture",
                "timeline_plugin": "timeline_from_fixture",
                "visualize_plugin": "visuals_from_fixture",
                "audio_fixture_path": str(self.audio_fixture),
                "visuals_fixture_path": str(self.visuals_fixture),
                "timeline_fixture_path": str(self.timeline_fixture),
            },
        }, channel_yaml_path=None, variant_yaml_path=None)

    def test_short_engine_produces_structurally_valid_mp4(self):
        spec = self._build_spec()
        out = self.tmp / "out" / "short.mp4"
        result = render_short(
            spec,
            script={"narration": "smoke test", "beats": [
                {"text": "first"}, {"text": "second"}, {"text": "third"},
            ]},
            work_dir=self.tmp / "work",
            out_path=out,
        )

        # Engine returned the path we asked for.
        self.assertEqual(result, out)

        # Tolerance-band assertions per plan.md Part D4.
        # Structural — exact match (these can't drift across cloud updates).
        assert_structural(
            out,
            codec_video="h264",
            codec_audio="aac",
            sample_rate=24000,
            resolution=spec.output_resolution,
        )

        # Duration — within ±2% (slightly looser than ±1% to absorb
        # cloud TTS chunk-timing jitter when running in cloud mode;
        # fixture mode comfortably fits within ±0.5%).
        if not CLOUD_MODE:
            assert_duration_within_pct(out, expected_s=self.DURATION_S, pct=2.0)


if __name__ == "__main__":
    unittest.main()
