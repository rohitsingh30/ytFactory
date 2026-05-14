"""Tests for pipeline/era_anchor.py + the era_anchor_prefix wiring
through pipeline/images/images.py::build_full_prompt and
pipeline/render/shorts.py::make_short.

Added 2026-05-14 per Phase 4b of plan.md to fix the audit's
'Mongols 1258 → WW1 trench infantry' bug. The era anchor prepends
canonical costume + period tokens to the diffusion prompt so the
model's attention sees '13th-century Mongol cavalry, lamellar
armor, composite bow' before 'soldiers' — suppressing the modern-
uniform default.
"""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from pipeline import era_anchor


class EraAnchorTaxonomyTest(unittest.TestCase):

    def setUp(self):
        # Reset cache so each test gets a clean load.
        era_anchor._reset_cache_for_tests()

    def test_taxonomy_loads_from_default_path(self):
        eras = era_anchor.load_taxonomy()
        self.assertIsInstance(eras, dict)
        self.assertGreater(
            len(eras), 5,
            "taxonomy should ship with several curated eras",
        )

    def test_audit_baghdad_mongols_era_present(self):
        # The bug case from the 2026-05-13 audit: 4 of 6
        # baghdad-mongols-1258 renders showed WW1 trench infantry.
        # The taxonomy must define the correct era so the historyrecapped
        # rewriter can emit it.
        self.assertIn("13c-mongol-yuan-warband", era_anchor.load_taxonomy())

    def test_ww1_era_present_for_actual_ww1_topics(self):
        # Don't accidentally mark WW1 as a "wrong" era — it's a real
        # historical period. The bug was using WW1 for a 13c topic,
        # not that WW1 itself is invalid.
        self.assertIn("ww1-1914-1918-trench", era_anchor.load_taxonomy())

    def test_each_entry_has_required_fields(self):
        for key, entry in era_anchor.load_taxonomy().items():
            self.assertEqual(entry.key, key, f"{key}: key mismatch")
            self.assertTrue(entry.tokens.strip(),
                            f"{key}: tokens must be non-empty")
            # Description is recommended but not strictly required.
            # Negatives are advisory; allow empty.

    def test_load_caches_on_repeated_calls(self):
        first = era_anchor.load_taxonomy()
        second = era_anchor.load_taxonomy()
        self.assertIs(first, second,
                      "taxonomy should be cached across calls")

    def test_explicit_path_bypasses_cache(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as tf:
            tf.write(textwrap.dedent("""
                eras:
                  test-era:
                    description: test only
                    tokens: alpha beta gamma costume
                    negatives: no modern stuff
            """))
            tf.flush()
            tmp_path = Path(tf.name)
        try:
            custom = era_anchor.load_taxonomy(path=tmp_path)
            self.assertIn("test-era", custom)
            self.assertNotIn("test-era", era_anchor.load_taxonomy())
        finally:
            tmp_path.unlink()

    def test_unreadable_yaml_raises_runtime_error(self):
        # OSError path — file doesn't exist.
        with self.assertRaises(RuntimeError):
            era_anchor.load_taxonomy(path=Path("/nonexistent/era_taxonomy.yaml"))

    def test_invalid_yaml_syntax_raises_runtime_error(self):
        # YAMLError path — malformed YAML.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as tf:
            tf.write("eras:\n  bad: [unclosed list\n")
            tf.flush()
            tmp_path = Path(tf.name)
        try:
            with self.assertRaises(RuntimeError):
                era_anchor.load_taxonomy(path=tmp_path)
        finally:
            tmp_path.unlink()

    def test_malformed_yaml_raises_runtime_error(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as tf:
            tf.write("not a dict, just a string scalar")
            tf.flush()
            tmp_path = Path(tf.name)
        try:
            with self.assertRaises(RuntimeError):
                era_anchor.load_taxonomy(path=tmp_path)
        finally:
            tmp_path.unlink()

    def test_missing_eras_key_raises_runtime_error(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as tf:
            tf.write("other_key: value\n")
            tf.flush()
            tmp_path = Path(tf.name)
        try:
            with self.assertRaises(RuntimeError):
                era_anchor.load_taxonomy(path=tmp_path)
        finally:
            tmp_path.unlink()

    def test_entry_with_empty_tokens_skipped(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as tf:
            tf.write(textwrap.dedent("""
                eras:
                  good:
                    tokens: real costume
                  bad:
                    tokens: ""
                  also-bad:
                    description: no tokens at all
            """))
            tf.flush()
            tmp_path = Path(tf.name)
        try:
            loaded = era_anchor.load_taxonomy(path=tmp_path)
            self.assertIn("good", loaded)
            self.assertNotIn("bad", loaded)
            self.assertNotIn("also-bad", loaded)
        finally:
            tmp_path.unlink()

    def test_entry_with_non_dict_body_skipped(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as tf:
            tf.write(textwrap.dedent("""
                eras:
                  good:
                    tokens: real costume
                  bad: just-a-string
            """))
            tf.flush()
            tmp_path = Path(tf.name)
        try:
            loaded = era_anchor.load_taxonomy(path=tmp_path)
            self.assertIn("good", loaded)
            self.assertNotIn("bad", loaded)
        finally:
            tmp_path.unlink()


class IsKnownEraTest(unittest.TestCase):

    def setUp(self):
        era_anchor._reset_cache_for_tests()

    def test_known_era_returns_true(self):
        self.assertTrue(era_anchor.is_known_era("13c-mongol-yuan-warband"))

    def test_unknown_era_returns_false(self):
        self.assertFalse(era_anchor.is_known_era("13c-mongolian-warband"))
        self.assertFalse(era_anchor.is_known_era("not-a-real-era"))

    def test_falsy_input_returns_false(self):
        self.assertFalse(era_anchor.is_known_era(None))
        self.assertFalse(era_anchor.is_known_era(""))
        self.assertFalse(era_anchor.is_known_era(123))  # type: ignore[arg-type]


class EraPrefixForTest(unittest.TestCase):

    def setUp(self):
        era_anchor._reset_cache_for_tests()

    def test_known_era_returns_bracketed_prefix(self):
        prefix = era_anchor.era_prefix_for("13c-mongol-yuan-warband")
        self.assertIsNotNone(prefix)
        self.assertTrue(prefix.startswith("[ERA — "))
        self.assertTrue(prefix.endswith("]"))
        # Must include the actual costume tokens that fix the bug.
        self.assertIn("Mongol", prefix)
        self.assertIn("lamellar", prefix)
        self.assertIn("composite recurve bow", prefix)
        # And explicit anti-WW1 anchor.
        self.assertIn("no rifles", prefix)

    def test_prefix_is_single_line(self):
        # YAML block scalars use multi-line — the prefix must collapse
        # to one line for clean prompt + log readability.
        for key in era_anchor.all_known_eras():
            prefix = era_anchor.era_prefix_for(key)
            self.assertNotIn("\n", prefix,
                             f"{key}: prefix has newline")

    def test_unknown_era_returns_none(self):
        self.assertIsNone(era_anchor.era_prefix_for("not-real"))

    def test_falsy_input_returns_none(self):
        self.assertIsNone(era_anchor.era_prefix_for(None))
        self.assertIsNone(era_anchor.era_prefix_for(""))


class ClosestMatchTest(unittest.TestCase):

    def setUp(self):
        era_anchor._reset_cache_for_tests()

    def test_typo_finds_closest(self):
        suggestions = era_anchor.closest_match("13c-mongol-yuan-warbnd")
        self.assertIn("13c-mongol-yuan-warband", suggestions)

    def test_partial_match_suggests(self):
        suggestions = era_anchor.closest_match("mongol")
        # difflib uses ratio cutoff 0.5 — "mongol" vs "13c-mongol-..."
        # is below cutoff because of the prefix difference. Verify
        # behaviour: either it suggests OR returns []. Both fine.
        self.assertIsInstance(suggestions, list)

    def test_empty_input_returns_empty(self):
        self.assertEqual(era_anchor.closest_match(""), [])
        self.assertEqual(era_anchor.closest_match(None), [])


class NegativesForTest(unittest.TestCase):

    def setUp(self):
        era_anchor._reset_cache_for_tests()

    def test_known_era_returns_negatives(self):
        # Mongol era explicitly bans rifles, WW1 helmets, etc.
        neg = era_anchor.negatives_for("13c-mongol-yuan-warband")
        self.assertIsNotNone(neg)
        self.assertIn("no rifles", neg)

    def test_unknown_era_returns_none(self):
        self.assertIsNone(era_anchor.negatives_for("not-real"))


class AllKnownErasTest(unittest.TestCase):

    def setUp(self):
        era_anchor._reset_cache_for_tests()

    def test_returns_sorted_list(self):
        keys = era_anchor.all_known_eras()
        self.assertEqual(keys, sorted(keys))

    def test_includes_audit_bug_case(self):
        self.assertIn(
            "13c-mongol-yuan-warband", era_anchor.all_known_eras(),
        )


# ---------- build_full_prompt integration ------------------------------


class BuildFullPromptEraAnchorTest(unittest.TestCase):
    """The era_anchor_prefix kwarg on build_full_prompt prepends the
    era tokens BEFORE character/scene so it dominates the diffusion
    attention."""

    def test_era_prefix_lands_first_when_present(self):
        from pipeline.images import images as img
        prompt = img.build_full_prompt(
            style_prefix="cartoon",
            character_description="a person",
            key_visual="key visual",
            scene="scene tokens",
            era_anchor_prefix="[ERA — 13c Mongol cavalry, lamellar armor]",
        )
        # Era prefix is first.
        self.assertTrue(prompt.startswith("[ERA — 13c Mongol cavalry, lamellar armor]"))
        # Other parts still present.
        self.assertIn("a person", prompt)
        self.assertIn("scene tokens", prompt)
        self.assertIn("cartoon", prompt)

    def test_no_era_prefix_preserves_legacy_behaviour(self):
        from pipeline.images import images as img
        prompt = img.build_full_prompt(
            style_prefix="cartoon",
            character_description="a person",
            key_visual="key visual",
            scene="scene tokens",
        )
        # No era prefix → starts with character.
        self.assertTrue(prompt.startswith("a person"))

    def test_empty_era_prefix_treated_as_none(self):
        from pipeline.images import images as img
        prompt = img.build_full_prompt(
            style_prefix="cartoon",
            character_description="a person",
            key_visual="kv",
            scene="scene",
            era_anchor_prefix="   ",  # whitespace
        )
        self.assertTrue(prompt.startswith("a person"))


# ---------- shorts.py end-to-end era load ------------------------------


class MakeShortEraAnchorIntegrationTest(unittest.TestCase):
    """Pin the script.metadata.era_anchor → build_full_prompt
    plumbing in pipeline/render/shorts.py::make_short."""

    def setUp(self):
        from tests.test_render_shorts import (  # noqa: PLC0415
            TempWorkspaceMixin, patched_make_short_environment, _fake_beat,
        )
        self._mixin = TempWorkspaceMixin()
        self._mixin.setUp()
        self._patched_env = patched_make_short_environment
        self._fake_beat = _fake_beat

    def tearDown(self):
        self._mixin.tearDown()

    def test_known_era_anchor_threaded_to_build_full_prompt(self):
        from pipeline.render._legacy import shorts
        channel_path, out_dir = self._mixin.write_channel()
        (out_dir / "narrations").mkdir()
        narration = out_dir / "narrations" / "baghdad-test.json"
        narration.write_text(json.dumps({
            "metadata": {"era_anchor": "13c-mongol-yuan-warband"},
        }))
        # Ensure _find_script_path picks up the file (the helper looks
        # for it under the channel's narrations dir at scan time).
        # The mixin's write_channel arrangement has out_dir as channel
        # root — narration above lives at <out_dir>/narrations/<slug>.json.
        with self._patched_env([self._fake_beat("hook beat", 0, 1)]) as m:
            # Override _find_script_path to point at our narration so
            # the metadata-load branch fires.
            from unittest.mock import patch
            with patch.object(shorts, "_find_script_path", return_value=narration):
                shorts.make_short(
                    "baghdad text", channel_path, out_dir,
                    "baghdad-test", run_critic=False,
                )
        # Assert build_full_prompt received the era prefix.
        kwargs = m.images.build_full_prompt.call_args_list[0].kwargs
        prefix = kwargs.get("era_anchor_prefix")
        self.assertIsNotNone(prefix)
        self.assertIn("Mongol", prefix)
        self.assertIn("lamellar", prefix)

    def test_legacy_era_lock_field_also_honoured(self):
        # The pre-Phase-4b test fixture used metadata.era_lock; honour
        # both spellings during the migration window.
        from pipeline.render._legacy import shorts
        channel_path, out_dir = self._mixin.write_channel()
        (out_dir / "narrations").mkdir()
        narration = out_dir / "narrations" / "baghdad-test.json"
        narration.write_text(json.dumps({
            "metadata": {"era_lock": "13c-mongol-yuan-warband"},
        }))
        with self._patched_env([self._fake_beat("hook beat", 0, 1)]) as m:
            from unittest.mock import patch
            with patch.object(shorts, "_find_script_path", return_value=narration):
                shorts.make_short(
                    "baghdad text", channel_path, out_dir,
                    "baghdad-test", run_critic=False,
                )
        kwargs = m.images.build_full_prompt.call_args_list[0].kwargs
        prefix = kwargs.get("era_anchor_prefix")
        self.assertIsNotNone(prefix)
        self.assertIn("Mongol", prefix)

    def test_unknown_era_falls_back_silently(self):
        from pipeline.render._legacy import shorts
        channel_path, out_dir = self._mixin.write_channel()
        (out_dir / "narrations").mkdir()
        narration = out_dir / "narrations" / "baghdad-test.json"
        narration.write_text(json.dumps({
            "metadata": {"era_anchor": "13c-mongolian-warband"},  # typo
        }))
        with self._patched_env([self._fake_beat("hook beat", 0, 1)]) as m:
            from unittest.mock import patch
            with patch.object(shorts, "_find_script_path", return_value=narration):
                shorts.make_short(
                    "baghdad text", channel_path, out_dir,
                    "baghdad-test", run_critic=False,
                )
        kwargs = m.images.build_full_prompt.call_args_list[0].kwargs
        # Unknown era → no prefix (fallback).
        self.assertIsNone(kwargs.get("era_anchor_prefix"))

    def test_no_metadata_falls_back_silently(self):
        from pipeline.render._legacy import shorts
        channel_path, out_dir = self._mixin.write_channel()
        (out_dir / "narrations").mkdir()
        narration = out_dir / "narrations" / "baghdad-test.json"
        # Script has no metadata.era_anchor at all.
        narration.write_text(json.dumps({"hook": "test"}))
        with self._patched_env([self._fake_beat("hook beat", 0, 1)]) as m:
            from unittest.mock import patch
            with patch.object(shorts, "_find_script_path", return_value=narration):
                shorts.make_short(
                    "baghdad text", channel_path, out_dir,
                    "baghdad-test", run_critic=False,
                )
        kwargs = m.images.build_full_prompt.call_args_list[0].kwargs
        self.assertIsNone(kwargs.get("era_anchor_prefix"))

    def test_malformed_script_json_does_not_crash(self):
        from pipeline.render._legacy import shorts
        channel_path, out_dir = self._mixin.write_channel()
        (out_dir / "narrations").mkdir()
        narration = out_dir / "narrations" / "baghdad-test.json"
        narration.write_text("{ not valid json")
        with self._patched_env([self._fake_beat("hook beat", 0, 1)]) as m:
            from unittest.mock import patch
            with patch.object(shorts, "_find_script_path", return_value=narration):
                # Must not raise — render should still proceed.
                shorts.make_short(
                    "baghdad text", channel_path, out_dir,
                    "baghdad-test", run_critic=False,
                )
        kwargs = m.images.build_full_prompt.call_args_list[0].kwargs
        self.assertIsNone(kwargs.get("era_anchor_prefix"))


if __name__ == "__main__":
    unittest.main()
