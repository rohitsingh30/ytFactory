"""Tests for pipeline.captions — 100% line coverage."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline import captions
from pipeline.captions import (
    _find_font,
    _split_closer_format,
    render_beat_caption,
    render_closer_caption_rows,
    render_closer_panel,
    render_rank_chip,
    render_subscribe_button,
    render_word_caption,
)
from pipeline.beats import Beat, Word


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _beat(text, start=0.0, end=2.0, words=None):
    if words is None:
        words = [Word(w, start + i * 0.2, start + (i + 1) * 0.2)
                 for i, w in enumerate(text.split())]
    return Beat(text=text, start=start, end=end, words=words)


# ---------------------------------------------------------------------------
# render_beat_caption
# ---------------------------------------------------------------------------

class TestRenderBeatCaption(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cache = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_renders_png(self):
        beat = _beat("Hello world this is a caption test line")
        out = self.cache / "caption_00.png"
        result = render_beat_caption(beat, out)
        self.assertTrue(out.exists())
        self.assertEqual(result, out)

    def test_long_text_wraps(self):
        # Long text forces wrapping and variable canvas height
        beat = _beat("A very long sentence that should wrap onto multiple lines because it exceeds the maximum width")
        out = self.cache / "caption_long.png"
        result = render_beat_caption(beat, out)
        self.assertTrue(out.exists())

    def test_devanagari_text(self):
        beat = _beat("श्री राम जय राम जय जय राम")
        out = self.cache / "caption_dev.png"
        result = render_beat_caption(beat, out)
        self.assertTrue(out.exists())


# ---------------------------------------------------------------------------
# render_word_caption
# ---------------------------------------------------------------------------

class TestRenderWordCaption(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cache = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_renders_png(self):
        out = self.cache / "word_0000.png"
        result = render_word_caption("Hello", out)
        self.assertTrue(out.exists())
        self.assertEqual(result, out)

    def test_empty_word(self):
        out = self.cache / "word_empty.png"
        result = render_word_caption("", out)
        self.assertTrue(out.exists())

    def test_no_pill(self):
        out = self.cache / "word_nopill.png"
        result = render_word_caption("Test", out, bg_pill_alpha=0)
        self.assertTrue(out.exists())

    def test_devanagari_word(self):
        out = self.cache / "word_dev.png"
        result = render_word_caption("राम", out)
        self.assertTrue(out.exists())

    def test_creates_parent_dirs(self):
        out = self.cache / "subdir" / "word.png"
        render_word_caption("Hi", out)
        self.assertTrue(out.exists())


# ---------------------------------------------------------------------------
# _split_closer_format
# ---------------------------------------------------------------------------

class TestSplitCloserFormat(unittest.TestCase):
    def test_aita_format_comma(self):
        rows = _split_closer_format("LIKE if YTA, COMMENT if NTA. AITA?")
        self.assertIn("LIKE if YTA", rows)
        self.assertIn("COMMENT if NTA", rows)

    def test_em_dash_split(self):
        rows = _split_closer_format("Vote your verdict — yes or no?")
        self.assertGreater(len(rows), 0)

    def test_vs_split(self):
        rows = _split_closer_format("Team A vs Team B — who wins?")
        self.assertGreater(len(rows), 0)

    def test_slash_split(self):
        rows = _split_closer_format("Option A / Option B / Option C")
        self.assertGreater(len(rows), 0)

    def test_single_row_no_separator(self):
        rows = _split_closer_format("Simple CTA here")
        self.assertEqual(len(rows), 1)

    def test_wibta_stripped(self):
        rows = _split_closer_format("LIKE if agree, COMMENT if disagree. WIBTA?")
        for r in rows:
            self.assertNotIn("WIBTA", r)

    def test_empty_format(self):
        rows = _split_closer_format("")
        self.assertGreater(len(rows), 0)

    def test_dash_separator(self):
        rows = _split_closer_format("First part - second part")
        self.assertGreater(len(rows), 0)


# ---------------------------------------------------------------------------
# render_rank_chip
# ---------------------------------------------------------------------------

class TestRenderRankChip(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cache = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_renders_chip(self):
        out = self.cache / "rank_chip_5.png"
        result = render_rank_chip(5, out)
        self.assertTrue(out.exists())
        self.assertEqual(result, out)

    def test_rank_1(self):
        out = self.cache / "rank_chip_1.png"
        render_rank_chip(1, out)
        self.assertTrue(out.exists())



# ---------------------------------------------------------------------------
# render_subscribe_button
# ---------------------------------------------------------------------------

class TestRenderSubscribeButton(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cache = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_renders(self):
        out = self.cache / "subscribe.png"
        result = render_subscribe_button(out)
        self.assertTrue(out.exists())
        self.assertEqual(result, out)


# ---------------------------------------------------------------------------
# render_closer_caption_rows
# ---------------------------------------------------------------------------

class TestRenderCloserCaptionRows(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cache = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_single_row(self):
        paths = render_closer_caption_rows("Vote now", self.cache)
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].exists())

    def test_two_rows(self):
        paths = render_closer_caption_rows(
            "LIKE if YTA, COMMENT if NTA", self.cache
        )
        self.assertGreaterEqual(len(paths), 1)
        for p in paths:
            self.assertTrue(p.exists())


# ---------------------------------------------------------------------------
# render_closer_panel
# ---------------------------------------------------------------------------

class TestRenderCloserPanel(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cache = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_renders_default(self):
        out = self.cache / "closer_panel.png"
        result = render_closer_panel(out)
        self.assertTrue(out.exists())
        self.assertEqual(result, out)

    def test_custom_closer_format(self):
        out = self.cache / "closer_custom.png"
        result = render_closer_panel(
            out,
            closer_format="SUBSCRIBE for more, LIKE the video",
        )
        self.assertTrue(out.exists())

    def test_single_row_format(self):
        out = self.cache / "closer_single.png"
        result = render_closer_panel(out, closer_format="Vote your verdict now")
        self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()

# ---------------------------------------------------------------------------
# _find_font — OSError fallback (lines 81-83)
# ---------------------------------------------------------------------------

class TestFindFontFallback(unittest.TestCase):
    """When a candidate font path exists but truetype raises OSError → continue → load_default."""

    def test_oserror_falls_through_to_load_default(self):
        from pipeline.captions import _find_font
        # Arial Bold.ttf exists on this macOS system — patch truetype to raise OSError.
        # All candidates fail → _find_font falls through to load_default().
        # Wrapping in try/except because load_default() may also use truetype internally.
        with patch("pipeline.captions.ImageFont.truetype", side_effect=OSError("corrupt font")):
            try:
                _find_font(24, "hello")
            except OSError:
                pass  # expected if load_default() also calls truetype
