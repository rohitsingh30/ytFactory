"""Tests for pipeline.voice.voice_catalog — 100% line + branch coverage.

Strategy: use real tempfiles for catalog YAML so path interactions are
realistic. Patch _load_catalog via lru_cache clear + real I/O where possible,
or inject catalog dicts directly via patch() when needed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


def _clear_cache():
    from pipeline.voice import voice_catalog
    voice_catalog._load_catalog.cache_clear()


class TestLooksLikePath(unittest.TestCase):
    def _fn(self, v):
        from pipeline.voice.voice_catalog import _looks_like_path
        return _looks_like_path(v)

    def test_contains_slash(self):
        self.assertTrue(self._fn("pipeline/voice_refs/sarah.wav"))

    def test_wav_suffix(self):
        self.assertTrue(self._fn("sarah.wav"))

    def test_plain_name(self):
        self.assertFalse(self._fn("hindi-female-storyteller"))

    def test_empty_string(self):
        self.assertFalse(self._fn(""))


class TestLoadCatalog(unittest.TestCase):
    def setUp(self):
        _clear_cache()

    def tearDown(self):
        _clear_cache()

    def test_missing_catalog_returns_empty_and_warns(self):
        from pipeline.voice.voice_catalog import _load_catalog
        with tempfile.TemporaryDirectory() as tmp:
            # No catalog.yaml created → should warn + return {}
            with patch("pipeline.voice.voice_catalog.logger") as mock_log:
                result = _load_catalog(tmp)
        self.assertEqual(result, {})
        mock_log.warning.assert_called_once()
        _clear_cache()

    def test_empty_yaml_returns_empty(self):
        from pipeline.voice.voice_catalog import _load_catalog
        with tempfile.TemporaryDirectory() as tmp:
            cat_dir = Path(tmp) / "pipeline" / "voice_refs"
            cat_dir.mkdir(parents=True)
            (cat_dir / "catalog.yaml").write_text("")
            result = _load_catalog(tmp)
        self.assertEqual(result, {})
        _clear_cache()

    def test_valid_entry_loaded(self):
        from pipeline.voice.voice_catalog import _load_catalog, VoiceEntry
        with tempfile.TemporaryDirectory() as tmp:
            voice_refs = Path(tmp) / "pipeline" / "voice_refs"
            voice_refs.mkdir(parents=True)
            # Create the WAV file the catalog points to
            (voice_refs / "test.wav").write_bytes(b"RIFF")
            catalog_yaml = (
                "voices:\n"
                "  test-voice:\n"
                "    path: pipeline/voice_refs/test.wav\n"
                "    transcript: hello world\n"
                "    language: en\n"
                "    register: storyteller\n"
                "    duration_s: 5.5\n"
                "    use_cases:\n"
                "      - narration\n"
                "    notes: a note\n"
            )
            (voice_refs / "catalog.yaml").write_text(catalog_yaml)
            result = _load_catalog(tmp)
        self.assertIn("test-voice", result)
        e = result["test-voice"]
        self.assertEqual(e.language, "en")
        self.assertEqual(e.duration_s, 5.5)
        self.assertEqual(e.use_cases, ("narration",))
        self.assertEqual(e.notes, "a note")
        _clear_cache()

    def test_missing_wav_skips_and_warns(self):
        from pipeline.voice.voice_catalog import _load_catalog
        with tempfile.TemporaryDirectory() as tmp:
            voice_refs = Path(tmp) / "pipeline" / "voice_refs"
            voice_refs.mkdir(parents=True)
            # Catalog points at a WAV that does NOT exist
            catalog_yaml = (
                "voices:\n"
                "  missing-voice:\n"
                "    path: pipeline/voice_refs/missing.wav\n"
                "    transcript: hello\n"
            )
            (voice_refs / "catalog.yaml").write_text(catalog_yaml)
            with patch("pipeline.voice.voice_catalog.logger") as mock_log:
                result = _load_catalog(tmp)
        self.assertEqual(result, {})
        mock_log.warning.assert_called_once()
        _clear_cache()

    def test_entry_defaults(self):
        """Optional fields should fall back to neutral values."""
        from pipeline.voice.voice_catalog import _load_catalog
        with tempfile.TemporaryDirectory() as tmp:
            voice_refs = Path(tmp) / "pipeline" / "voice_refs"
            voice_refs.mkdir(parents=True)
            (voice_refs / "bare.wav").write_bytes(b"RIFF")
            catalog_yaml = (
                "voices:\n"
                "  bare-voice:\n"
                "    path: pipeline/voice_refs/bare.wav\n"
            )
            (voice_refs / "catalog.yaml").write_text(catalog_yaml)
            result = _load_catalog(tmp)
        e = result["bare-voice"]
        self.assertEqual(e.transcript, "")
        self.assertEqual(e.language, "")
        self.assertEqual(e.register, "")
        self.assertEqual(e.duration_s, 0.0)
        self.assertEqual(e.use_cases, ())
        self.assertEqual(e.notes, "")
        _clear_cache()


class TestResolveVoice(unittest.TestCase):
    def setUp(self):
        _clear_cache()

    def tearDown(self):
        _clear_cache()

    def test_empty_string(self):
        from pipeline.voice.voice_catalog import resolve_voice
        path, t = resolve_voice("", "/any/root")
        self.assertIsNone(path)
        self.assertEqual(t, "")

    def test_path_style_stem_txt(self):
        """Path-style: prefers <stem>.txt sidecar."""
        from pipeline.voice.voice_catalog import resolve_voice
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "ref.wav"
            wav.write_bytes(b"RIFF")
            (Path(tmp) / "ref.txt").write_text("stem transcript")
            result_path, transcript = resolve_voice("ref.wav", tmp)
        self.assertEqual(transcript, "stem transcript")
        self.assertTrue(str(result_path).endswith("ref.wav"))

    def test_path_style_ref_txt_fallback(self):
        """Path-style: falls back to ref.txt when stem.txt absent."""
        from pipeline.voice.voice_catalog import resolve_voice
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "voices"
            sub.mkdir()
            wav = sub / "voice.wav"
            wav.write_bytes(b"RIFF")
            (sub / "ref.txt").write_text("fallback")
            result_path, transcript = resolve_voice(
                str(wav.relative_to(tmp)), tmp
            )
        self.assertEqual(transcript, "fallback")

    def test_path_style_no_sidecar(self):
        """Path-style: empty transcript when no sidecar exists."""
        from pipeline.voice.voice_catalog import resolve_voice
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "voice.wav"
            wav.write_bytes(b"RIFF")
            _, transcript = resolve_voice("voice.wav", tmp)
        self.assertEqual(transcript, "")

    def test_name_style_found(self):
        from pipeline.voice.voice_catalog import resolve_voice, VoiceEntry
        fake_path = MagicMock(spec=Path)
        entry = VoiceEntry(
            name="test-voice", path=fake_path, transcript="hello",
            language="en", register="", duration_s=5.0, use_cases=()
        )
        with patch("pipeline.voice.voice_catalog._load_catalog",
                   return_value={"test-voice": entry}):
            path, transcript = resolve_voice("test-voice", "/root")
        self.assertIs(path, fake_path)
        self.assertEqual(transcript, "hello")

    def test_name_style_not_found_empty_catalog(self):
        from pipeline.voice.voice_catalog import resolve_voice
        with patch("pipeline.voice.voice_catalog._load_catalog", return_value={}):
            with self.assertRaises(ValueError) as ctx:
                resolve_voice("nonexistent", "/root")
        self.assertIn("(empty catalog)", str(ctx.exception))

    def test_name_style_not_found_lists_available(self):
        from pipeline.voice.voice_catalog import resolve_voice, VoiceEntry
        existing = VoiceEntry(
            name="voice-a", path=MagicMock(spec=Path), transcript="",
            language="en", register="", duration_s=0.0, use_cases=()
        )
        with patch("pipeline.voice.voice_catalog._load_catalog",
                   return_value={"voice-a": existing}):
            with self.assertRaises(ValueError) as ctx:
                resolve_voice("voice-b", "/root")
        self.assertIn("voice-a", str(ctx.exception))


class TestListVoices(unittest.TestCase):
    def setUp(self):
        _clear_cache()

    def tearDown(self):
        _clear_cache()

    def test_empty(self):
        from pipeline.voice.voice_catalog import list_voices
        with patch("pipeline.voice.voice_catalog._load_catalog", return_value={}):
            self.assertEqual(list_voices("/root"), [])

    def test_populated(self):
        from pipeline.voice.voice_catalog import list_voices, VoiceEntry
        entry = VoiceEntry(
            name="v", path=MagicMock(spec=Path), transcript="t",
            language="en", register="", duration_s=1.0, use_cases=()
        )
        with patch("pipeline.voice.voice_catalog._load_catalog",
                   return_value={"v": entry}):
            result = list_voices("/root")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "v")


if __name__ == "__main__":
    unittest.main()
