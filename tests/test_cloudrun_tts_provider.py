"""Tests for the cloud-run TTS provider — laptop-side glue.

Two layers:

1. **Dispatcher** — fast, no network. Verifies the surviving cloud
   providers (cloudrun_chatterbox, cloudrun_indicf5) route through
   ``audio.synthesize()``. Post 2026-05-16 cost-optimization sweep
   the f5/higgs/cosyvoice/indicparler/all-azure routes were dropped;
   restore from git history if revival is needed.

2. **Live cloud smoke** — actually hits the Cloud Run chatterbox
   service and confirms a real WAV comes back. Slow (~3 s warm,
   ~45 s cold). Auto-skipped unless ``CLOUDRUN_TTS_LIVE=1`` AND
   ``CLOUDRUN_TTS_CHATTERBOX_URL`` is set.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.audio import audio
from pipeline.tts.cloudrun import CloudRunUnavailable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REF_WAV = PROJECT_ROOT / "pipeline" / "voice_refs" / "sarah.wav"
REF_TXT = PROJECT_ROOT / "pipeline" / "voice_refs" / "sarah.txt"


# --------------------------------------------------------------- dispatcher


class TestCloudRunDispatch(unittest.TestCase):
    """Pure dispatcher — no network, no model loads."""

    def test_synthesize_routes_cloudrun_chatterbox(self) -> None:
        out = Path(tempfile.gettempdir()) / "dispatch-test.wav"
        with patch.object(audio, "_synth_cloudrun_chatterbox") as mock_synth:
            mock_synth.return_value = out
            audio.synthesize(
                "Hello world.",
                voice=str(REF_WAV),
                out_path=out,
                provider="cloudrun_chatterbox",
            )
            mock_synth.assert_called_once()
            kwargs = mock_synth.call_args.kwargs
            self.assertEqual(kwargs["ref_audio_path"], str(REF_WAV))

    def test_synthesize_routes_cloudrun_indicf5(self) -> None:
        out = Path(tempfile.gettempdir()) / "dispatch-test-if5.wav"
        with patch.object(audio, "_synth_cloudrun_indicf5") as mock_synth:
            mock_synth.return_value = out
            audio.synthesize(
                "नमस्कार",
                voice=str(REF_WAV),
                out_path=out,
                provider="cloudrun_indicf5",
                ref_audio_text="Reference transcript.",
            )
            mock_synth.assert_called_once()

    def test_cloudrun_indicf5_requires_ref_text(self) -> None:
        no_sidecar_wav = (
            PROJECT_ROOT / "pipeline" / "voice_refs" / "sports_male_intense.wav"
        )
        with self.assertRaises(ValueError) as ctx:
            audio.synthesize(
                "Hello world.",
                voice=str(no_sidecar_wav),
                out_path=Path("/tmp/never.wav"),
                provider="cloudrun_indicf5",
                ref_audio_text=None,
            )
        self.assertIn("ref_audio_text", str(ctx.exception))

    def test_unknown_provider_lists_surviving_providers(self) -> None:
        """Error message must enumerate the surviving providers."""
        with self.assertRaises(ValueError) as ctx:
            audio.synthesize(
                "x", voice="v", out_path=Path("/tmp/never.wav"),
                provider="not_real",
            )
        self.assertIn("cloudrun_chatterbox", str(ctx.exception))
        self.assertIn("cloudrun_indicf5", str(ctx.exception))


# ----------------------------------------------------------------- fallback


class TestCloudRunFallback(unittest.TestCase):
    """Cloud failures must surface ``CloudRunUnavailable``.

    As of 2026-05-09 (laptop nuclear cleanup) there is no local
    fallback for the cloud TTS path."""

    def test_5xx_re_raises_cloud_unavailable(self) -> None:
        with patch("pipeline.tts.cloudrun._post_synth") as mock_post:
            mock_post.side_effect = CloudRunUnavailable("503 simulated")
            from pipeline.tts.cloudrun import _synth_cloudrun_chatterbox

            with self.assertRaises(CloudRunUnavailable):
                _synth_cloudrun_chatterbox(
                    text="hello",
                    ref_audio_path=str(REF_WAV),
                    ref_audio_text="ref",
                    out_path=Path("/tmp/fallback-test.wav"),
                    speed=1.0,
                )


# --------------------------------------------------------------- live smoke


class TestCloudRunLiveSmoke(unittest.TestCase):
    """Live hit against the real Cloud Run chatterbox service. Opt-in only."""

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("CLOUDRUN_TTS_LIVE", "") != "1":
            raise unittest.SkipTest(
                "set CLOUDRUN_TTS_LIVE=1 (and CLOUDRUN_TTS_CHATTERBOX_URL) to enable"
            )
        if not os.environ.get("CLOUDRUN_TTS_CHATTERBOX_URL", "").strip():
            raise unittest.SkipTest("CLOUDRUN_TTS_CHATTERBOX_URL not set")
        if not REF_WAV.exists():
            raise unittest.SkipTest(f"missing ref WAV: {REF_WAV}")

    def test_short_synth_round_trip(self) -> None:
        out = Path(tempfile.gettempdir()) / "cloudrun-smoke-live.wav"
        out.unlink(missing_ok=True)
        audio.synthesize(
            "Cloud Run smoke test, one two three.",
            voice=str(REF_WAV),
            out_path=out,
            provider="cloudrun_chatterbox",
        )
        self.assertTrue(out.exists(), "cloud /synth produced no file")
        size = out.stat().st_size
        self.assertGreater(size, 50_000, f"cloud WAV suspiciously small: {size}")
        with open(out, "rb") as f:
            self.assertEqual(f.read(4), b"RIFF")


if __name__ == "__main__":
    unittest.main()
