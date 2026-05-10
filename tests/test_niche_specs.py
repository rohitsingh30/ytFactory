"""Tests for pipeline.niche_specs — file-system niche store."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import niche_specs
from pipeline.niche_specs import (
    NicheDoc,
    _channel_root,
    _niche_path,
    _niches_dir,
    delete_niche,
    get_niche,
    list_niches,
    save_niche,
    slugify_label,
)


def _minimal_doc(key="test_niche", **kwargs) -> NicheDoc:
    return NicheDoc(key=key, label="Test Niche", **kwargs)


class TestNicheDocModel(unittest.TestCase):
    def test_create_minimal(self):
        doc = NicheDoc(key="my_niche", label="My Niche")
        self.assertEqual(doc.key, "my_niche")
        self.assertEqual(doc.voice, "sarah")
        self.assertEqual(doc.format, "animated")

    def test_invalid_key_raises(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            NicheDoc(key="Bad Key!", label="x")

    def test_invalid_key_starts_with_underscore_raises(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            NicheDoc(key="_bad", label="x")

    def test_all_optional_fields(self):
        doc = NicheDoc(
            key="full_niche",
            label="Full",
            description="desc",
            prompt_style_guide="guide",
            length_kind="long",
            voice="am_michael",
            format="footage_only",
            source_kind="reddit",
            source_ref="https://example.com",
            hook_template="hook tmpl",
            closer_template="closer tmpl",
            image_style="oil painting",
            music_bed="lo_fi",
            created_by="backfill",
        )
        self.assertEqual(doc.length_kind, "long")
        self.assertEqual(doc.source_kind, "reddit")

    def test_extra_fields_ignored(self):
        doc = NicheDoc(key="niche1", label="N", target_length_s=55)
        self.assertFalse(hasattr(doc, "target_length_s"))


class TestChannelRootValidation(unittest.TestCase):
    def test_valid_key_returns_path(self):
        root = _channel_root("mystoriesanimated")
        self.assertIn("mystoriesanimated", str(root))

    def test_invalid_key_raises(self):
        with self.assertRaises(ValueError):
            _channel_root("bad-channel-key")


class TestNichePathHelpers(unittest.TestCase):
    def test_niches_dir(self):
        nd = _niches_dir("historyrecapped")
        self.assertTrue(str(nd).endswith("historyrecapped/niches"))

    def test_niche_path(self):
        p = _niche_path("historyrecapped", "history_short")
        self.assertTrue(str(p).endswith("history_short.json"))

    def test_invalid_niche_key_raises(self):
        with self.assertRaises(ValueError):
            _niche_path("historyrecapped", "Bad Key!")


class TestListNiches(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._patcher = patch.object(niche_specs, "PROJECT_ROOT", self._root)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._td.cleanup()

    def _niches_dir(self, channel):
        d = self._root / channel / "niches"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_returns_empty_when_no_dir(self):
        result = list_niches("nosuchchannel")
        self.assertEqual(result, [])

    def test_returns_sorted_by_label(self):
        nd = self._niches_dir("chan1")
        for key, label in [("b_niche", "Zebra"), ("a_niche", "Apple")]:
            doc = NicheDoc(key=key, label=label)
            (nd / f"{key}.json").write_text(doc.model_dump_json())
        result = list_niches("chan1")
        self.assertEqual([r.label for r in result], ["Apple", "Zebra"])

    def test_skips_malformed_json(self):
        nd = self._niches_dir("chan2")
        (nd / "bad.json").write_text("{not valid json!!!}")
        doc = NicheDoc(key="good_niche", label="Good")
        (nd / "good_niche.json").write_text(doc.model_dump_json())
        result = list_niches("chan2")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].key, "good_niche")

    def test_skips_invalid_schema(self):
        nd = self._niches_dir("chan3")
        (nd / "invalid_schema.json").write_text(json.dumps({"key": "x", "no_label": True}))
        result = list_niches("chan3")
        self.assertEqual(result, [])


class TestGetNiche(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._patcher = patch.object(niche_specs, "PROJECT_ROOT", self._root)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._td.cleanup()

    def _write(self, channel, doc: NicheDoc):
        nd = self._root / channel / "niches"
        nd.mkdir(parents=True, exist_ok=True)
        (nd / f"{doc.key}.json").write_text(doc.model_dump_json())

    def test_returns_none_when_missing(self):
        self.assertIsNone(get_niche("ch", "no_key"))

    def test_returns_doc_when_present(self):
        doc = NicheDoc(key="test_niche", label="Test")
        self._write("ch", doc)
        got = get_niche("ch", "test_niche")
        self.assertIsNotNone(got)
        self.assertEqual(got.label, "Test")  # type: ignore[union-attr]

    def test_returns_none_on_malformed(self):
        nd = self._root / "ch" / "niches"
        nd.mkdir(parents=True, exist_ok=True)
        (nd / "bad_niche.json").write_text("NOT JSON")
        self.assertIsNone(get_niche("ch", "bad_niche"))


class TestSaveNiche(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._patcher = patch.object(niche_specs, "PROJECT_ROOT", self._root)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._td.cleanup()

    def test_creates_dirs_and_writes(self):
        doc = NicheDoc(key="my_niche", label="My Niche")
        returned = save_niche("ch", doc)
        self.assertEqual(returned.key, "my_niche")
        p = self._root / "ch" / "niches" / "my_niche.json"
        self.assertTrue(p.exists())

    def test_overwrites_existing(self):
        doc = NicheDoc(key="my_niche", label="Old")
        save_niche("ch", doc)
        doc2 = NicheDoc(key="my_niche", label="New")
        save_niche("ch", doc2)
        got = get_niche("ch", "my_niche")
        self.assertEqual(got.label, "New")  # type: ignore[union-attr]

    def test_round_trips_cleanly(self):
        doc = NicheDoc(key="my_niche", label="Round Trip", source_kind="reddit")
        save_niche("ch", doc)
        got = get_niche("ch", "my_niche")
        self.assertEqual(got.source_kind, "reddit")  # type: ignore[union-attr]


class TestDeleteNiche(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._patcher = patch.object(niche_specs, "PROJECT_ROOT", self._root)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._td.cleanup()

    def test_returns_false_when_missing(self):
        self.assertFalse(delete_niche("ch", "no_key"))

    def test_deletes_and_returns_true(self):
        doc = NicheDoc(key="del_me", label="Delete Me")
        save_niche("ch", doc)
        result = delete_niche("ch", "del_me")
        self.assertTrue(result)
        self.assertIsNone(get_niche("ch", "del_me"))


class TestSlugifyLabel(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(slugify_label("My Niche"), "my_niche")

    def test_strips_special_chars(self):
        self.assertEqual(slugify_label("Hello, World!"), "hello_world")

    def test_collapses_underscores(self):
        self.assertEqual(slugify_label("foo  --  bar"), "foo_bar")

    def test_fallback_when_empty(self):
        self.assertEqual(slugify_label(""), "niche")

    def test_fallback_when_no_alnum(self):
        self.assertEqual(slugify_label("!!!"), "niche")

    def test_leading_non_alpha_stripped(self):
        result = slugify_label("123test")
        self.assertTrue(result[0].isalnum())

    def test_truncates_at_64(self):
        long_label = "word " * 20
        result = slugify_label(long_label)
        self.assertLessEqual(len(result), 64)

    def test_numbers_ok(self):
        self.assertEqual(slugify_label("top 5"), "top_5")


if __name__ == "__main__":
    unittest.main()
