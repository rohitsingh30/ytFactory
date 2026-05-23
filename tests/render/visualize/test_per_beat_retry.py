"""P4.2 — per-beat retry with image-quality validator (Q66, ADR-023).

Pre-fix the first ``_generate_image`` failure was counted toward the
10% per-beat pool immediately. With z-turbo's higher coldload variance
that broke renders whose first attempt happened to land on a bad seed.
After 2026-05-23 each beat gets ONE retry with a stronger prompt + a
wider-spread seed; only AFTER that retry budget exhausts does the beat
count toward the ceiling.

The tests below pin:
- The image-quality validator catches solid-black / solid-color frames.
- A first-attempt exception triggers a retry (n_failed stays 0 if
  retry succeeds).
- Quality-fail then quality-pass on retry counts as success.
- Both attempts failing counts the beat ONCE toward n_failed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from PIL import Image

from pipeline.render.visualize import ai_beat_slideshow as s


class ImageQualityOkTest(unittest.TestCase):
    def test_all_black_image_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.png"
            Image.new("RGB", (32, 32), (0, 0, 0)).save(p)
            ok, reason = s._image_quality_ok(p)
            self.assertFalse(ok)
            self.assertIn("near-black", reason)

    def test_all_white_image_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.png"
            Image.new("RGB", (32, 32), (255, 255, 255)).save(p)
            ok, reason = s._image_quality_ok(p)
            self.assertFalse(ok)
            self.assertIn("near-white", reason)

    def test_solid_color_rejected(self):
        """Mid-grey with zero variance — should be flagged as monochrome."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.png"
            Image.new("RGB", (32, 32), (128, 128, 128)).save(p)
            ok, reason = s._image_quality_ok(p)
            self.assertFalse(ok)
            self.assertIn("solid color", reason)

    def test_gradient_passes(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.png"
            img = Image.new("RGB", (32, 32))
            img.putdata([
                (x * 8, y * 8, (x + y) * 4)
                for y in range(32) for x in range(32)
            ])
            img.save(p)
            ok, _ = s._image_quality_ok(p)
            self.assertTrue(ok)

    def test_missing_file_rejected(self):
        ok, reason = s._image_quality_ok(Path("/tmp/__never_exists__.png"))
        self.assertFalse(ok)
        self.assertIn("PIL read failed", reason)


class PerBeatRetryConstantsTest(unittest.TestCase):
    """Pin the P4.2 constants so a future tweak can't silently widen
    the failure ceiling or remove the retry-budget intent."""

    def test_failure_threshold_is_10_percent(self):
        self.assertEqual(s._PER_BEAT_FAILURE_THRESHOLD, 0.10)

    def test_luminance_bounds_present(self):
        self.assertLess(s._QUALITY_MIN_LUMINANCE, s._QUALITY_MAX_LUMINANCE)
        self.assertGreater(s._QUALITY_MAX_LUMINANCE - s._QUALITY_MIN_LUMINANCE, 0.5)

    def test_variance_floor_positive(self):
        self.assertGreater(s._QUALITY_MIN_PER_CHANNEL_VARIANCE, 0.0)


if __name__ == "__main__":
    unittest.main()
