"""Tests for pipeline.quality_gate.check_image — render-time image gate."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import random
from PIL import Image, ImageDraw

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm.quality_gate import check_image


def _save(img: Image.Image, suffix=".png") -> Path:
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.close()
    p = Path(f.name)
    img.save(p)
    return p


def _rich_image(w=512, h=512) -> Image.Image:
    """A rich-content PNG: noise + scattered shapes. Easily clears the
    edge / std-dev thresholds."""
    rng = random.Random(0)
    img = Image.new("RGB", (w, h), (200, 200, 200))
    pixels = img.load()
    # Random noise
    for x in range(w):
        for y in range(h):
            r, g, b = pixels[x, y]
            pixels[x, y] = (
                max(0, min(255, r + rng.randint(-40, 40))),
                max(0, min(255, g + rng.randint(-40, 40))),
                max(0, min(255, b + rng.randint(-40, 40))),
            )
    draw = ImageDraw.Draw(img)
    # Some chunky shapes for edges
    for _ in range(8):
        x0, y0 = rng.randint(0, w - 50), rng.randint(0, h - 50)
        draw.rectangle(
            [x0, y0, x0 + rng.randint(20, 100), y0 + rng.randint(20, 100)],
            fill=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)),
            outline=(0, 0, 0), width=2,
        )
    return img


class CheckImagePassTest(unittest.TestCase):
    def test_rich_image_passes(self):
        path = _save(_rich_image())
        try:
            ok, reason = check_image(path)
            self.assertTrue(ok, f"expected pass, got reason={reason!r}")
            self.assertEqual(reason, "")
        finally:
            path.unlink(missing_ok=True)

    def test_expected_size_match(self):
        path = _save(_rich_image(w=300, h=400))
        try:
            ok, reason = check_image(path, expected_w=300, expected_h=400)
            self.assertTrue(ok)
        finally:
            path.unlink(missing_ok=True)


class CheckImageFailureTest(unittest.TestCase):
    def test_missing_file(self):
        ok, reason = check_image(Path("/tmp/_does_not_exist_qg.png"))
        self.assertFalse(ok)
        self.assertIn("does not exist", reason)

    def test_too_small_file(self):
        # Tiny 1×1 PNG — well under 30 KB threshold
        path = _save(Image.new("RGB", (1, 1), (0, 0, 0)))
        try:
            ok, reason = check_image(path)
            self.assertFalse(ok)
            self.assertIn("too small", reason)
        finally:
            path.unlink(missing_ok=True)

    def test_all_black_rejected(self):
        # A large all-black PNG passes the size check (compresses small,
        # so we lower the file-size threshold for this test). The std-dev
        # check should still reject it.
        path = _save(Image.new("RGB", (512, 512), (0, 0, 0)))
        try:
            ok, reason = check_image(path, min_file_size=0)
            self.assertFalse(ok)
            self.assertIn("flat", reason)
        finally:
            path.unlink(missing_ok=True)

    def test_all_white_rejected_by_edge_density(self):
        path = _save(Image.new("RGB", (512, 512), (255, 255, 255)))
        try:
            ok, reason = check_image(path, min_file_size=0)
            self.assertFalse(ok)
            # Flat white should hit either the std-dev OR edge-density rule
            self.assertTrue("flat" in reason or "mush" in reason)
        finally:
            path.unlink(missing_ok=True)

    def test_wrong_dimensions_rejected(self):
        path = _save(_rich_image(w=200, h=200))
        try:
            ok, reason = check_image(path, expected_w=400, expected_h=400)
            self.assertFalse(ok)
            self.assertIn("width", reason)
        finally:
            path.unlink(missing_ok=True)

    def test_corrupt_file_rejected(self):
        # Write garbage that's >30KB but isn't a valid image
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"not a real png " * 5000)
            p = Path(f.name)
        try:
            ok, reason = check_image(p)
            self.assertFalse(ok)
            self.assertIn("could not open", reason)
        finally:
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
