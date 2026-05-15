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
        # Empty catalog AND nothing on disk under voice_refs/ →
        # raises with the new error format that lists both catalog +
        # on-disk options. Use a fresh tmpdir so the on-disk probe
        # finds nothing.
        with tempfile.TemporaryDirectory() as tmp:
            with patch("pipeline.voice.voice_catalog._load_catalog", return_value={}):
                with self.assertRaises(ValueError) as ctx:
                    resolve_voice("nonexistent", tmp)
        msg = str(ctx.exception)
        self.assertIn("(empty catalog)", msg)
        self.assertIn("(none)", msg)  # no on-disk voices either

    def test_name_style_not_found_lists_available(self):
        from pipeline.voice.voice_catalog import resolve_voice, VoiceEntry
        existing = VoiceEntry(
            name="voice-a", path=MagicMock(spec=Path), transcript="",
            language="en", register="", duration_s=0.0, use_cases=()
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch("pipeline.voice.voice_catalog._load_catalog",
                       return_value={"voice-a": existing}):
                with self.assertRaises(ValueError) as ctx:
                    resolve_voice("voice-b", tmp)
        self.assertIn("voice-a", str(ctx.exception))


class TestResolveVoiceBareNameFallback(unittest.TestCase):
    """Bare-name fallback (added 2026-05-15): the wizard form sends
    voice IDs like ``"sarah"`` (basenames discovered via filesystem
    listing of voice_refs/*.wav). The catalog doesn't always have
    these — they're legacy single-file refs predating the catalog.
    Pre-fix the dispatcher passed ``"sarah"`` straight through and
    chatterbox crashed with FileNotFoundError. The resolver now
    probes voice_refs/<name>.wav and voice_refs/<name>/ref.wav as
    a fallback before raising. Surfaced by job 215e411b canary."""

    def setUp(self):
        _clear_cache()

    def tearDown(self):
        _clear_cache()

    def _make_root(self, *, layout: dict[str, str]) -> tempfile.TemporaryDirectory:
        """layout maps relative-to-voice_refs paths → file content."""
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        vr = root / "pipeline" / "voice_refs"
        vr.mkdir(parents=True)
        for rel, content in layout.items():
            f = vr / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        return tmp

    def test_bare_name_resolves_to_flat_wav(self):
        from pipeline.voice.voice_catalog import resolve_voice
        tmp = self._make_root(layout={"sarah.wav": "fake-wav-bytes"})
        try:
            with patch("pipeline.voice.voice_catalog._load_catalog", return_value={}):
                wav, transcript = resolve_voice("sarah", tmp.name)
            self.assertIsNotNone(wav)
            self.assertTrue(wav.exists())
            self.assertEqual(wav.name, "sarah.wav")
            self.assertEqual(transcript, "")
        finally:
            tmp.cleanup()

    def test_bare_name_resolves_to_flat_wav_with_transcript_sidecar(self):
        from pipeline.voice.voice_catalog import resolve_voice
        tmp = self._make_root(layout={
            "sarah.wav": "fake-wav-bytes",
            "sarah.txt": "She speaks with a calm warm voice.",
        })
        try:
            with patch("pipeline.voice.voice_catalog._load_catalog", return_value={}):
                wav, transcript = resolve_voice("sarah", tmp.name)
            self.assertEqual(wav.name, "sarah.wav")
            self.assertEqual(transcript, "She speaks with a calm warm voice.")
        finally:
            tmp.cleanup()

    def test_bare_name_resolves_to_nested_ref_wav(self):
        # Newer per-voice-folder layout: voice_refs/<name>/ref.wav
        from pipeline.voice.voice_catalog import resolve_voice
        tmp = self._make_root(layout={
            "hindi-male-anurag-vardaan/ref.wav": "fake-wav-bytes",
            "hindi-male-anurag-vardaan/ref.txt": "नमस्ते मेरा नाम अनुराग है।",
        })
        try:
            with patch("pipeline.voice.voice_catalog._load_catalog", return_value={}):
                wav, transcript = resolve_voice("hindi-male-anurag-vardaan", tmp.name)
            self.assertEqual(wav.name, "ref.wav")
            self.assertIn("अनुराग", transcript)
        finally:
            tmp.cleanup()

    def test_bare_name_not_on_disk_raises_with_helpful_error(self):
        from pipeline.voice.voice_catalog import resolve_voice
        tmp = self._make_root(layout={"sarah.wav": "x", "michael.wav": "y"})
        try:
            with patch("pipeline.voice.voice_catalog._load_catalog", return_value={}):
                with self.assertRaises(ValueError) as ctx:
                    resolve_voice("totally-fake", tmp.name)
        finally:
            tmp.cleanup()
        msg = str(ctx.exception)
        # Error must list the available on-disk voices so the operator
        # can spot the typo.
        self.assertIn("sarah", msg)
        self.assertIn("michael", msg)
        # And tell them how to fix it.
        self.assertIn("catalog.yaml", msg)
        self.assertIn("totally-fake.wav", msg)

    def test_catalog_takes_priority_over_bare_name_fallback(self):
        # If the same name exists in BOTH catalog AND voice_refs/<n>.wav,
        # catalog wins (it has the curated transcript + tested config).
        from pipeline.voice.voice_catalog import resolve_voice, VoiceEntry
        tmp = self._make_root(layout={
            "sarah.wav": "fake-wav-bytes",  # bare-name fallback target
            "sarah.txt": "fallback transcript",
        })
        try:
            catalog_path = MagicMock(spec=Path)
            entry = VoiceEntry(
                name="sarah",
                path=catalog_path,
                transcript="catalog transcript wins",
                language="en", register="", duration_s=0.0, use_cases=(),
            )
            with patch("pipeline.voice.voice_catalog._load_catalog",
                       return_value={"sarah": entry}):
                wav, transcript = resolve_voice("sarah", tmp.name)
            # Catalog entry won — transcript is the catalog's, not the
            # filesystem sidecar's.
            self.assertEqual(transcript, "catalog transcript wins")
            self.assertIs(wav, catalog_path)
        finally:
            tmp.cleanup()


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


class CatalogYamlIntegrityTest(unittest.TestCase):
    """Pin the 2026-05-15 stale-WAV-path regression.

    Backstory: ``hindi-female-storyteller`` in
    ``pipeline/voice_refs/catalog.yaml`` pointed at
    ``pipeline/voice_refs/bench/hindutavaanimated__shorts__hindi_female_storyteller/ref.wav``
    — a path the 2026-05-07 voice-rotation refactor deleted. The
    catalog entry survived the deletion. Cloud render of job
    79cdca90 (HindutavaAnimated Krishna leela) failed with::

        voice 'hindi-female-storyteller' catalog entry points at
        missing WAV ... — skipping
        cloudrun_indicf5 provider requires `ref_audio_text`

    The catalog "skip on missing WAV" logic in voice_catalog.resolve_voice
    is the right runtime fallback, but the warning was buried in
    cloud logs and the actual symptom (missing ref_audio_text) was
    the contract-level error from the IndicF5 client — operator had
    to dig 2 layers deep to find the catalog drift.

    This test pins every catalog entry's path against on-disk
    presence so the next stale entry hard-fails at unit-test time.
    """

    def test_every_catalog_entry_points_at_extant_wav(self) -> None:
        from pathlib import Path
        from pipeline.voice.voice_catalog import _load_catalog
        project_root = Path(__file__).resolve().parents[1]
        # Real catalog (no monkey-patch).
        _load_catalog.cache_clear()
        catalog = _load_catalog(str(project_root))

        missing: list[tuple[str, str]] = []
        for name, entry in catalog.items():
            wav_rel = entry.path
            wav_abs = (project_root / wav_rel).resolve()
            if not wav_abs.exists():
                missing.append((name, str(wav_rel)))

        self.assertFalse(
            missing,
            f"Catalog entries reference missing WAV files (will cause "
            f"silent skip + downstream contract errors at render time): "
            f"{missing}. Either delete the catalog entry or alias it to "
            f"a real WAV path (see hindi-female-storyteller for the "
            f"alias pattern shipped 2026-05-15).",
        )


if __name__ == "__main__":
    unittest.main()
