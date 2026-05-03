"""Tests for layout: measure_primitive, expand_template, resolve_position."""

from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

from render_from_spec import (
    _template_helpers,
    expand_template,
    measure_primitive,
    resolve_position,
)


class TemplateHelpersTest(unittest.TestCase):
    def test_pill_grid_for_width_940(self):
        h = _template_helpers(940)
        # pad=36, so two pills of width (940 - 3*36)/2 = 416
        self.assertEqual(h["pad"], 36)
        self.assertEqual(h["pill_h"], 170)
        self.assertEqual(h["pill_w"], 416.0)
        self.assertEqual(h["pill_x"](0), 36)            # first pill starts at left pad
        self.assertEqual(h["pill_x"](1), 36 + 416 + 36)  # = 488
        self.assertEqual(h["pill_cx"](0), 36 + 208)      # = 244
        self.assertEqual(h["pill_cx"](1), 488 + 208)     # = 696


class MeasurePrimitiveTest(unittest.TestCase):
    def test_explicit_box(self):
        box = measure_primitive(
            {"kind": "rounded_rect", "x": 10, "y": 20, "w": 100, "h": 50},
            {},
        )
        self.assertEqual(box, {"x": 10, "y": 20, "w": 100, "h": 50})

    def test_region_full(self):
        # region=full means the rect spans the parent — should report
        # parent_w/parent_h from the namespace
        box = measure_primitive(
            {"kind": "rounded_rect", "region": "full"},
            {"parent_w": 940, "parent_h": 240},
        )
        self.assertEqual(box["w"], 940)
        self.assertEqual(box["h"], 240)
        self.assertEqual((box["x"], box["y"]), (0, 0))

    def test_text_block_height_grows_with_lines(self):
        # 56pt text wrapping ~900px should produce multi-line box
        box = measure_primitive(
            {
                "kind": "text_block",
                "x": 0, "y": 0, "w": 900,
                "text": "AITA for refusing a water birth in my living room?",
                "size": 56, "line_height": 64, "bold": True,
            },
            {},
        )
        # Whatever the wrap produces, height must be a multiple of line_height
        self.assertEqual(box["h"] % 64, 0)
        self.assertGreaterEqual(box["h"], 64)  # at least one line

    def test_emoji_box_is_size_squared(self):
        box = measure_primitive({"kind": "emoji", "x": 0, "y": 0, "size": 220}, {})
        self.assertEqual(box["w"], 220)
        self.assertEqual(box["h"], 220)


class ExpandTemplateTest(unittest.TestCase):
    def _two_button_template(self):
        return {
            "two_button_panel": {
                "params": ["width", "buttons"],
                "size": {"w": "${width}", "h": 240},
                "anchors": {
                    "button_0": {"x": "pad",                "y": 36, "w": "pill_w", "h": 170},
                    "button_1": {"x": "pad + pill_w + pad", "y": 36, "w": "pill_w", "h": 170},
                },
                "layout": [
                    {"kind": "rounded_rect", "region": "full", "fill": "#141414eb"},
                    {
                        "kind": "each",
                        "of": "${buttons}",
                        "as": "btn",
                        "index": "i",
                        "do": [
                            {"kind": "rounded_rect", "x": "pill_x(i)", "y": 36,
                             "w": "pill_w", "h": 170, "fill": "${btn.bg}"},
                            {"kind": "text", "x": "pill_cx(i)", "y": 96,
                             "text": "${btn.line1}", "size": 58, "color": "${btn.fg}",
                             "anchor": "mm"},
                        ],
                    },
                ],
            }
        }

    def test_each_loop_expands_buttons(self):
        templates = self._two_button_template()
        args = {
            "width": 940,
            "buttons": [
                {"line1": "Like",    "bg": "#c83238", "fg": "#ffffff"},
                {"line1": "Comment", "bg": "#228b46", "fg": "#ffffff"},
            ],
        }
        out = expand_template("two_button_panel", args, templates)

        # 1 base rect + 2 buttons * 2 prims each = 5
        self.assertEqual(len(out["primitives"]), 5)

        # First two button-pill rects should have correct fills + x positions
        pill_rects = [p for p in out["primitives"]
                      if p["kind"] == "rounded_rect" and p.get("region") != "full"]
        self.assertEqual(len(pill_rects), 2)
        self.assertEqual(pill_rects[0]["fill"], "#c83238")
        self.assertEqual(pill_rects[1]["fill"], "#228b46")
        self.assertEqual(pill_rects[0]["x"], 36)        # pill_x(0)
        self.assertEqual(pill_rects[1]["x"], 36 + 416 + 36)  # pill_x(1)

        # Button labels should have substituted text + colors
        texts = [p for p in out["primitives"] if p["kind"] == "text"]
        self.assertEqual(texts[0]["text"], "Like")
        self.assertEqual(texts[1]["text"], "Comment")
        for t in texts:
            self.assertEqual(t["color"], "#ffffff")

    def test_template_size_resolved(self):
        templates = self._two_button_template()
        args = {"width": 940, "buttons": []}
        out = expand_template("two_button_panel", args, templates)
        self.assertEqual(out["width"], 940)
        self.assertEqual(out["height"], 240)

    def test_anchors_resolved_with_corners(self):
        templates = self._two_button_template()
        args = {"width": 940, "buttons": []}
        out = expand_template("two_button_panel", args, templates)
        b0 = out["anchors"]["button_0"]
        self.assertEqual(b0["x"], 36)
        self.assertEqual(b0["w"], 416)
        # corner tuples
        self.assertEqual(b0["top_left"], (36, 36))
        self.assertEqual(b0["top_right"], (452, 36))         # 36 + 416
        self.assertEqual(b0["bottom_right"], (452, 206))     # 36+416, 36+170
        self.assertEqual(b0["center"], (244.0, 121.0))       # mid-pill

    def test_auto_height_grows_with_content(self):
        templates = {
            "card": {
                "params": ["width"],
                "size": {"w": "${width}", "h": "auto"},
                "layout": [
                    {"kind": "rounded_rect", "region": "full", "fill": "#fff"},
                    {"kind": "ellipse", "x": 10, "y": 10, "w": 50, "h": 50, "fill": "#000"},
                    {"kind": "ellipse", "x": 10, "y": 200, "w": 50, "h": 50, "fill": "#000"},
                ],
            }
        }
        out = expand_template("card", {"width": 500}, templates)
        # max(y+h) = 200 + 50 = 250, plus 30 bottom-pad = 280
        self.assertEqual(out["height"], 280)

    def test_unknown_template_raises(self):
        with self.assertRaises(ValueError):
            expand_template("does_not_exist", {}, {})


class ResolvePositionTest(unittest.TestCase):
    def test_explicit_xy(self):
        x, y = resolve_position({"x": 100, "y": 50}, 200, 100, 1080, 1920, {})
        self.assertEqual((x, y), (100, 50))

    def test_x_center(self):
        # element 980 wide on 1080 canvas → x = (1080 - 980) // 2 = 50
        x, y = resolve_position({"x": "center", "y": 220}, 980, 340, 1080, 1920, {})
        self.assertEqual((x, y), (50, 220))

    def test_y_from_bottom(self):
        # element 240 tall on 1920 canvas with y_from_bottom=360
        # → y = 1920 - 240 - 360 = 1320
        x, y = resolve_position(
            {"x": "center", "y_from_bottom": 360}, 1000, 240, 1080, 1920, {}
        )
        self.assertEqual(y, 1320)

    def test_region_lower_third(self):
        x, y = resolve_position({"region": "lower_third"}, 1000, 240, 1080, 1920, {})
        # 18% bottom margin of 1920 = 345 (int trunc); y = 1920 - 240 - 345 = 1335
        self.assertEqual(x, 40)
        self.assertEqual(y, 1335)

    def test_region_middle(self):
        x, y = resolve_position({"region": "middle"}, 200, 100, 1080, 1920, {})
        self.assertEqual((x, y), ((1080 - 200) // 2, (1920 - 100) // 2))

    def test_anchor_to_named_anchor_corner(self):
        # Simulate a registered closer_panel overlay with button_0 anchor.
        registry = {
            "closer_panel": {
                "x": 70, "y": 220, "w": 940, "h": 240,
                "anchors": {
                    "button_0": {
                        "x": 36, "y": 36, "w": 416, "h": 170,
                        "top_right": (36 + 416, 36),  # local
                    },
                },
            },
        }
        # emoji 220×220 anchored to button_0.top_right with offset (-30, -30)
        # → center at (70 + 452 - 30, 220 + 36 - 30) = (492, 226)
        # → top-left at (492 - 110, 226 - 110) = (382, 116)
        x, y = resolve_position(
            {"anchor": "closer_panel.button_0.top_right", "offset": [-30, -30]},
            220, 220, 1080, 1920, registry,
        )
        self.assertEqual((x, y), (382, 116))

    def test_anchor_to_overlay_corner(self):
        registry = {
            "card": {"x": 100, "y": 200, "w": 400, "h": 300, "anchors": {}},
        }
        # bottom_right of card is (500, 500); element centered there
        x, y = resolve_position(
            {"anchor": "card.bottom_right"}, 100, 80, 1080, 1920, registry,
        )
        # center at (500, 500) → top-left at (450, 460)
        self.assertEqual((x, y), (450, 460))

    def test_anchor_unknown_target_raises(self):
        with self.assertRaises(ValueError):
            resolve_position(
                {"anchor": "does_not_exist.top_left"},
                100, 100, 1080, 1920, {},
            )

    def test_anchor_unknown_named_raises(self):
        registry = {"card": {"x": 0, "y": 0, "w": 100, "h": 100, "anchors": {}}}
        with self.assertRaises(ValueError):
            resolve_position(
                {"anchor": "card.nonexistent.top_left"},
                10, 10, 1080, 1920, registry,
            )


if __name__ == "__main__":
    unittest.main()
