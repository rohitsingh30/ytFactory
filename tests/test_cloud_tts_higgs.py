"""Audit Q2.18 — `_apply_speed_post_process` actually applies the
requested speed factor. Pre-fix the higgs and indicparler servers
both accepted a `speed` parameter and silently dropped it.

Tests exercise: cloud/tts-higgs/server.py
"""
from __future__ import annotations

import importlib.util
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _import_higgs_server_module():
    """Higgs server depends on torch / boson_multimodal which we don't
    have in the laptop env. Stub the imports out so we can import the
    module just to test the small ffmpeg-based speed helper.
    """
    # Provide stub modules for the heavyweight deps.
    fake = sys.modules.copy()
    for mod_name in ("torch", "torchaudio", "boson_multimodal",
                     "boson_multimodal.data_types",
                     "boson_multimodal.serve", "boson_multimodal.serve.serve_engine"):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = type(sys)(mod_name)

    spec = importlib.util.spec_from_file_location(
        "ytfactory_test_higgs_server",
        Path(__file__).resolve().parent.parent / "cloud" / "tts-higgs" / "server.py",
    )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        # Restore module table on failure.
        sys.modules.clear()
        sys.modules.update(fake)
        raise
    return module


def _make_short_wav(path: Path, *, duration_s: float = 0.5,
                    sr: int = 16000) -> None:
    """Write a minimal valid mono 16-bit PCM WAV via ffmpeg's silent
    source. We use ffmpeg to avoid a numpy dep here."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=mono",
         "-t", str(duration_s), str(path)],
        check=True,
    )


class TestApplySpeedPostProcess(unittest.TestCase):
    """End-to-end: invoke ffmpeg via the helper, check output duration
    is approximately what speed says it should be."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("ffmpeg") is None:
            raise unittest.SkipTest("ffmpeg not on PATH; skipping audio tests")
        cls.module = _import_higgs_server_module()

    def _wav_duration_s(self, path: Path) -> float:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, check=True,
        )
        return float(out.stdout.decode().strip())

    def test_speed_1_0_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "in.wav"
            _make_short_wav(wav, duration_s=0.5)
            d_before = self._wav_duration_s(wav)
            self.module._apply_speed_post_process(wav, 1.0)
            self.assertAlmostEqual(self._wav_duration_s(wav), d_before, places=2)

    def test_speed_2_0_halves_duration(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "in.wav"
            _make_short_wav(wav, duration_s=1.0)
            self.module._apply_speed_post_process(wav, 2.0)
            self.assertAlmostEqual(self._wav_duration_s(wav), 0.5, delta=0.05)

    def test_speed_0_5_doubles_duration(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "in.wav"
            _make_short_wav(wav, duration_s=0.5)
            self.module._apply_speed_post_process(wav, 0.5)
            # 0.5x speed makes duration roughly double; ffmpeg's
            # atempo isn't perfectly precise, especially for short
            # clips, so allow ±20%.
            self.assertAlmostEqual(self._wav_duration_s(wav), 1.0, delta=0.15)

    def test_ffmpeg_failure_raises_runtime_error(self):
        # Point ffmpeg at a nonexistent input → non-zero exit.
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "missing.wav"
            with self.assertRaises(RuntimeError) as ctx:
                self.module._apply_speed_post_process(wav, 1.5)
            self.assertIn("ffmpeg atempo=1.5", str(ctx.exception))


class TestRefAudioToPath(unittest.TestCase):
    """Audit Q2.19 — F5 + Higgs ref_audio_b64 must be validated for
    length and RIFF magic, matching the chatterbox + indicf5 hardening
    from 2026-05-10. Pre-fix Higgs ate the empty/corrupt blob and
    crashed deep inside torchaudio with a useless error.
    """

    @classmethod
    def setUpClass(cls):
        cls.module = _import_higgs_server_module()

    def test_empty_b64_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            self.module._ref_audio_to_path("")
        self.assertIn("empty ref_audio_b64", str(ctx.exception))

    def test_too_short_raises_value_error(self):
        import base64
        with self.assertRaises(ValueError) as ctx:
            self.module._ref_audio_to_path(base64.b64encode(b"abc").decode())
        self.assertIn("too small", str(ctx.exception))

    def test_non_riff_raises_value_error(self):
        import base64
        # 44 bytes but no RIFF prefix.
        bad = b"X" * 50
        with self.assertRaises(ValueError) as ctx:
            self.module._ref_audio_to_path(base64.b64encode(bad).decode())
        self.assertIn("RIFF", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
