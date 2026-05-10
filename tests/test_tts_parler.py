"""Tests for pipeline/tts/parler.py — 100 % branch coverage.

Mocks: parler_tts, transformers, torch, soundfile, subprocess.
No real model loads, no GPU.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from tests._helpers import PROJECT_ROOT  # noqa: F401

import pipeline.tts.parler as _mod


def _reset():
    _mod._INDIC_PARLER_MODEL = None
    _mod._INDIC_PARLER_TOKENIZER = None
    _mod._INDIC_PARLER_DESC_TOKENIZER = None


class TestIndicParlerModelImportError(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_parler_tts_missing_raises_runtime_error(self):
        with patch.dict(sys.modules, {
            "parler_tts": None,
            "transformers": None,
        }):
            with self.assertRaises(RuntimeError) as ctx:
                _mod._indic_parler_model()
        msg = str(ctx.exception)
        self.assertIn("parler-tts", msg)
        self.assertIn("pip install", msg)
        self.assertIn("underlying error", msg)

    def test_gated_repo_os_error_raises_runtime_error(self):
        mock_parler_cls = MagicMock()
        mock_parler_cls.from_pretrained.side_effect = OSError("403 gated")
        mock_parler_mod = MagicMock()
        mock_parler_mod.ParlerTTSForConditionalGeneration = mock_parler_cls

        mock_auto_tokenizer = MagicMock()
        mock_transformers = MagicMock()
        mock_transformers.AutoTokenizer = mock_auto_tokenizer

        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = False

        with patch.dict(sys.modules, {
            "parler_tts": mock_parler_mod,
            "transformers": mock_transformers,
            "torch": mock_torch,
        }):
            with self.assertRaises(RuntimeError) as ctx:
                _mod._indic_parler_model()
        msg = str(ctx.exception)
        self.assertIn("gated-repo", msg)

    def test_gated_repo_403_in_message(self):
        mock_parler_cls = MagicMock()
        mock_parler_cls.from_pretrained.side_effect = OSError("HTTP 403 Forbidden")
        mock_parler_mod = MagicMock()
        mock_parler_mod.ParlerTTSForConditionalGeneration = mock_parler_cls

        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = False

        with patch.dict(sys.modules, {
            "parler_tts": mock_parler_mod,
            "transformers": MagicMock(),
            "torch": mock_torch,
        }):
            with self.assertRaises(RuntimeError) as ctx:
                _mod._indic_parler_model()
        self.assertIn("gated-repo", str(ctx.exception))

    def test_other_os_error_re_raised(self):
        mock_parler_cls = MagicMock()
        mock_parler_cls.from_pretrained.side_effect = OSError("disk full")
        mock_parler_mod = MagicMock()
        mock_parler_mod.ParlerTTSForConditionalGeneration = mock_parler_cls

        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = False

        with patch.dict(sys.modules, {
            "parler_tts": mock_parler_mod,
            "transformers": MagicMock(),
            "torch": mock_torch,
        }):
            with self.assertRaises(OSError):
                _mod._indic_parler_model()


class TestIndicParlerModelSuccess(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def _make_mocks(self, device="cpu"):
        mock_model = MagicMock()
        mock_model.config.text_encoder._name_or_path = "t5-base"
        mock_model.config.sampling_rate = 16000

        mock_parler_cls = MagicMock()
        mock_parler_cls.from_pretrained.return_value = mock_model

        mock_parler_mod = MagicMock()
        mock_parler_mod.ParlerTTSForConditionalGeneration = mock_parler_cls

        mock_tokenizer = MagicMock()
        mock_auto_tokenizer = MagicMock()
        mock_auto_tokenizer.from_pretrained.return_value = mock_tokenizer

        mock_transformers = MagicMock()
        mock_transformers.AutoTokenizer = mock_auto_tokenizer

        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = (device == "mps")

        return (mock_parler_cls, mock_parler_mod, mock_auto_tokenizer,
                mock_transformers, mock_torch, mock_model, mock_tokenizer)

    def test_success_returns_triple(self):
        (mock_parler_cls, mock_parler_mod, mock_auto_tokenizer,
         mock_transformers, mock_torch, mock_model, mock_tokenizer) = self._make_mocks()

        # model.to(device) returns itself
        mock_model.to.return_value = mock_model

        with patch.dict(sys.modules, {
            "parler_tts": mock_parler_mod,
            "transformers": mock_transformers,
            "torch": mock_torch,
        }):
            model, tok, desc_tok = _mod._indic_parler_model()

        self.assertIs(model, mock_model)
        self.assertIs(tok, mock_tokenizer)
        self.assertIs(desc_tok, mock_tokenizer)

    def test_loads_on_mps(self):
        (mock_parler_cls, mock_parler_mod, mock_auto_tokenizer,
         mock_transformers, mock_torch, mock_model, mock_tokenizer) = self._make_mocks(device="mps")
        mock_model.to.return_value = mock_model

        with patch.dict(sys.modules, {
            "parler_tts": mock_parler_mod,
            "transformers": mock_transformers,
            "torch": mock_torch,
        }):
            _mod._indic_parler_model()

        # Model should be moved to mps device
        mock_model.to.assert_called_with("mps")

    def test_cached_singleton_returned(self):
        cached_model = MagicMock()
        cached_tok = MagicMock()
        cached_desc_tok = MagicMock()
        _mod._INDIC_PARLER_MODEL = cached_model
        _mod._INDIC_PARLER_TOKENIZER = cached_tok
        _mod._INDIC_PARLER_DESC_TOKENIZER = cached_desc_tok

        m, t, d = _mod._indic_parler_model()
        self.assertIs(m, cached_model)
        self.assertIs(t, cached_tok)
        self.assertIs(d, cached_desc_tok)


class TestSynthIndicParler(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def _make_mock_model(self, sampling_rate=16000):
        mock_gen = MagicMock()
        mock_gen.cpu.return_value.numpy.return_value.squeeze.return_value = np.zeros(16000, dtype=np.float32)

        mock_model = MagicMock()
        mock_model.generate.return_value = mock_gen
        mock_model.config.sampling_rate = sampling_rate

        # next(model.parameters()).device
        mock_param = MagicMock()
        mock_param.device = "cpu"
        mock_model.parameters.return_value = iter([mock_param])

        mock_tok = MagicMock()
        mock_desc_tok = MagicMock()
        return mock_model, mock_tok, mock_desc_tok

    def test_speed_one_writes_directly(self):
        out = Path("/fake/parler.wav")
        mock_model, mock_tok, mock_desc_tok = self._make_mock_model()

        mock_torch = MagicMock()
        mock_torch.no_grad.return_value.__enter__ = MagicMock(return_value=None)
        mock_torch.no_grad.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(_mod, "_indic_parler_model",
                          return_value=(mock_model, mock_tok, mock_desc_tok)), \
             patch.dict(sys.modules, {"torch": mock_torch}), \
             patch("soundfile.write") as mock_sf, \
             patch("pathlib.Path.mkdir"):
            _mod._synth_indic_parler(
                text="नमस्कार",
                description=None,
                out_path=out,
                speed=1.0,
            )
        mock_sf.assert_called_once()
        # Path is written to out directly
        self.assertEqual(mock_sf.call_args[0][0], out)

    def test_empty_description_uses_default(self):
        out = Path("/fake/parler2.wav")
        mock_model, mock_tok, mock_desc_tok = self._make_mock_model()

        mock_torch = MagicMock()
        mock_torch.no_grad.return_value.__enter__ = MagicMock(return_value=None)
        mock_torch.no_grad.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(_mod, "_indic_parler_model",
                          return_value=(mock_model, mock_tok, mock_desc_tok)), \
             patch.dict(sys.modules, {"torch": mock_torch}), \
             patch("soundfile.write"), \
             patch("pathlib.Path.mkdir"):
            _mod._synth_indic_parler(
                text="test",
                description=None,  # should use default description
                out_path=out,
                speed=1.0,
            )
        # desc_tok was called with the default description
        desc_call = mock_desc_tok.call_args_list[0][0][0]
        self.assertIn("Sneha", desc_call)

    def test_speed_not_one_calls_ffmpeg(self):
        out = Path("/fake/parler3.wav")
        mock_model, mock_tok, mock_desc_tok = self._make_mock_model()
        mock_model.to.return_value = mock_model

        mock_torch = MagicMock()
        mock_torch.no_grad.return_value.__enter__ = MagicMock(return_value=None)
        mock_torch.no_grad.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(_mod, "_indic_parler_model",
                          return_value=(mock_model, mock_tok, mock_desc_tok)), \
             patch.dict(sys.modules, {"torch": mock_torch}), \
             patch("soundfile.write"), \
             patch("subprocess.run") as mock_sp, \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.rename"), \
             patch("pathlib.Path.unlink"):
            _mod._synth_indic_parler(
                text="test",
                description="Sneha speaks calmly.",
                out_path=out,
                speed=0.85,
            )
        mock_sp.assert_called_once()
        cmd = mock_sp.call_args[0][0]
        self.assertIn("ffmpeg", cmd)
        self.assertIn("atempo=0.8500", cmd[cmd.index("-filter:a") + 1])

    def test_returns_out_path(self):
        out = Path("/fake/result.wav")
        mock_model, mock_tok, mock_desc_tok = self._make_mock_model()

        mock_torch = MagicMock()
        mock_torch.no_grad.return_value.__enter__ = MagicMock(return_value=None)
        mock_torch.no_grad.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(_mod, "_indic_parler_model",
                          return_value=(mock_model, mock_tok, mock_desc_tok)), \
             patch.dict(sys.modules, {"torch": mock_torch}), \
             patch("soundfile.write"), \
             patch("pathlib.Path.mkdir"):
            result = _mod._synth_indic_parler(
                text="test",
                description="voice desc",
                out_path=out,
                speed=1.0,
            )
        self.assertEqual(result, out)


if __name__ == "__main__":
    unittest.main()
