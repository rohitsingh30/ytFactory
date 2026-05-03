"""Tests for pipeline.critic.regenerate_with_corrections — the file-level
patcher. The LLM-call path is integration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.critic import regenerate_with_corrections


def _save_dummy_png(path: Path) -> None:
    Image.new("RGB", (8, 8), (123, 45, 67)).save(path)


class RegenerateNoOpTest(unittest.TestCase):
    def test_empty_corrections_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self.assertEqual(regenerate_with_corrections(
                slug="x", cache_dir=cache, beat_corrections={},
            ), set())

    def test_missing_prompts_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            # No prompts.json present
            self.assertEqual(regenerate_with_corrections(
                slug="x", cache_dir=cache, beat_corrections={"0": "fix it"},
            ), set())


class RegeneratePatchTest(unittest.TestCase):
    def _setup(self, n_beats=3):
        tmp = Path(tempfile.mkdtemp())
        prompts = [
            {"key_visual": f"kv {i}", "scene": f"the character at scene {i}"}
            for i in range(n_beats)
        ]
        (tmp / "prompts.json").write_text(json.dumps(prompts))
        for i in range(n_beats):
            _save_dummy_png(tmp / f"img_{i:02d}.png")
        return tmp, prompts

    def test_appends_fix_to_scene(self):
        cache, original = self._setup()
        try:
            patched = regenerate_with_corrections(
                slug="s", cache_dir=cache,
                beat_corrections={"1": "make her angry, arms crossed"},
            )
            self.assertEqual(patched, {1})

            new = json.loads((cache / "prompts.json").read_text())
            # Beat 1 was patched — scene now appended
            self.assertIn(original[1]["scene"], new[1]["scene"])
            self.assertIn("arms crossed", new[1]["scene"])
            # Other beats untouched
            self.assertEqual(new[0]["scene"], original[0]["scene"])
            self.assertEqual(new[2]["scene"], original[2]["scene"])
        finally:
            for p in cache.glob("*"):
                p.unlink()
            cache.rmdir()

    def test_deletes_image_for_patched_beat(self):
        cache, _ = self._setup()
        try:
            self.assertTrue((cache / "img_01.png").exists())
            regenerate_with_corrections(
                slug="s", cache_dir=cache,
                beat_corrections={"1": "fix"},
            )
            # The patched beat's image is removed so the next
            # orchestrator pass regenerates it.
            self.assertFalse((cache / "img_01.png").exists())
            # Untouched images survive
            self.assertTrue((cache / "img_00.png").exists())
            self.assertTrue((cache / "img_02.png").exists())
        finally:
            for p in cache.glob("*"):
                p.unlink()
            cache.rmdir()

    def test_skips_out_of_range_index(self):
        cache, _ = self._setup(n_beats=2)
        try:
            patched = regenerate_with_corrections(
                slug="s", cache_dir=cache,
                beat_corrections={"7": "fix"},
            )
            self.assertEqual(patched, set())
        finally:
            for p in cache.glob("*"):
                p.unlink()
            cache.rmdir()

    def test_skips_non_integer_key(self):
        cache, _ = self._setup()
        try:
            patched = regenerate_with_corrections(
                slug="s", cache_dir=cache,
                beat_corrections={"not-a-number": "fix"},
            )
            self.assertEqual(patched, set())
        finally:
            for p in cache.glob("*"):
                p.unlink()
            cache.rmdir()

    def test_multiple_patches(self):
        cache, _ = self._setup()
        try:
            patched = regenerate_with_corrections(
                slug="s", cache_dir=cache,
                beat_corrections={"0": "first fix", "2": "third fix"},
            )
            self.assertEqual(patched, {0, 2})
            new = json.loads((cache / "prompts.json").read_text())
            self.assertIn("first fix", new[0]["scene"])
            self.assertIn("third fix", new[2]["scene"])
            # Beat 1 untouched
            self.assertNotIn("fix", new[1]["scene"])
        finally:
            for p in cache.glob("*"):
                p.unlink()
            cache.rmdir()


if __name__ == "__main__":
    unittest.main()
