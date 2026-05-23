"""Tests for pipeline/render/long_form.py:_ffmpeg + ffprobe hardening.

Pinning Tier 0 batch E (2026-05-14):
- _ffmpeg captures stderr and surfaces it on failure (R-30: pre-fix
  the error tail was overwritten by OTel metric dumps in the worker
  subprocess, leaving us with opaque "ffmpeg failed: ..." messages).
- _ffmpeg + ffprobe accept timeouts so a hung process doesn't stall
  the entire render (R-19, R-20).
- _probe_wav_params has a 30s timeout (R-19: TEL-FS-21 / TEL-EXEC-15
  surfaced renders that hung at this exact ffprobe call).
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock


from pipeline.render.shared import long_form_lib as lf


class TestFfmpegStderrCapture(unittest.TestCase):
    """_ffmpeg must surface ffmpeg stderr in the RuntimeError."""

    def test_failed_ffmpeg_includes_stderr_in_exception(self):
        # Simulate ffmpeg returning non-zero with a real-looking stderr.
        fake_proc = mock.MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = (
            "[concat @ 0x12345] DTS 1234 < 5678 out of order\n"
            "Conversion failed!"
        )
        fake_proc.stdout = ""
        with mock.patch.object(lf.subprocess, "run", return_value=fake_proc):
            with self.assertRaises(RuntimeError) as ctx:
                lf._ffmpeg(["-i", "in.wav", "out.wav"])
        msg = str(ctx.exception)
        self.assertIn("exit=1", msg)
        self.assertIn("DTS 1234 < 5678 out of order", msg)
        self.assertIn("Conversion failed!", msg)

    def test_successful_ffmpeg_returns_none(self):
        fake_proc = mock.MagicMock()
        fake_proc.returncode = 0
        fake_proc.stderr = ""
        fake_proc.stdout = ""
        with mock.patch.object(lf.subprocess, "run", return_value=fake_proc):
            # Must not raise.
            self.assertIsNone(lf._ffmpeg(["-i", "in.wav", "out.wav"]))

    def test_timeout_surfaces_typed_error(self):
        with mock.patch.object(
            lf.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=5),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                lf._ffmpeg(["-i", "in.wav", "out.wav"], timeout=5)
        self.assertIn("timed out after 5s", str(ctx.exception))

    def test_no_timeout_passes_none(self):
        """Default behaviour: no timeout (preserve existing multi-minute
        long-form compose calls that legitimately run for >30 min)."""
        fake_proc = mock.MagicMock(returncode=0, stderr="", stdout="")
        with mock.patch.object(lf.subprocess, "run", return_value=fake_proc) as m_run:
            lf._ffmpeg(["-i", "in.wav", "out.wav"])
        # subprocess.run was called with timeout=None (no cap).
        self.assertEqual(m_run.call_args.kwargs.get("timeout"), None)


class TestProbeWavParamsTimeout(unittest.TestCase):
    """_probe_wav_params must cap ffprobe at 30s.

    R-19 + TEL-FS-21 / TEL-EXEC-15: a hung ffprobe (corrupt WAV header,
    network mount stall) would block the entire long-form render.
    """

    def test_ffprobe_called_with_timeout(self):
        fake_proc = mock.MagicMock(returncode=0)
        fake_proc.stdout = "sample_rate=44100\nchannel_layout=mono\nchannels=1"
        with mock.patch.object(lf.subprocess, "run", return_value=fake_proc) as m_run:
            lf._probe_wav_params(Path("/tmp/x.wav"))
        # Called with timeout=30.
        self.assertEqual(m_run.call_args.kwargs.get("timeout"), 30)


if __name__ == "__main__":
    unittest.main()
