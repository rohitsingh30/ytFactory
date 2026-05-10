"""Tests for pipeline/tts/styletts2.py — 100 % branch coverage.

Mocks: styletts2, soundfile, subprocess.
No real model loads.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from tests._helpers import PROJECT_ROOT  # noqa: F401

import pipeline.tts.styletts2 as _mod


def _reset():
    _mod._STYLETTS2_MODEL = None


class TestStyleTTS2ModelImportError(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_import_error_raises_runtime_error(self):
        with patch.dict(sys.modules, {
            "styletts2": None, "styletts2.tts": None,
        }):
            with self.assertRaises(RuntimeError) as ctx:
                _mod._styletts2_model()
        msg = str(ctx.exception)
        self.assertIn("styletts2", msg)
        self.assertIn("pip install", msg)
        self.assertIn("underlying error", msg)


class TestStyleTTS2ModelSuccess(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_cold_load_stores_singleton(self):
        mock_instance = MagicMock()

        mock_styletts2 = MagicMock()
        # from styletts2 import tts as _styletts2_tts → mock_styletts2.tts
        mock_styletts2.tts.StyleTTS2.return_value = mock_instance

        with patch.dict(sys.modules, {
            "styletts2": mock_styletts2,
            "styletts2.tts": mock_styletts2.tts,
        }):
            result = _mod._styletts2_model()

        self.assertIs(result, mock_instance)
        self.assertIs(_mod._STYLETTS2_MODEL, mock_instance)

    def test_warm_cache_returns_singleton(self):
        cached = MagicMock()
        _mod._STYLETTS2_MODEL = cached
        result = _mod._styletts2_model()
        self.assertIs(result, cached)


class TestSynthStyleTTS2SpeedOne(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def _make_mock_model(self):
        audio = np.zeros(24000, dtype=np.float32)
        mock_model = MagicMock()
        mock_model.inference.return_value = audio
        return mock_model

    def test_speed_one_writes_directly(self):
        out = Path("/fake/sty.wav")
        mock_model = self._make_mock_model()

        with patch.object(_mod, "_styletts2_model", return_value=mock_model), \
             patch("soundfile.write") as mock_sf, \
             patch("pathlib.Path.mkdir"):
            _mod._synth_styletts2(
                text="Hello world",
                ref_audio_path="/fake/ref.wav",
                out_path=out,
                speed=1.0,
            )
        mock_sf.assert_called_once()
        args = mock_sf.call_args[0]
        self.assertEqual(args[0], out)
        self.assertEqual(args[2], 24000)

    def test_default_params_forwarded_to_inference(self):
        out = Path("/fake/sty2.wav")
        mock_model = self._make_mock_model()

        with patch.object(_mod, "_styletts2_model", return_value=mock_model), \
             patch("soundfile.write"), \
             patch("pathlib.Path.mkdir"):
            _mod._synth_styletts2(
                text="Test narration",
                ref_audio_path="/ref.wav",
                out_path=out,
                speed=1.0,
            )
        mock_model.inference.assert_called_once_with(
            "Test narration",
            target_voice_path="/ref.wav",
            alpha=0.3,
            beta=0.7,
            diffusion_steps=7,
            embedding_scale=1.0,
        )

    def test_custom_params_forwarded(self):
        out = Path("/fake/sty3.wav")
        mock_model = self._make_mock_model()

        with patch.object(_mod, "_styletts2_model", return_value=mock_model), \
             patch("soundfile.write"), \
             patch("pathlib.Path.mkdir"):
            _mod._synth_styletts2(
                text="Test",
                ref_audio_path="/ref.wav",
                out_path=out,
                speed=1.0,
                alpha=0.5,
                beta=0.5,
                diffusion_steps=10,
                embedding_scale=1.2,
            )
        mock_model.inference.assert_called_once_with(
            "Test",
            target_voice_path="/ref.wav",
            alpha=0.5,
            beta=0.5,
            diffusion_steps=10,
            embedding_scale=1.2,
        )

    def test_returns_out_path(self):
        out = Path("/fake/result.wav")
        mock_model = self._make_mock_model()

        with patch.object(_mod, "_styletts2_model", return_value=mock_model), \
             patch("soundfile.write"), \
             patch("pathlib.Path.mkdir"):
            result = _mod._synth_styletts2(
                text="Test",
                ref_audio_path="/ref.wav",
                out_path=out,
                speed=1.0,
            )
        self.assertEqual(result, out)


class TestSynthStyleTTS2FfmpegPath(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def _make_mock_model(self):
        audio = np.zeros(24000, dtype=np.float32)
        mock_model = MagicMock()
        mock_model.inference.return_value = audio
        return mock_model

    def test_speed_not_one_calls_ffmpeg(self):
        out = Path("/fake/sty_speed.wav")
        mock_model = self._make_mock_model()

        with patch.object(_mod, "_styletts2_model", return_value=mock_model), \
             patch("soundfile.write"), \
             patch("subprocess.run") as mock_sp, \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.rename"), \
             patch("pathlib.Path.unlink"):
            _mod._synth_styletts2(
                text="Hello",
                ref_audio_path="/ref.wav",
                out_path=out,
                speed=0.9,
            )
        mock_sp.assert_called_once()
        cmd = mock_sp.call_args[0][0]
        self.assertIn("ffmpeg", cmd)
        self.assertIn("atempo=0.9000", cmd[cmd.index("-filter:a") + 1])

    def test_speed_120_ffmpeg(self):
        out = Path("/fake/sty_fast.wav")
        mock_model = self._make_mock_model()

        with patch.object(_mod, "_styletts2_model", return_value=mock_model), \
             patch("soundfile.write"), \
             patch("subprocess.run"), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.rename"), \
             patch("pathlib.Path.unlink"):
            result = _mod._synth_styletts2(
                text="Hello",
                ref_audio_path="/ref.wav",
                out_path=out,
                speed=1.2,
            )
        self.assertEqual(result, out)


if __name__ == "__main__":
    unittest.main()
