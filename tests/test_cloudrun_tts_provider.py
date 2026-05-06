"""Tests for the cloud-run TTS provider — laptop-side glue.

Two layers (mirrors tests/test_audio_tts_providers.py):

1. **Dispatcher + fallback** — fast, no network. Verifies the
   `cloudrun_f5` route through `synthesize()` and that
   CloudRunUnavailable falls back to local f5_tts unless the
   DISABLE_FALLBACK env var is set.

2. **Live cloud smoke** — actually hits the Cloud Run service and
   confirms a real WAV comes back. Slow (~3 s warm, ~45 s cold).
   Auto-skipped unless ``CLOUDRUN_TTS_LIVE=1`` AND
   ``CLOUDRUN_TTS_URL`` is set::

       export CLOUDRUN_TTS_URL=https://ytfactory-tts-...run.app
       CLOUDRUN_TTS_LIVE=1 .venv/bin/python -m unittest \\
           tests.test_cloudrun_tts_provider
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import audio
from pipeline.tts.cloudrun import CloudRunUnavailable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REF_WAV = PROJECT_ROOT / "pipeline" / "voice_refs" / "sarah.wav"
REF_TXT = PROJECT_ROOT / "pipeline" / "voice_refs" / "sarah.txt"


# --------------------------------------------------------------- dispatcher


class TestCloudRunDispatch(unittest.TestCase):
    """Pure dispatcher — no network, no model loads."""

    def test_synthesize_routes_cloudrun_f5(self) -> None:
        """provider='cloudrun_f5' must dispatch to the cloud synth fn."""
        out = Path(tempfile.gettempdir()) / "dispatch-test.wav"
        with patch.object(audio, "_synth_cloudrun_f5") as mock_synth:
            mock_synth.return_value = out
            audio.synthesize(
                "Hello world.",
                voice=str(REF_WAV),
                out_path=out,
                provider="cloudrun_f5",
                ref_audio_text="Reference transcript.",
            )
            mock_synth.assert_called_once()
            kwargs = mock_synth.call_args.kwargs
            # Must forward the same fields as the local f5 route.
            self.assertEqual(kwargs["ref_audio_path"], str(REF_WAV))
            self.assertEqual(kwargs["ref_audio_text"], "Reference transcript.")

    def test_cloudrun_f5_requires_ref_text(self) -> None:
        """Missing ref_audio_text must raise the same error as f5_tts."""
        with self.assertRaises(ValueError) as ctx:
            audio.synthesize(
                "Hello world.",
                voice=str(REF_WAV),
                out_path=Path("/tmp/never.wav"),
                provider="cloudrun_f5",
                ref_audio_text=None,
            )
        self.assertIn("ref_audio_text", str(ctx.exception))

    def test_unknown_provider_lists_cloudrun_f5(self) -> None:
        """Error message for unknown provider must enumerate cloudrun_f5."""
        with self.assertRaises(ValueError) as ctx:
            audio.synthesize(
                "x", voice="v", out_path=Path("/tmp/never.wav"),
                provider="not_real",
            )
        self.assertIn("cloudrun_f5", str(ctx.exception))


# ----------------------------------------------------------------- fallback


class TestCloudRunFallback(unittest.TestCase):
    """When the cloud is unavailable, the call must fall back to local
    f5_tts unless explicitly disabled."""

    def test_5xx_falls_back_to_local_f5(self) -> None:
        # Simulate cloud failure; assert local f5 is called with same args.
        with patch("pipeline.tts.cloudrun._post_synth") as mock_post, \
             patch("pipeline.tts.f5._synth_f5_tts") as mock_local:
            mock_post.side_effect = CloudRunUnavailable("503 simulated")
            from pipeline.tts.cloudrun import _synth_cloudrun_f5

            out = Path("/tmp/fallback-test.wav")
            _synth_cloudrun_f5(
                text="hello",
                ref_audio_path=str(REF_WAV),
                ref_audio_text="ref",
                out_path=out,
                speed=1.0,
            )
            mock_local.assert_called_once()
            self.assertEqual(
                mock_local.call_args.kwargs["ref_audio_path"], str(REF_WAV),
            )

    def test_disable_fallback_env_var_re_raises(self) -> None:
        """CLOUDRUN_TTS_DISABLE_FALLBACK=1 must surface cloud failures."""
        with patch.dict(os.environ, {"CLOUDRUN_TTS_DISABLE_FALLBACK": "1"}), \
             patch("pipeline.tts.cloudrun._post_synth") as mock_post:
            mock_post.side_effect = CloudRunUnavailable("503 simulated")
            from pipeline.tts.cloudrun import _synth_cloudrun_f5

            with self.assertRaises(CloudRunUnavailable):
                _synth_cloudrun_f5(
                    text="hello",
                    ref_audio_path=str(REF_WAV),
                    ref_audio_text="ref",
                    out_path=Path("/tmp/nope.wav"),
                    speed=1.0,
                )


# --------------------------------------------------------------- live smoke


class TestCloudRunLiveSmoke(unittest.TestCase):
    """Live hit against the real Cloud Run service. Opt-in only."""

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("CLOUDRUN_TTS_LIVE", "") != "1":
            raise unittest.SkipTest(
                "set CLOUDRUN_TTS_LIVE=1 (and CLOUDRUN_TTS_URL) to enable"
            )
        if not os.environ.get("CLOUDRUN_TTS_URL", "").strip():
            raise unittest.SkipTest("CLOUDRUN_TTS_URL not set")
        if not REF_WAV.exists():
            raise unittest.SkipTest(f"missing ref WAV: {REF_WAV}")

    def test_short_synth_round_trip(self) -> None:
        """Render a 2-3s clip via the real cloud service. Confirms:
        - Auth flow works (gcloud ID token accepted)
        - Cloud writes a valid WAV
        - We materialise it correctly to disk
        - Output is non-trivial (has audio bytes)
        """
        out = Path(tempfile.gettempdir()) / "cloudrun-smoke-live.wav"
        out.unlink(missing_ok=True)
        audio.synthesize(
            "Cloud Run smoke test, one two three.",
            voice=str(REF_WAV),
            out_path=out,
            provider="cloudrun_f5",
            ref_audio_text=REF_TXT.read_text().strip(),
        )
        self.assertTrue(out.exists(), "cloud /synth produced no file")
        size = out.stat().st_size
        self.assertGreater(size, 50_000, f"cloud WAV suspiciously small: {size}")
        # First 4 bytes should be RIFF header.
        with open(out, "rb") as f:
            self.assertEqual(f.read(4), b"RIFF")


if __name__ == "__main__":
    unittest.main()
