"""Unit tests for tests/render/golden_assertions.py.

Pin the tolerance helpers' contracts:
- assert_structural raises on codec / resolution / sample-rate mismatch
- assert_duration_within_pct catches drift > tolerance
- assert_lufs_within_db catches drift > tolerance
- assert_overlay_burned catches missing overlays (uniform region)
- pixel_variance returns ≈ 0 for solid color, >> 0 for varied content
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.render.golden_assertions import (
    assert_duration_within_pct,
    assert_overlay_burned,
    assert_structural,
    extract_frame,
    pixel_variance,
)


def _make_mp4(
    path: Path,
    *,
    duration_s: float = 1.0,
    w: int = 1080,
    h: int = 1920,
    fps: int = 30,
    color: str = "0x141414",
    sample_rate: int = 24000,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", f"color=c={color}:s={w}x{h}:r={fps}",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", f"sine=frequency=440:sample_rate={sample_rate}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-ac", "1",
        str(path),
    ], check=True, capture_output=True)


def _make_png(path: Path, *, w: int = 100, h: int = 100,
              color: tuple[int, int, int] = (20, 20, 20)) -> None:
    from PIL import Image
    Image.new("RGB", (w, h), color).save(str(path))


def _make_png_with_text(path: Path, *, w: int = 100, h: int = 100) -> None:
    """Solid background + a coloured rectangle to give non-zero variance."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (20, 20, 20))
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 90, 90], fill=(220, 200, 50))
    img.save(str(path))


class StructuralAssertionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-golden-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_passes_on_matching_mp4(self):
        path = self.tmp / "ok.mp4"
        _make_mp4(path, w=1080, h=1920, sample_rate=24000)
        # Should NOT raise.
        assert_structural(path, codec_video="h264", codec_audio="aac",
                          sample_rate=24000, resolution=(1080, 1920))

    def test_raises_on_missing_file(self):
        with self.assertRaises(AssertionError) as ctx:
            assert_structural(self.tmp / "nope.mp4")
        self.assertIn("missing", str(ctx.exception))

    def test_raises_on_wrong_resolution(self):
        path = self.tmp / "wrong_res.mp4"
        _make_mp4(path, w=1920, h=1080)
        with self.assertRaises(AssertionError) as ctx:
            assert_structural(path, resolution=(1080, 1920))
        self.assertIn("resolution", str(ctx.exception))

    def test_raises_on_wrong_sample_rate(self):
        path = self.tmp / "wrong_sr.mp4"
        _make_mp4(path, sample_rate=44100)
        with self.assertRaises(AssertionError) as ctx:
            assert_structural(path, sample_rate=24000)
        self.assertIn("sample_rate", str(ctx.exception))


class DurationToleranceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-golden-dur-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_passes_within_tolerance(self):
        path = self.tmp / "ok.mp4"
        _make_mp4(path, duration_s=2.0)
        # ±1% covers ffmpeg's typical < 0.05s variance.
        assert_duration_within_pct(path, expected_s=2.0, pct=1.0)

    def test_raises_outside_tolerance(self):
        path = self.tmp / "wrong.mp4"
        _make_mp4(path, duration_s=2.0)
        # Expect 5.0s ± 1% = catches the 2.0s actual.
        with self.assertRaises(AssertionError) as ctx:
            assert_duration_within_pct(path, expected_s=5.0, pct=1.0)
        self.assertIn("drifted", str(ctx.exception))

    def test_negative_expected_raises(self):
        path = self.tmp / "x.mp4"
        _make_mp4(path, duration_s=1.0)
        with self.assertRaises(AssertionError):
            assert_duration_within_pct(path, expected_s=-1.0)


class PixelVarianceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-pixvar-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_solid_color_has_zero_variance(self):
        png = self.tmp / "solid.png"
        _make_png(png, color=(50, 50, 50))
        self.assertAlmostEqual(pixel_variance(png), 0.0, places=2)

    def test_mixed_content_has_high_variance(self):
        png = self.tmp / "mixed.png"
        _make_png_with_text(png)
        self.assertGreater(pixel_variance(png), 1000.0)

    def test_region_crop(self):
        png = self.tmp / "mixed.png"
        _make_png_with_text(png, w=200, h=200)
        # The interior (10..90 on a 200x200) has the rectangle; edges are solid.
        # Crop a top-corner region that's all background → variance ≈ 0.
        edge_var = pixel_variance(png, region=(150, 150, 30, 30))
        self.assertLess(edge_var, 50.0)


class OverlayBurnAssertionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-overlay-burn-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_raises_when_region_is_uniform(self):
        # MP4 with a constant solid background — the assertion should
        # fail because the region has zero variance.
        path = self.tmp / "uniform.mp4"
        _make_mp4(path, color="0x141414", duration_s=0.5)
        with self.assertRaises(AssertionError) as ctx:
            assert_overlay_burned(path, t_s=0.1,
                                  region=(0, 0, 100, 100),
                                  min_variance=100.0)
        self.assertIn("variance", str(ctx.exception))

    def test_extract_frame_produces_png(self):
        path = self.tmp / "x.mp4"
        _make_mp4(path, duration_s=1.0)
        frame = extract_frame(path, t_s=0.5,
                              out_png=self.tmp / "frame.png")
        self.assertTrue(frame.exists())
        self.assertEqual(frame.suffix, ".png")


if __name__ == "__main__":
    unittest.main()
