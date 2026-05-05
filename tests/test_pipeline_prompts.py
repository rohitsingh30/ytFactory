"""Tests for pipeline.prompts._validate_and_clean — schema enforcement
on the LLM's per-beat prompt array.

The actual `claude -p` call is integration; we exercise the validator.

`pipeline.prompts` imports `pipeline.images`, which transitively pulls
in torch + diffusers (~25s cold). We stub `pipeline.images` in
`sys.modules` BEFORE the import so this test stays fast.
"""

from __future__ import annotations

import sys
import types
import unittest

from tests._helpers import PROJECT_ROOT, FakeBeat  # noqa: F401


# Pre-stub pipeline.images so prompts.py's `from . import images, llm`
# resolves without triggering torch. _validate_and_clean uses both
# images.lint_prompt and images.strip_text_bait (the latter dedupes the
# text-bait stripper between the author path and the critic path —
# pipeline/critic.py:regenerate_with_corrections also calls it).
if "pipeline.images" not in sys.modules:
    _stub = types.ModuleType("pipeline.images")
    _stub.lint_prompt = lambda scene: []
    _stub.strip_text_bait = lambda scene: (scene, [])
    sys.modules["pipeline.images"] = _stub

from pipeline.prompts import _validate_and_clean  # noqa: E402


def _beats(n: int) -> list:
    return [FakeBeat(text=f"beat {i}", start=i * 1.5, end=(i + 1) * 1.5)
            for i in range(n)]


class ValidateShapeTest(unittest.TestCase):
    def test_happy_path(self):
        beats = _beats(3)
        raw = [
            {"key_visual": "kv0", "scene": "s0"},
            {"key_visual": "kv1", "scene": "s1"},
            {"key_visual": "kv2", "scene": "s2"},
        ]
        out = _validate_and_clean(raw, beats, opening_directives=None)
        self.assertEqual(len(out), 3)
        for i, item in enumerate(out):
            self.assertEqual(item["scene"], f"s{i}")
            self.assertEqual(item["key_visual"], f"kv{i}")

    def test_strips_whitespace(self):
        beats = _beats(1)
        raw = [{"key_visual": "  punchline  ", "scene": "  scene  "}]
        out = _validate_and_clean(raw, beats, opening_directives=None)
        self.assertEqual(out[0]["key_visual"], "punchline")
        self.assertEqual(out[0]["scene"], "scene")


class ValidateFailureTest(unittest.TestCase):
    def test_non_array_raises(self):
        with self.assertRaises(ValueError):
            _validate_and_clean({"not": "a list"}, _beats(1), None)

    def test_count_mismatch_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(
                [{"key_visual": "x", "scene": "y"}],
                _beats(3),
                None,
            )
        self.assertIn("counts must match", str(ctx.exception))

    def test_non_dict_item_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(
                ["just a string"],
                _beats(1),
                None,
            )
        self.assertIn("not an object", str(ctx.exception))

    def test_empty_scene_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(
                [{"key_visual": "x", "scene": "   "}],
                _beats(1),
                None,
            )
        self.assertIn("empty 'scene'", str(ctx.exception))

    def test_missing_scene_raises(self):
        with self.assertRaises(ValueError):
            _validate_and_clean(
                [{"key_visual": "x"}],  # no scene
                _beats(1),
                None,
            )


class ValidateMissingKeyVisualTolerantTest(unittest.TestCase):
    """key_visual can be empty; only `scene` is required."""

    def test_missing_key_visual_falls_back_to_empty(self):
        beats = _beats(1)
        out = _validate_and_clean(
            [{"scene": "the only thing that matters"}],
            beats, None,
        )
        self.assertEqual(out[0]["key_visual"], "")
        self.assertEqual(out[0]["scene"], "the only thing that matters")


class OpeningDirectivesTest(unittest.TestCase):
    """Beat 0 directive check is warning-only — should not raise."""

    def test_directives_pass_with_matching_tokens(self):
        beats = _beats(2)
        # Beat 0's scene contains "kitchen" — matches example tokens
        raw = [
            {"key_visual": "salad bowl", "scene": "the character standing in the kitchen at a table"},
            {"key_visual": "x", "scene": "y"},
        ]
        directives = {
            "required_concrete_tokens": 2,
            "example_tokens": ["kitchen", "table"],
        }
        # Should not raise
        out = _validate_and_clean(raw, beats, directives)
        self.assertEqual(len(out), 2)

    def test_directives_warning_does_not_raise(self):
        beats = _beats(1)
        raw = [{"key_visual": "kv", "scene": "abstract feelings sadness"}]
        directives = {
            "required_concrete_tokens": 2,
            "example_tokens": ["restaurant table", "wedding cake"],
        }
        # Doesn't raise — only warns to stdout
        out = _validate_and_clean(raw, beats, directives)
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
