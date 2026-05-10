"""Tests for pipeline.critic.regenerate_with_corrections — the file-level
patcher. The LLM-call path is integration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm.critic import regenerate_with_corrections


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
                # Use a realistic visual-content correction. Short or
                # meta-only strings (e.g. "fix") are correctly rejected
                # by _sanitise_scene_patch + the second-pass
                # images.strip_text_bait length floor — see
                # pipeline/critic.py:regenerate_with_corrections.
                beat_corrections={"1": "make her angry, arms crossed, leaning forward"},
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


# ---- Additional coverage for sampling, critique, and patch sanitation ----

import shutil
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.llm import critic as cr


SCRATCH_CRITIC = PROJECT_ROOT / "tests" / ".scratch_critic"


class SanitiseAndSampleFramesTest(unittest.TestCase):
    def tearDown(self):
        shutil.rmtree(SCRATCH_CRITIC, ignore_errors=True)

    def test_sanitise_scene_patch_edge_cases(self):
        self.assertEqual(cr._sanitise_scene_patch("   "), "")
        self.assertEqual(cr._sanitise_scene_patch("tiny"), "")
        cleaned = cr._sanitise_scene_patch("Audio: say this. character leaning forward with clenched fists")
        self.assertEqual(cleaned, "character leaning forward with clenched fists")

    def test_sample_frames_success_and_failure(self):
        mp4 = SCRATCH_CRITIC / "video.mp4"
        frames_dir = SCRATCH_CRITIC / "frames"
        mp4.parent.mkdir(parents=True, exist_ok=True)
        mp4.write_text("mp4")
        def fake_run(cmd, capture_output, text):
            (frames_dir / "t_02.png").write_text("2")
            (frames_dir / "t_01.png").write_text("1")
            return SimpleNamespace(returncode=0, stderr="")
        with patch.object(cr.subprocess, "run", side_effect=fake_run):
            frames = cr._sample_frames(mp4, frames_dir)
        self.assertEqual([p.name for p in frames], ["t_01.png", "t_02.png"])
        with patch.object(cr.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="x" * 600)):
            with self.assertRaises(RuntimeError):
                cr._sample_frames(mp4, SCRATCH_CRITIC / "badframes")


class CritiqueShortTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH_CRITIC, ignore_errors=True)
        SCRATCH_CRITIC.mkdir(parents=True, exist_ok=True)
        self.mp4 = SCRATCH_CRITIC / "video.mp4"
        self.mp4.write_text("mp4")
        self.cache = SCRATCH_CRITIC / "cache"
        self.cache.mkdir()
        (self.cache / "beats.json").write_text("[]")
        self.out = SCRATCH_CRITIC / "out"

    def tearDown(self):
        shutil.rmtree(SCRATCH_CRITIC, ignore_errors=True)

    def test_missing_inputs_raise(self):
        with self.assertRaises(FileNotFoundError):
            cr.critique_short(slug="s", mp4_path=SCRATCH_CRITIC / "missing.mp4", cache_dir=self.cache, out_dir=self.out)
        (self.cache / "beats.json").unlink()
        with self.assertRaises(FileNotFoundError):
            cr.critique_short(slug="s", mp4_path=self.mp4, cache_dir=self.cache, out_dir=self.out)

    def test_success_writes_score_and_surfaces_system_corrections(self):
        raw = {"score": 6, "one_line_take": "needs work", "top_issues": [], "beat_corrections": {}, "system_corrections": [{"issue_class": "x", "where": "y", "fix": "z", "principle": "NEW"}], "highest_leverage_change": "fix"}
        frame = SCRATCH_CRITIC / "frame.png"
        frame.write_text("png")
        with patch.object(cr, "_sample_frames", return_value=[frame]), \
             patch.object(cr.llm, "model_for", return_value="opus"), \
             patch.object(cr.llm, "call_claude_cli", return_value=raw) as call:
            result = cr.critique_short(slug="slug", mp4_path=self.mp4, cache_dir=self.cache, out_dir=self.out)
        self.assertEqual(result["score"], 6)
        self.assertTrue((self.out / "slug.score.json").exists())
        self.assertEqual(call.call_args.kwargs["allowed_tools"], ["Read", "Bash"])
        self.assertIs(call.call_args.kwargs["json_schema"], cr._CRITIC_SCHEMA)

    def test_non_dict_llm_output_raises(self):
        with patch.object(cr, "_sample_frames", return_value=[]), \
             patch.object(cr.llm, "model_for", return_value="opus"), \
             patch.object(cr.llm, "call_claude_cli", return_value=[]):
            with self.assertRaises(ValueError):
                cr.critique_short(slug="slug", mp4_path=self.mp4, cache_dir=self.cache, out_dir=self.out)


class RegenerateSanitiserBranchesTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH_CRITIC, ignore_errors=True)
        SCRATCH_CRITIC.mkdir(parents=True, exist_ok=True)
        self.cache = SCRATCH_CRITIC / "cache"
        self.cache.mkdir()
        (self.cache / "prompts.json").write_text(json.dumps([{"key_visual": "kv", "scene": "original"}]))
        _save_dummy_png(self.cache / "img_00.png")

    def tearDown(self):
        shutil.rmtree(SCRATCH_CRITIC, ignore_errors=True)

    def test_meta_only_patch_rejected(self):
        patched = cr.regenerate_with_corrections(
            slug="s", cache_dir=self.cache,
            beat_corrections={"0": "Spoken closer rewrite to: 'this sentence is only spoken narration'"},
        )
        self.assertEqual(patched, set())

    def test_second_pass_rejects_empty_and_logs_removed_content(self):
        import inspect
        if "removed_secondpass" not in inspect.getsource(cr.regenerate_with_corrections):
            self.skipTest("second-pass strip not present in this source version")
        target = "pipeline.images.strip_text_bait" if "from .. import images" in inspect.getsource(cr.regenerate_with_corrections) else "pipeline.images.images.strip_text_bait"
        with patch(target, return_value=("", ["all text"])):
            patched = cr.regenerate_with_corrections(slug="s", cache_dir=self.cache, beat_corrections={"0": "visible character holding a sign"})
        self.assertEqual(patched, set())

    def test_second_pass_and_merged_scene_strips_still_patch(self):
        import inspect
        if "removed_secondpass" not in inspect.getsource(cr.regenerate_with_corrections):
            self.skipTest("second-pass strip not present in this source version")
        calls = []
        def strip(s):
            calls.append(s)
            if s == "visible character holding a sign":
                return ("visible character holding a prop", ["sign"])
            if s.startswith("original."):
                return ("merged clean scene", ["boundary text"])
            return (s, [])
        target = "pipeline.images.strip_text_bait" if "from .. import images" in inspect.getsource(cr.regenerate_with_corrections) else "pipeline.images.images.strip_text_bait"
        with patch(target, side_effect=strip):
            patched = cr.regenerate_with_corrections(slug="s", cache_dir=self.cache, beat_corrections={"0": "visible character holding a sign"})
        self.assertEqual(patched, {0})
        new = json.loads((self.cache / "prompts.json").read_text())
        self.assertEqual(new[0]["scene"], "merged clean scene")
        self.assertFalse((self.cache / "img_00.png").exists())


if __name__ == "__main__":
    unittest.main()
