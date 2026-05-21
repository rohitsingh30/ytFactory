"""Unit tests pinning the per-model prompt-shape contract.

Post 2026-05-16 cost-optimization sweep: only ``z_image_turbo`` remains
in :func:`best_prompt_for`. flux2_klein / flux2_dev / qwen_image
contracts are preserved in git history; restore those test classes if
those models are revived (see ``docs/cost_optimized_deploy.md``).
"""
from __future__ import annotations

import unittest

from pipeline.images.prompt_per_model import best_prompt_for
from pipeline.images.bake_off_beats import BEATS


class TestZImageTurboContract(unittest.TestCase):
    """z_image_turbo: Tencent distilled, concise front-loaded."""

    def setUp(self):
        beat = BEATS["mahabharat_arjun_battle"]
        self.out = best_prompt_for("z_image_turbo", **beat)

    def test_anti_text_is_prefix(self):
        p = self.out["prompt"]
        self.assertIn("Clean unmarked", p[:120])

    def test_distilled_settings(self):
        # Tencent Z-Image-Turbo card recommends CFG=0 (no guidance) for the
        # distilled checkpoint. Accept 0.0 or 1.0 as "no real guidance".
        self.assertIn(self.out["guidance_scale"], (0.0, 1.0))
        self.assertFalse(self.out["negative_prompt"])  # None or "" — both mean "no negative"
        # Tencent recommends 4-8 steps on turbo.
        self.assertLessEqual(self.out["steps"], 8)

    def test_concise_not_paragraphs(self):
        # zimage prefers compact form; no double-newlines.
        self.assertNotIn("\n\n", self.out["prompt"])


class TestAllBeatsZImageTurbo(unittest.TestCase):
    """Smoke: every beat through z_image_turbo produces a usable dict."""

    def test_no_beat_crashes(self):
        for beat_name, beat in BEATS.items():
            out = best_prompt_for("z_image_turbo", **beat)
            self.assertTrue(
                out["prompt"] and out["prompt"].strip(),
                f"empty prompt for z_image_turbo / {beat_name}",
            )
            self.assertIn(out["aspect"], {"9:16", "16:9", "1:1"})
            self.assertIsInstance(out["steps"], int)
            self.assertGreater(out["steps"], 0)


class TestUnknownModelRejected(unittest.TestCase):
    def test_unknown_model_raises(self):
        with self.assertRaises(ValueError) as ctx:
            best_prompt_for("flux2_klein", **BEATS["aita_couch_conflict"])
        self.assertIn("unknown model", str(ctx.exception))

    def test_dropped_models_rejected(self):
        # flux2_dev, qwen_image, hidream all dropped 2026-05-16.
        for dropped in ("flux2_dev", "qwen_image", "hidream"):
            with self.assertRaises(ValueError):
                best_prompt_for(dropped, **BEATS["aita_couch_conflict"])


if __name__ == "__main__":
    unittest.main()
