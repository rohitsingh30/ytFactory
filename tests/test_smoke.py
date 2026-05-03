"""Light smoke tests: tiny end-to-end render-time logic without touching
TTS/Whisper/ffmpeg. Verifies the orchestration glue around the spec
interpreter: deep_merge, draw_primitive (PIL roundtrip), emoji-pop
ffmpeg expression generator.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from PIL import Image, ImageDraw

from tests._helpers import PROJECT_ROOT  # noqa: F401

from render_from_spec import (
    _deep_merge,
    _emoji_pop_scale_expr,
    draw_primitive,
    render_template_to_png,
)


class DeepMergeTest(unittest.TestCase):
    def test_b_wins_on_scalar(self):
        out = _deep_merge({"x": 1}, {"x": 2})
        self.assertEqual(out, {"x": 2})

    def test_recursive_merge(self):
        out = _deep_merge(
            {"audio": {"voice": "af_bella", "speed": 1.0}},
            {"audio": {"speed": 1.1}},
        )
        self.assertEqual(out, {"audio": {"voice": "af_bella", "speed": 1.1}})

    def test_keys_only_in_a_preserved(self):
        out = _deep_merge({"a": 1, "b": 2}, {"b": 22, "c": 3})
        self.assertEqual(out, {"a": 1, "b": 22, "c": 3})

    def test_lists_replace_not_merge(self):
        # Lists replace wholesale (intentional — overlays list shouldn't merge)
        out = _deep_merge({"xs": [1, 2, 3]}, {"xs": [9]})
        self.assertEqual(out, {"xs": [9]})


class EmojiPopExprTest(unittest.TestCase):
    def test_scale_overshoot_contains_t0(self):
        expr = _emoji_pop_scale_expr(
            T0=17.34, S=220.0,
            anim={"kind": "scale_overshoot", "duration_s": 0.25, "overshoot": 1.25},
        )
        # Sanity: expression mentions t0 and the target size + overshoot
        self.assertIn("17.340", expr)
        self.assertIn("220.0", expr)
        self.assertIn("1.25", expr)
        # Must reference the running time variable t
        self.assertIn("t-17.340", expr)

    def test_static_when_no_animation(self):
        expr = _emoji_pop_scale_expr(T0=10.0, S=200.0, anim={"kind": "none"})
        self.assertEqual(expr, "200.0")


class DrawPrimitiveTest(unittest.TestCase):
    def _make_canvas(self, w=200, h=200):
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        return img, ImageDraw.Draw(img)

    def test_rounded_rect_explicit_box(self):
        img, draw = self._make_canvas()
        draw_primitive(
            draw,
            {"kind": "rounded_rect", "x": 10, "y": 10, "w": 80, "h": 60,
             "fill": "#ff0000", "radius": 8},
            ns={},
        )
        # Centre of the rect should be red-ish (rounded corners are at edges)
        self.assertEqual(img.getpixel((50, 40))[:3], (255, 0, 0))
        # Outside the rect should still be transparent
        self.assertEqual(img.getpixel((150, 150)), (0, 0, 0, 0))

    def test_rounded_rect_region_full(self):
        img, draw = self._make_canvas(w=120, h=80)
        draw_primitive(
            draw,
            {"kind": "rounded_rect", "region": "full", "fill": "#00ff00", "radius": 0},
            ns={"parent_w": 120, "parent_h": 80},
        )
        # Centre pixel is green
        self.assertEqual(img.getpixel((60, 40))[:3], (0, 255, 0))

    def test_ellipse_fills_centre(self):
        img, draw = self._make_canvas()
        draw_primitive(
            draw,
            {"kind": "ellipse", "x": 50, "y": 50, "w": 100, "h": 100, "fill": "#0000ff"},
            ns={},
        )
        self.assertEqual(img.getpixel((100, 100))[:3], (0, 0, 255))

    def test_text_draws_something(self):
        img, draw = self._make_canvas(w=300, h=100)
        draw_primitive(
            draw,
            {"kind": "text", "x": 10, "y": 30, "text": "AITA",
             "size": 48, "color": "#ffffff", "bold": True},
            ns={},
        )
        # Some white pixel should exist somewhere in the text area
        any_white = any(
            img.getpixel((x, y))[:3] == (255, 255, 255)
            for x in range(10, 200) for y in range(30, 80)
        )
        self.assertTrue(any_white, "expected text glyphs to draw white pixels")


class RenderTemplateRoundtripTest(unittest.TestCase):
    def test_simple_template_writes_png(self):
        # Minimal template: a coloured rect.
        expanded = {
            "width": 200,
            "height": 100,
            "primitives": [
                {"kind": "rounded_rect", "x": 0, "y": 0, "w": 200, "h": 100,
                 "fill": "#fa0", "radius": 12},
            ],
            "ns": {"parent_w": 200, "parent_h": 100},
        }
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            out = Path(f.name)
        try:
            render_template_to_png(expanded, out)
            img = Image.open(out)
            self.assertEqual(img.size, (200, 100))
            # Mid-pixel should be the orange fill
            self.assertEqual(img.getpixel((100, 50))[:3], (0xFF, 0xAA, 0x00))
        finally:
            out.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
