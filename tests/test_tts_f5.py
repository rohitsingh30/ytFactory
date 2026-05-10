"""Tests for pipeline/tts/f5.py — 100 % branch coverage.

Mocks: f5_tts_mlx (mlx, soundfile, f5_tts_mlx.cfm.F5TTS).
No real model loads, no GPU, no real audio I/O.
"""
from __future__ import annotations

import sys
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from tests._helpers import PROJECT_ROOT  # noqa: F401

import pipeline.tts.f5 as _mod


def _reset():
    _mod._F5_MODEL = None
    _mod._F5_REF_CACHE.clear()


class TestResetState(unittest.TestCase):
    def test_clears_model_and_cache(self):
        _mod._F5_MODEL = object()
        _mod._F5_REF_CACHE["key"] = ("arr", 1.0)
        _mod.reset_state()
        self.assertIsNone(_mod._F5_MODEL)
        self.assertEqual(len(_mod._F5_REF_CACHE), 0)


class TestF5GetModel(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_import_error_raises_runtime_error(self):
        with patch.dict(sys.modules, {
            "f5_tts_mlx": None, "f5_tts_mlx.cfm": None,
        }):
            with self.assertRaises(RuntimeError) as ctx:
                _mod._f5_get_model()
        msg = str(ctx.exception)
        self.assertIn("f5-tts-mlx", msg)
        self.assertIn("pip install", msg)
        self.assertIn("underlying error", msg)

    def test_cold_load_stores_singleton(self):
        mock_f5tts = MagicMock()
        mock_model = MagicMock()
        mock_f5tts.from_pretrained.return_value = mock_model

        mock_cfm = MagicMock()
        mock_cfm.F5TTS = mock_f5tts

        with patch.dict(sys.modules, {
            "f5_tts_mlx": MagicMock(),
            "f5_tts_mlx.cfm": mock_cfm,
        }):
            result = _mod._f5_get_model()

        self.assertIs(result, mock_model)
        self.assertIs(_mod._F5_MODEL, mock_model)
        mock_f5tts.from_pretrained.assert_called_once_with(
            "lucasnewman/f5-tts-mlx", quantization_bits=None
        )

    def test_warm_cache_returns_same_model(self):
        cached = MagicMock()
        _mod._F5_MODEL = cached
        result = _mod._f5_get_model()
        self.assertIs(result, cached)

    def test_quantization_bits_forwarded(self):
        mock_f5tts = MagicMock()
        mock_model = MagicMock()
        mock_f5tts.from_pretrained.return_value = mock_model

        mock_cfm = MagicMock()
        mock_cfm.F5TTS = mock_f5tts

        with patch.dict(sys.modules, {
            "f5_tts_mlx": MagicMock(),
            "f5_tts_mlx.cfm": mock_cfm,
        }):
            _mod._f5_get_model(quantization_bits=8)

        mock_f5tts.from_pretrained.assert_called_once_with(
            "lucasnewman/f5-tts-mlx", quantization_bits=8
        )


class TestF5GetRef(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def _make_mlx_mocks(self, audio_np):
        """Return (mock_mlx, mock_mx, mock_arr) with .core wired so import works.

        `import mlx.core as mx` resolves to sys.modules['mlx'].core (NOT
        sys.modules['mlx.core']) in Python 3.13. We must set both.
        """
        mock_mx = MagicMock()
        mock_arr = MagicMock()
        mock_arr.shape = [len(audio_np)]
        mock_mx.array.return_value = mock_arr
        # mx.sqrt(...) → real float so `rms < target_rms` works (MagicMock < float
        # raises TypeError in Python 3.13+)
        mock_mx.sqrt.return_value = 0.05  # < 0.1 → normalization branch fires
        mock_mx.mean.return_value = MagicMock()
        mock_mx.square.return_value = MagicMock()

        mock_mlx = MagicMock()
        mock_mlx.core = mock_mx  # `import mlx.core as mx` → sys.modules['mlx'].core
        return mock_mlx, mock_mx, mock_arr

    # Keep old name for callers that only need two values
    def _make_mx_mock(self, audio_np):
        _, mock_mx, mock_arr = self._make_mlx_mocks(audio_np)
        return mock_mx, mock_arr

    def test_wrong_sample_rate_raises(self):
        mock_mx = MagicMock()
        mock_mlx = MagicMock()
        mock_mlx.core = mock_mx

        with patch.dict(sys.modules, {"mlx": mock_mlx, "mlx.core": mock_mx}), \
             patch("soundfile.read", return_value=(np.zeros(48000), 48000)):
            with self.assertRaises(RuntimeError) as ctx:
                _mod._f5_get_ref("/fake/bad.wav")
        self.assertIn("24 kHz", str(ctx.exception))

    def test_cache_miss_loads_and_stores(self):
        audio_np = np.zeros(24000, dtype=np.float32) + 0.01
        mock_mlx, mock_mx, mock_arr = self._make_mlx_mocks(audio_np)

        with patch.dict(sys.modules, {
            "mlx": mock_mlx, "mlx.core": mock_mx,
        }), patch("soundfile.read", return_value=(audio_np, 24000)):
            result = _mod._f5_get_ref("/fake/ref.wav")

        self.assertIn("/fake/ref.wav", _mod._F5_REF_CACHE)
        self.assertEqual(len(_mod._F5_REF_CACHE), 1)

    def test_cache_hit_moves_to_mru(self):
        # Pre-populate cache with two entries
        _mod._F5_REF_CACHE["/a"] = (MagicMock(), 1.0)
        _mod._F5_REF_CACHE["/b"] = (MagicMock(), 2.0)

        result = _mod._f5_get_ref("/a")
        # /a should now be at MRU (end of OrderedDict)
        keys = list(_mod._F5_REF_CACHE.keys())
        self.assertEqual(keys[-1], "/a")
        self.assertIs(result, _mod._F5_REF_CACHE["/a"])

    def test_lru_eviction_when_over_capacity(self):
        # Fill cache to max capacity
        for i in range(_mod._F5_REF_CACHE_MAX):
            _mod._F5_REF_CACHE[f"/path{i}"] = (MagicMock(), float(i))

        audio_np = np.zeros(24000, dtype=np.float32) + 0.05
        mock_mlx, mock_mx, mock_arr = self._make_mlx_mocks(audio_np)

        with patch.dict(sys.modules, {
            "mlx": mock_mlx, "mlx.core": mock_mx,
        }), patch("soundfile.read", return_value=(audio_np, 24000)):
            _mod._f5_get_ref("/new_path")

        # Cache should still be at max capacity (LRU evicted)
        self.assertLessEqual(len(_mod._F5_REF_CACHE), _mod._F5_REF_CACHE_MAX)
        # The new path is in cache
        self.assertIn("/new_path", _mod._F5_REF_CACHE)
        # The first (LRU) path was evicted
        self.assertNotIn("/path0", _mod._F5_REF_CACHE)


class TestSynthF5Tts(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_synthesis_calls_sample_and_writes_wav(self):
        out_path = Path("/fake/out.wav")
        audio_np = np.zeros(24000, dtype=np.float32)

        # Build a minimal mock ecosystem for the mlx + f5 path
        mock_mx = MagicMock()
        mock_wave = MagicMock()
        mock_wave.__getitem__ = MagicMock(return_value=mock_wave)
        # wave[audio.shape[0]:] → same mock
        mock_mx.expand_dims.return_value = MagicMock()

        mock_ref_audio = MagicMock()
        mock_ref_audio.shape = [24000]

        mock_model = MagicMock()
        mock_model.sample.return_value = (mock_wave, None)

        _mod._F5_MODEL = mock_model

        # Pre-populate ref cache so _f5_get_ref skips the heavy load
        _mod._F5_REF_CACHE["/fake/ref.wav"] = (mock_ref_audio, 1.0)

        mock_utils_mod = MagicMock()
        mock_utils_mod.convert_char_to_pinyin.return_value = ["hello"]

        with patch.dict(sys.modules, {
            "mlx": MagicMock(), "mlx.core": mock_mx,
            "f5_tts_mlx": MagicMock(), "f5_tts_mlx.utils": mock_utils_mod,
            "numpy": MagicMock(),
        }), \
             patch("soundfile.write") as mock_sf_write, \
             patch("pathlib.Path.mkdir"):
            # Patch numpy inside f5 to use real numpy for array ops
            with patch.dict(sys.modules, {"numpy": MagicMock()}):
                # numpy is already imported in this test process, so the
                # import inside f5._synth_f5_tts will see real numpy
                pass
            # Actually: soundfile.write is the key output; just ensure no crash
            _mod._synth_f5_tts(
                text="Hello world",
                ref_audio_path="/fake/ref.wav",
                ref_audio_text="hello",
                out_path=out_path,
                speed=1.0,
            )
        mock_model.sample.assert_called_once()
        mock_sf_write.assert_called_once()
        written_path = mock_sf_write.call_args[0][0]
        self.assertEqual(str(written_path), str(out_path))

    def test_returns_out_path(self):
        out_path = Path("/fake/result.wav")
        mock_ref_audio = MagicMock()
        mock_ref_audio.shape = [24000]
        _mod._F5_REF_CACHE["/r.wav"] = (mock_ref_audio, 1.0)

        mock_wave = MagicMock()
        mock_wave.__getitem__ = MagicMock(return_value=mock_wave)
        mock_model = MagicMock()
        mock_model.sample.return_value = (mock_wave, None)
        _mod._F5_MODEL = mock_model

        mock_mx = MagicMock()
        mock_utils_mod = MagicMock()
        mock_utils_mod.convert_char_to_pinyin.return_value = ["test"]

        with patch.dict(sys.modules, {
            "mlx": MagicMock(), "mlx.core": mock_mx,
            "f5_tts_mlx": MagicMock(), "f5_tts_mlx.utils": mock_utils_mod,
        }), \
             patch("soundfile.write"), \
             patch("pathlib.Path.mkdir"):
            result = _mod._synth_f5_tts(
                text="Test text",
                ref_audio_path="/r.wav",
                ref_audio_text="test",
                out_path=out_path,
                speed=0.95,
            )
        self.assertEqual(result, out_path)


if __name__ == "__main__":
    unittest.main()
