"""Audit Q2.19 — F5 server ref_audio_b64 validation.

Pre-fix the F5 server didn't validate the decoded blob's length or
WAV magic — chatterbox + indicf5 already did per the 2026-05-10
hardening. Without this guard, F5TTS.infer() crashes deep inside
torchaudio with "stream is empty" 30s into the call instead of a
clear 400 to the laptop client right away.

Tests exercise: cloud/tts-f5/server.py
"""
from __future__ import annotations

import base64
import importlib.util
import sys
import unittest
from pathlib import Path


def _import_f5_server_module():
    fake = sys.modules.copy()
    for mod_name in ("torch", "torchaudio", "soundfile", "numpy",
                     "f5_tts", "f5_tts.api", "f5_tts.infer",
                     "f5_tts.infer.utils_infer",
                     "handler"):
        if mod_name not in sys.modules:
            stub = type(sys)(mod_name)
            sys.modules[mod_name] = stub
    # Inject the symbols server.py imports from `handler`.
    sys.modules["handler"].SUPPORTED_MODELS = {}  # type: ignore[attr-defined]
    sys.modules["handler"].synthesize = lambda **k: None  # type: ignore[attr-defined]

    spec = importlib.util.spec_from_file_location(
        "ytfactory_test_f5_server",
        Path(__file__).resolve().parent.parent / "cloud" / "tts-f5" / "server.py",
    )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        sys.modules.clear()
        sys.modules.update(fake)
        raise
    return module


class TestF5RefAudioToPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _import_f5_server_module()

    def test_empty_b64_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            self.module._ref_audio_to_path("")
        self.assertIn("empty ref_audio_b64", str(ctx.exception))

    def test_too_short_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            self.module._ref_audio_to_path(base64.b64encode(b"abc").decode())
        self.assertIn("too small", str(ctx.exception))

    def test_non_riff_raises_value_error(self):
        bad = b"X" * 50  # 50 bytes, no RIFF prefix.
        with self.assertRaises(ValueError) as ctx:
            self.module._ref_audio_to_path(base64.b64encode(bad).decode())
        self.assertIn("RIFF", str(ctx.exception))

    def test_valid_riff_header_is_accepted(self):
        # 44 bytes minimal valid-shaped WAV header (the file body is
        # garbage, but we only validate the header here).
        riff = b"RIFF" + (0).to_bytes(4, "little") + b"WAVE" + (b"\x00" * 32)
        self.assertEqual(len(riff), 44)
        path = self.module._ref_audio_to_path(base64.b64encode(riff).decode())
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
