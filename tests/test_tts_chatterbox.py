"""Tests for pipeline/tts/chatterbox.py — 100 % branch coverage.

Mocks: chatterbox.tts, torch, soundfile.write, subprocess.run.
No real model loads, no GPU, no real audio I/O.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import numpy as np

# Ensure project root is importable
from tests._helpers import PROJECT_ROOT  # noqa: F401

import pipeline.tts.chatterbox as _mod


def _reset():
    """Reset the module-level singleton before each test."""
    _mod._CHATTERBOX_MODEL = None


class TestChatterboxModelImportError(unittest.TestCase):
    """_chatterbox_model() raises RuntimeError when chatterbox-tts is missing."""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_import_error_raises_runtime_error(self):
        with patch.dict(sys.modules, {"chatterbox": None, "chatterbox.tts": None}):
            with self.assertRaises(RuntimeError) as ctx:
                _mod._chatterbox_model()
        msg = str(ctx.exception)
        self.assertIn("chatterbox-tts", msg)
        self.assertIn("pip install", msg)

    def test_import_error_message_contains_underlying_error(self):
        with patch.dict(sys.modules, {"chatterbox": None, "chatterbox.tts": None}):
            with self.assertRaises(RuntimeError) as ctx:
                _mod._chatterbox_model()
        self.assertIn("underlying error", str(ctx.exception))


class TestChatterboxModelLoadMPS(unittest.TestCase):
    """_chatterbox_model() loads on MPS device when available."""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_loads_on_mps_when_available(self):
        mock_chatterbox_tts_cls = MagicMock()
        mock_model = MagicMock()
        mock_chatterbox_tts_cls.from_pretrained.return_value = mock_model

        mock_chatterbox_tts_mod = MagicMock()
        mock_chatterbox_tts_mod.ChatterboxTTS = mock_chatterbox_tts_cls

        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = True

        with patch.dict(sys.modules, {
            "chatterbox": MagicMock(),
            "chatterbox.tts": mock_chatterbox_tts_mod,
            "torch": mock_torch,
        }):
            result = _mod._chatterbox_model()

        self.assertIs(result, mock_model)
        mock_chatterbox_tts_cls.from_pretrained.assert_called_once_with(device="mps")
        # Singleton is set
        self.assertIs(_mod._CHATTERBOX_MODEL, mock_model)

    def test_loads_on_cpu_when_mps_unavailable(self):
        mock_chatterbox_tts_cls = MagicMock()
        mock_model = MagicMock()
        mock_chatterbox_tts_cls.from_pretrained.return_value = mock_model

        mock_chatterbox_tts_mod = MagicMock()
        mock_chatterbox_tts_mod.ChatterboxTTS = mock_chatterbox_tts_cls

        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = False

        with patch.dict(sys.modules, {
            "chatterbox": MagicMock(),
            "chatterbox.tts": mock_chatterbox_tts_mod,
            "torch": mock_torch,
        }):
            result = _mod._chatterbox_model()

        mock_chatterbox_tts_cls.from_pretrained.assert_called_once_with(device="cpu")
        self.assertIs(result, mock_model)

    def test_cached_singleton_not_reloaded(self):
        cached_model = MagicMock()
        _mod._CHATTERBOX_MODEL = cached_model
        result = _mod._chatterbox_model()
        self.assertIs(result, cached_model)


class TestSynthChatterboxSpeedOne(unittest.TestCase):
    """_synth_chatterbox() writes WAV directly when speed ≈ 1.0."""

    def setUp(self):
        _reset()
        self.out_path = Path("/fake/out.wav")
        self.fake_audio = np.zeros(24000, dtype=np.float32)

    def tearDown(self):
        _reset()

    def _make_mock_model(self):
        mock_model = MagicMock()
        mock_wav = MagicMock()
        mock_wav.detach.return_value.cpu.return_value.numpy.return_value.squeeze.return_value = self.fake_audio
        mock_model.generate.return_value = mock_wav
        mock_model.sr = 24000
        return mock_model

    def test_speed_one_writes_directly(self):
        mock_model = self._make_mock_model()
        _mod._CHATTERBOX_MODEL = mock_model

        with patch("soundfile.write") as mock_sf_write, \
             patch("pathlib.Path.mkdir"):
            _mod._synth_chatterbox(
                text="Hello world",
                ref_audio_path="/fake/ref.wav",
                out_path=self.out_path,
                speed=1.0,
            )
            mock_sf_write.assert_called_once()
            args = mock_sf_write.call_args[0]
            self.assertEqual(args[0], self.out_path)
            self.assertEqual(args[2], 24000)

    def test_speed_slightly_off_one_still_direct(self):
        """Exactly 0.001 off from 1.0 → still direct write (threshold is 0.01)."""
        mock_model = self._make_mock_model()
        _mod._CHATTERBOX_MODEL = mock_model

        with patch("soundfile.write") as mock_sf_write, \
             patch("pathlib.Path.mkdir"):
            _mod._synth_chatterbox(
                text="hi",
                ref_audio_path="/fake/ref.wav",
                out_path=self.out_path,
                speed=1.005,
            )
            mock_sf_write.assert_called_once()

    def test_returns_out_path(self):
        out = Path("/fake/out.wav")
        mock_model = self._make_mock_model()
        _mod._CHATTERBOX_MODEL = mock_model

        with patch("soundfile.write"), patch("pathlib.Path.mkdir"):
            result = _mod._synth_chatterbox(
                text="hi",
                ref_audio_path="/fake/ref.wav",
                out_path=out,
                speed=1.0,
            )
        self.assertEqual(result, out)

    def test_exaggeration_and_cfg_weight_forwarded(self):
        mock_model = self._make_mock_model()
        _mod._CHATTERBOX_MODEL = mock_model

        with patch("soundfile.write"), patch("pathlib.Path.mkdir"):
            _mod._synth_chatterbox(
                text="hi",
                ref_audio_path="/fake/ref.wav",
                out_path=self.out_path,
                speed=1.0,
                exaggeration=0.8,
                cfg_weight=0.3,
            )
        mock_model.generate.assert_called_once_with(
            "hi",
            audio_prompt_path="/fake/ref.wav",
            exaggeration=0.8,
            cfg_weight=0.3,
        )


class TestSynthChatterboxFfmpegPath(unittest.TestCase):
    """_synth_chatterbox() uses ffmpeg when speed differs from 1.0."""

    def setUp(self):
        _reset()
        self.out_path = Path("/fake/out.wav")
        self.fake_audio = np.zeros(24000, dtype=np.float32)

    def tearDown(self):
        _reset()

    def _make_mock_model(self):
        mock_model = MagicMock()
        mock_wav = MagicMock()
        mock_wav.detach.return_value.cpu.return_value.numpy.return_value.squeeze.return_value = self.fake_audio
        mock_model.generate.return_value = mock_wav
        mock_model.sr = 24000
        return mock_model

    def test_speed_not_one_calls_ffmpeg(self):
        mock_model = self._make_mock_model()
        _mod._CHATTERBOX_MODEL = mock_model

        with patch("soundfile.write") as mock_sf, \
             patch("subprocess.run") as mock_run, \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.unlink"):
            _mod._synth_chatterbox(
                text="Hello",
                ref_audio_path="/fake/ref.wav",
                out_path=self.out_path,
                speed=0.9,
            )
            # soundfile.write called on the raw path (not out_path)
            mock_sf.assert_called_once()
            raw_args = mock_sf.call_args[0]
            self.assertIn(".raw.wav", str(raw_args[0]))

            # subprocess.run called with ffmpeg atempo
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertIn("ffmpeg", cmd)
            self.assertIn("atempo=0.9000", cmd[cmd.index("-filter:a") + 1])

    def test_speed_120_percent(self):
        mock_model = self._make_mock_model()
        _mod._CHATTERBOX_MODEL = mock_model

        with patch("soundfile.write"), \
             patch("subprocess.run"), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.unlink"):
            result = _mod._synth_chatterbox(
                text="Hello",
                ref_audio_path="/fake/ref.wav",
                out_path=self.out_path,
                speed=1.2,
            )
        self.assertEqual(result, self.out_path)


if __name__ == "__main__":
    unittest.main()
