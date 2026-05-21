"""Pin the 2026-05-18 (round 5) per-beat seed-stride fix.

Background
----------

Render ``/tmp/render-bd2d0848/short.mp4`` shipped with 11 visually-similar
beats — same character, same kitchen pose, same lighting — even though
the LLM author had emitted 11 DISTINCT key_visual prompts (the
uniqueness gate added in round 4 passed; the prompt_len log spread
2116-2173 confirmed they were different). Root cause: the first-pass
image-gen loop used ``seed=seed_base + i`` (42, 43, 44, ...). Adjacent
seeds in Z-Image-Turbo's latent space produce similar noise tensors →
similar denoising trajectories → visually similar outputs *even when
the prompts diverge*.

The dedupe loop only catches EXACT MD5 byte collisions, not semantic
look-alikes, so the bug shipped.

Round-5 fix: the first-pass loop now uses
``seed=seed_base + i * seed_stride`` (default stride=9973, a prime
matching the dedupe-retry constant). Each beat lands in a different
latent region; semantically-distinct prompts now produce visibly
distinct frames.

These tests pin:

  1. The default behaviour (stride=9973) — seeds are widely spread.
  2. The opt-in legacy behaviour (stride=1 via spec.extra) — preserves
     reproducibility against pre-fix snapshots if a channel needs it.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401 — sets sys.path

from pipeline.render.contracts import Segment
from pipeline.render.visualize.ai_beat_slideshow import AiBeatSlideshow


def _spec(**overrides) -> MagicMock:
    s = MagicMock()
    s.output_resolution = (1080, 1920)
    s.output_fps = 30
    s.extra = {
        "image_provider": "cloudrun_z_image_turbo",
        "image_style_prefix": "",
        "image_seed": 42,
        "image_steps": 4,
    }
    s.extra.update(overrides)
    return s


def _timeline(n: int) -> list[Segment]:
    return [
        Segment(start_s=i, end_s=i + 1, text=f"line {i}", anchor_id=f"a{i}")
        for i in range(n)
    ]


class PerBeatSeedStrideTest(unittest.TestCase):

    def _capture_seeds(self, spec_extra: dict | None = None) -> list[int]:
        """Run produce() with image-gen mocked + capture every seed
        the first-pass loop passes to ``generate``."""
        n = 6
        spec = _spec(**(spec_extra or {}))
        captured_seeds: list[int] = []

        def _fake_generate(*, out_path: Path, seed: int, **kwargs):
            captured_seeds.append(seed)
            # Write a unique 1-byte payload per seed so the MD5 dedupe
            # loop doesn't trigger and add EXTRA seeds to the capture.
            Path(out_path).write_bytes(bytes([seed % 256]))

        with tempfile.TemporaryDirectory() as td:
            with patch(
                "pipeline.images.images.generate",
                side_effect=_fake_generate,
            ), patch(
                "pipeline.render.visualize.ai_beat_slideshow."
                "AiBeatSlideshow._stitch_images",
            ), patch(
                "pipeline.render.visualize.ai_beat_slideshow.probe_duration",
                return_value=float(n),
            ):
                AiBeatSlideshow().produce(spec, _timeline(n), Path(td))
        return captured_seeds

    def test_default_stride_spreads_seeds_widely(self):
        """Default behaviour: seeds[i] = 42 + i * 9973. No two seeds
        should differ by < 100 (proves we're NOT using the legacy
        adjacent-seed pattern)."""
        seeds = self._capture_seeds()
        self.assertEqual(len(seeds), 6)
        # Exact expected values for the default stride.
        expected = [42 + i * 9973 for i in range(6)]
        self.assertEqual(seeds, expected)
        # Sanity: every pair is >= 9000 apart.
        for i in range(1, len(seeds)):
            self.assertGreater(
                seeds[i] - seeds[i - 1], 9000,
                f"adjacent seeds too close: {seeds[i - 1]} vs {seeds[i]}",
            )

    def test_legacy_stride_one_preserves_adjacent_seeds(self):
        """Opt-in for channels that need pre-fix reproducibility:
        ``image_seed_stride: 1`` → seeds[i] = 42 + i (the old behaviour)."""
        seeds = self._capture_seeds({"image_seed_stride": 1})
        self.assertEqual(seeds, [42 + i for i in range(6)])

    def test_custom_stride_honored(self):
        """A channel can pin its own stride — verify it round-trips."""
        seeds = self._capture_seeds({"image_seed_stride": 31337})
        self.assertEqual(seeds, [42 + i * 31337 for i in range(6)])


if __name__ == "__main__":
    unittest.main()
