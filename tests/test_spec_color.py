"""Tests for the color parser."""

from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

from render_from_spec import _parse_color


class ParseColorTest(unittest.TestCase):
    def test_hex_6(self):
        self.assertEqual(_parse_color("#ff4500"), (255, 69, 0, 255))

    def test_hex_6_lowercase(self):
        self.assertEqual(_parse_color("#fff050"), (255, 240, 80, 255))

    def test_hex_8_with_alpha(self):
        # #141414eb — translucent dark, used for closer panel bg
        self.assertEqual(_parse_color("#141414eb"), (20, 20, 20, 235))

    def test_hex_8_full_alpha(self):
        self.assertEqual(_parse_color("#000000ff"), (0, 0, 0, 255))

    def test_tuple_rgb(self):
        self.assertEqual(_parse_color((10, 20, 30)), (10, 20, 30, 255))

    def test_tuple_rgba(self):
        self.assertEqual(_parse_color((10, 20, 30, 100)), (10, 20, 30, 100))

    def test_list_rgb(self):
        self.assertEqual(_parse_color([1, 2, 3]), (1, 2, 3, 255))

    def test_none_returns_none(self):
        self.assertIsNone(_parse_color(None))

    def test_invalid_returns_none(self):
        self.assertIsNone(_parse_color(42))
        self.assertIsNone(_parse_color("not-a-color"))


if __name__ == "__main__":
    unittest.main()
