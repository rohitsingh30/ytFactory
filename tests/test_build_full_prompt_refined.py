"""Tests for the refined-prompt mode of
``pipeline.images.images.build_full_prompt``.

Added 2026-05-14 alongside the prompt-refiner pre-step (originally
FLUX.2 [klein] DALL-E 3-style; rewritten 2026-05-23 for Z-Image-Turbo,
see pipeline/images/prompt_refiner.py:80 REFINER_VERSION='v2-zturbo').
The refiner emits structured fields
``{refined_visual, refined_scene, style_block}`` per beat;
``build_full_prompt`` accepts them as kwargs and assembles a prompt that
preserves the post-2026-05-14-audit invariants (era_anchor + character
description ALWAYS prepend, refiner output never bypasses them).

Also pins the per-beat fallback contract: if **any** of the three refined
fields is missing/empty, that beat falls back to the legacy
``(key_visual, scene, style_prefix)`` path.

Reference: ``pipeline/images/prompt_refiner.py``.
"""

from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401 — sets sys.path


class BuildFullPromptRefinedModeTest(unittest.TestCase):
    """Refined assembly path: all three refined_* kwargs truthy → use them
    instead of (key_visual, scene, style_prefix)."""

    def _build(self, **overrides):
        from pipeline.images import images as img
        kwargs = {
            "style_prefix": "cartoon, soft pastel palette",
            "character_description": "a 28-year-old man, brown hair, navy hoodie",
            "key_visual": "a coffee cup",
            "scene": "the character holding the cup",
            "era_anchor_prefix": "[ERA — modern 2020s living room]",
            "refined_visual": "a steaming coffee cup with cinnamon dust",
            "refined_scene": (
                "no readable text in image. medium shot, "
                "warm window backlight, hands cupping the mug, "
                "kitchen counter"
            ),
            "style_block": "Style: animated comic illustration. Mood: cosy morning.",
        }
        kwargs.update(overrides)
        return img.build_full_prompt(**kwargs)

    def test_refined_kwargs_use_refined_visual_and_scene(self):
        prompt = self._build()
        self.assertIn("a steaming coffee cup with cinnamon dust", prompt)
        self.assertIn("medium shot", prompt)
        # The original key_visual and scene should NOT appear when
        # refined mode wins.
        self.assertNotIn("the character holding the cup", prompt)
        self.assertNotIn("a coffee cup.", prompt + ".")

    def test_refined_uses_style_block_instead_of_style_prefix(self):
        prompt = self._build()
        self.assertIn("Style: animated comic illustration. Mood: cosy morning.", prompt)
        # Legacy style_prefix should NOT appear in refined mode.
        self.assertNotIn("soft pastel palette", prompt)

    def test_era_anchor_prefix_still_leads_in_refined_mode(self):
        """post-2026-05-14 audit invariant: era_anchor wins regardless."""
        prompt = self._build()
        self.assertTrue(
            prompt.startswith("[ERA — modern 2020s living room]"),
            f"era_anchor must lead even in refined mode; got: {prompt[:80]!r}",
        )

    def test_character_description_still_present_in_refined_mode(self):
        """post-2026-05-14 audit invariant: cast lock survives refiner."""
        prompt = self._build()
        self.assertIn("a 28-year-old man, brown hair, navy hoodie", prompt)
        # And it lands BEFORE the refined visual content (cross-attention
        # weight rationale — character identity must dominate scene tokens).
        cidx = prompt.index("a 28-year-old man")
        ridx = prompt.index("a steaming coffee cup")
        self.assertLess(cidx, ridx)

    def test_no_compel_weighting_artifact_in_refined_mode(self):
        # Legacy mode wraps key_visual in (text:1.4) for compel backends;
        # refined mode emits plain refined_visual without weighting parens
        # (FLUX doesn't parse compel syntax anyway).
        prompt = self._build()
        self.assertNotIn(":1.4)", prompt)
        self.assertNotIn("(a steaming coffee cup", prompt)


class BuildFullPromptRefinedFallbackTest(unittest.TestCase):
    """Per-beat fallback: when ANY of the three refined fields is missing
    or empty, the legacy path runs. Keeps the kill switch + partial-refine
    semantics intact."""

    def _build(self, refined_visual, refined_scene, style_block):
        from pipeline.images import images as img
        return img.build_full_prompt(
            style_prefix="legacy_style",
            character_description="legacy_char",
            key_visual="legacy_kv",
            scene="legacy_scene",
            era_anchor_prefix=None,
            refined_visual=refined_visual,
            refined_scene=refined_scene,
            style_block=style_block,
        )

    def test_all_three_truthy_uses_refined(self):
        prompt = self._build(
            refined_visual="REFINED_VISUAL",
            refined_scene="no readable text in image. REFINED_SCENE",
            style_block="Style: x. Mood: y.",
        )
        self.assertIn("REFINED_VISUAL", prompt)
        self.assertNotIn("legacy_kv", prompt)
        self.assertNotIn("legacy_style", prompt)

    def test_missing_refined_visual_falls_through(self):
        prompt = self._build(
            refined_visual=None,
            refined_scene="no readable text in image. ok",
            style_block="Style: x. Mood: y.",
        )
        self.assertIn("legacy_kv", prompt)
        self.assertIn("legacy_style", prompt)
        self.assertNotIn("REFINED_SCENE", prompt)

    def test_missing_refined_scene_falls_through(self):
        prompt = self._build(
            refined_visual="ok visual",
            refined_scene=None,
            style_block="Style: x. Mood: y.",
        )
        self.assertIn("legacy_kv", prompt)
        self.assertIn("legacy_scene", prompt)

    def test_missing_style_block_falls_through(self):
        prompt = self._build(
            refined_visual="ok visual",
            refined_scene="no readable text in image. ok",
            style_block=None,
        )
        self.assertIn("legacy_kv", prompt)
        self.assertIn("legacy_style", prompt)

    def test_empty_string_refined_visual_falls_through(self):
        # Empty string is the per-beat-fallback sentinel
        # (prompt_refiner returns {} → caller may pass empty strings to
        # build_full_prompt). Must behave identically to None.
        prompt = self._build(
            refined_visual="",
            refined_scene="no readable text in image. ok",
            style_block="Style: x. Mood: y.",
        )
        self.assertIn("legacy_kv", prompt)

    def test_whitespace_only_style_block_falls_through(self):
        prompt = self._build(
            refined_visual="ok",
            refined_scene="no readable text in image. ok",
            style_block="   ",
        )
        self.assertIn("legacy_style", prompt)

    def test_all_three_none_falls_through_to_legacy(self):
        # The kill-switch path: caller passes no refined kwargs → legacy
        # path runs exactly as before this feature shipped.
        prompt = self._build(
            refined_visual=None,
            refined_scene=None,
            style_block=None,
        )
        self.assertIn("legacy_kv", prompt)
        self.assertIn("legacy_scene", prompt)
        self.assertIn("legacy_style", prompt)


class BuildFullPromptRefinedBackwardCompatTest(unittest.TestCase):
    """Calling build_full_prompt without any of the new kwargs must
    produce byte-identical output to the legacy implementation. Pre-
    refiner callers must not see any behaviour change."""

    def test_legacy_call_signature_unchanged(self):
        from pipeline.images import images as img
        # Exact same kwargs the laptop CLI has called with since the
        # 2026-05-14 audit landed era_anchor_prefix.
        legacy = img.build_full_prompt(
            style_prefix="comic illustration",
            character_description="a man",
            key_visual="a key visual",
            scene="a scene",
            era_anchor_prefix="[ERA — 1990s]",
            key_visual_weight=1.4,
            weighted=True,
        )
        # Same call with the new kwargs explicitly = None must produce
        # the SAME string (the new kwargs default to None already, but
        # we pin both paths so a future refactor that changes defaults
        # gets caught).
        with_new_kwargs = img.build_full_prompt(
            style_prefix="comic illustration",
            character_description="a man",
            key_visual="a key visual",
            scene="a scene",
            era_anchor_prefix="[ERA — 1990s]",
            key_visual_weight=1.4,
            weighted=True,
            refined_visual=None,
            refined_scene=None,
            style_block=None,
        )
        self.assertEqual(legacy, with_new_kwargs)


if __name__ == "__main__":
    unittest.main()
