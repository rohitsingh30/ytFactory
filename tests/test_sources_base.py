"""Tests for pipeline.sources.base — RawStory, slugify, save_raw, load_raw."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.sources.base import RawStory, load_raw, save_raw, slugify


class SlugifyTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_accents_stripped(self):
        result = slugify("Café naïve")
        self.assertNotIn("é", result)
        self.assertNotIn("ï", result)
        self.assertIn("cafe", result)

    def test_non_ascii_stripped(self):
        result = slugify("日本語テスト")
        # All non-ASCII → stripped; result may be "untitled" or empty chars
        self.assertRegex(result, r"^[a-z0-9\-]*$")

    def test_special_chars_replaced_with_hyphen(self):
        self.assertEqual(slugify("Hello   World!@#"), "hello-world")

    def test_leading_trailing_hyphens_stripped(self):
        result = slugify("---hello---")
        self.assertFalse(result.startswith("-"))
        self.assertFalse(result.endswith("-"))

    def test_max_len_truncates(self):
        long_text = "a" * 100
        self.assertEqual(len(slugify(long_text, max_len=20)), 20)

    def test_empty_string_returns_untitled(self):
        self.assertEqual(slugify(""), "untitled")

    def test_all_special_chars_returns_untitled(self):
        self.assertEqual(slugify("!!!???"), "untitled")


class RawStoryTest(unittest.TestCase):
    def test_creation_with_defaults(self):
        s = RawStory(slug="s1", title="T", body="B", source="src", url="http://x")
        self.assertEqual(s.slug, "s1")
        self.assertEqual(s.metadata, {})

    def test_creation_with_metadata(self):
        s = RawStory(slug="s2", title="T2", body="B2", source="src2", url="http://y",
                     metadata={"k": "v"})
        self.assertEqual(s.metadata["k"], "v")


class SaveLoadRawTest(unittest.TestCase):
    def test_roundtrip(self):
        story = RawStory(
            slug="test-slug",
            title="Test Title",
            body="Test body content here.",
            source="test:source",
            url="https://example.com/test",
            metadata={"key": "val", "num": 42},
        )
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "raw"
            path = save_raw(story, dest)
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "test-slug.json")

            loaded = load_raw(path)
        self.assertEqual(loaded.slug, story.slug)
        self.assertEqual(loaded.title, story.title)
        self.assertEqual(loaded.body, story.body)
        self.assertEqual(loaded.source, story.source)
        self.assertEqual(loaded.url, story.url)
        self.assertEqual(loaded.metadata["key"], "val")
        self.assertEqual(loaded.metadata["num"], 42)

    def test_save_raw_creates_parent_dirs(self):
        story = RawStory(slug="s", title="T", body="B", source="src", url="http://u")
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "deep" / "nested" / "dir"
            path = save_raw(story, dest)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
