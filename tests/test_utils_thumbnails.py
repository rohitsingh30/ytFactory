"""100% line coverage for pipeline/utils/thumbnails.py.

Uses small synthetic PIL images in-memory.  ``_find_font`` is patched to
return ``ImageFont.load_default()`` so no system font installation is needed.
``_cli()`` is excluded via ``# pragma: no cover`` (complex argparse + disk I/O
with no testable invariants beyond the underlying functions already covered).
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import thumbnails

_BASE = Path(__file__).resolve().parent

# A tiny but consistent font mock that reports realistic glyph widths.
_DEFAULT_FONT = ImageFont.load_default()


def _mock_font_factory(width_per_char: int = 8):
    """Return a MagicMock font whose getbbox reports width_per_char px per char.
    Only safe for tests that test layout logic WITHOUT calling draw.text().
    """
    m = MagicMock(spec=ImageFont.FreeTypeFont)

    def _bbox(text):
        w = len(text) * width_per_char
        return (0, 2, w, 14)

    m.getbbox.side_effect = _bbox
    return m


# Real default font — usable for tests that call draw.text() / draw.rounded_rectangle.
_REAL_FONT = ImageFont.load_default()


class TestStyleFromChannel(unittest.TestCase):
    def test_explicit_style_override(self) -> None:
        yaml_cfg = {"upload": {"thumbnail": {"style": "tifu"}}}
        s = thumbnails.style_from_channel(yaml_cfg)
        self.assertEqual(s, thumbnails._STYLES["tifu"])

    def test_channel_dir_tifu(self) -> None:
        s = thumbnails.style_from_channel({}, channel_dir="tifu_variant")
        self.assertEqual(s, thumbnails._STYLES["tifu"])

    def test_channel_dir_wiki_oddities(self) -> None:
        s = thumbnails.style_from_channel({}, channel_dir="wiki_oddities")
        self.assertEqual(s, thumbnails._STYLES["oddities"])

    def test_channel_dir_today_in_history(self) -> None:
        s = thumbnails.style_from_channel({}, channel_dir="today_in_history")
        self.assertEqual(s, thumbnails._STYLES["tih"])

    def test_channel_dir_aita(self) -> None:
        s = thumbnails.style_from_channel({}, channel_dir="reddit_amitheasshole")
        self.assertEqual(s, thumbnails._STYLES["aita"])

    def test_channel_dir_maliciouscompliance(self) -> None:
        s = thumbnails.style_from_channel({}, channel_dir="maliciouscompliance_v2")
        self.assertEqual(s, thumbnails._STYLES["aita"])

    def test_channel_dir_prorevenge(self) -> None:
        s = thumbnails.style_from_channel({}, channel_dir="prorevenge")
        self.assertEqual(s, thumbnails._STYLES["aita"])

    def test_name_fallback_tifu(self) -> None:
        # "TIFU" substring triggers tifu branch
        s = thumbnails.style_from_channel({"name": "TIFU Stories"})
        self.assertEqual(s, thumbnails._STYLES["tifu"])

    def test_name_fallback_tih(self) -> None:
        s = thumbnails.style_from_channel({"name": "Today In History Daily"})
        self.assertEqual(s, thumbnails._STYLES["tih"])

    def test_name_fallback_wiki(self) -> None:
        s = thumbnails.style_from_channel({"name": "Wiki Oddities"})
        self.assertEqual(s, thumbnails._STYLES["oddities"])

    def test_name_fallback_aita(self) -> None:
        s = thumbnails.style_from_channel({"name": "AITA stories"})
        self.assertEqual(s, thumbnails._STYLES["aita"])

    def test_name_fallback_stories(self) -> None:
        s = thumbnails.style_from_channel({"name": "My Stories Animated"})
        self.assertEqual(s, thumbnails._STYLES["aita"])

    def test_default_style(self) -> None:
        s = thumbnails.style_from_channel({})
        self.assertEqual(s, thumbnails._STYLES["default"])

    def test_explicit_unknown_style_falls_through(self) -> None:
        yaml_cfg = {"upload": {"thumbnail": {"style": "nonexistent"}}}
        s = thumbnails.style_from_channel(yaml_cfg)
        self.assertEqual(s, thumbnails._STYLES["default"])

    def test_oddit_in_dir(self) -> None:
        s = thumbnails.style_from_channel({}, channel_dir="oddit_weekly")
        self.assertEqual(s, thumbnails._STYLES["oddities"])

    def test_tih_in_dir(self) -> None:
        s = thumbnails.style_from_channel({}, channel_dir="tih_channel")
        self.assertEqual(s, thumbnails._STYLES["tih"])

    def test_aita_in_dir(self) -> None:
        s = thumbnails.style_from_channel({}, channel_dir="aita_cooking")
        self.assertEqual(s, thumbnails._STYLES["aita"])


class TestCleanToken(unittest.TestCase):
    def test_strips_punctuation(self) -> None:
        self.assertEqual(thumbnails._clean_token("—hello!"), "hello")

    def test_preserves_content(self) -> None:
        self.assertEqual(thumbnails._clean_token("word"), "word")

    def test_empty_string(self) -> None:
        self.assertEqual(thumbnails._clean_token(""), "")


class TestCuriosityHeadline(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        self.assertEqual(thumbnails._curiosity_headline(""), "")

    def test_short_text_returned_as_is(self) -> None:
        h = thumbnails._curiosity_headline("HELLO WORLD", max_words=5)
        self.assertEqual(h, "HELLO WORLD")

    def test_strips_aita_prefix(self) -> None:
        h = thumbnails._curiosity_headline("AITA for refusing to split the bill")
        self.assertNotIn("AITA", h.upper().split()[0] if h else "")

    def test_strips_tifu_prefix(self) -> None:
        h = thumbnails._curiosity_headline("TIFU by trusting my neighbour")
        self.assertFalse(h.upper().startswith("TIFU"))

    def test_strips_wibta_prefix(self) -> None:
        h = thumbnails._curiosity_headline("WIBTA for leaving early")
        self.assertFalse(h.upper().startswith("WIBTA"))

    def test_strips_wibta_for_prefix(self) -> None:
        h = thumbnails._curiosity_headline("wibta for leaving early")
        self.assertFalse(h.upper().startswith("WIBTA"))

    def test_strips_aitah_prefix(self) -> None:
        h = thumbnails._curiosity_headline("AITAH refusing to help")
        self.assertNotIn("AITAH", h.upper())

    def test_strips_today_i_fucked_up_prefix(self) -> None:
        h = thumbnails._curiosity_headline("today i fucked up everything at work")
        self.assertFalse(h.upper().startswith("TODAY I FUCKED"))

    def test_strips_am_i_the_asshole_for(self) -> None:
        h = thumbnails._curiosity_headline("am i the asshole for walking out")
        self.assertFalse(h.upper().startswith("AM I"))

    def test_strips_am_i_wrong_for(self) -> None:
        h = thumbnails._curiosity_headline("am i wrong for asking twice")
        self.assertFalse(h.upper().startswith("AM I"))

    def test_long_text_capped_at_max_words(self) -> None:
        text = "the quick brown fox jumped over the lazy dog runs fast"
        h = thumbnails._curiosity_headline(text, max_words=4)
        words = h.split()
        self.assertLessEqual(len(words), 4)

    def test_fewer_than_2_content_words_falls_back(self) -> None:
        # Need >max_words total words AND all filler → keep < 2 → hits line 193 fallback.
        # "I is the and but or for at" = 8 filler words > max_words(5).
        h = thumbnails._curiosity_headline("I is the and but or for at", max_words=5)
        self.assertGreater(len(h), 0)

    def test_output_is_uppercase(self) -> None:
        h = thumbnails._curiosity_headline("refusing to leave")
        self.assertEqual(h, h.upper())


class TestHeadlineScore(unittest.TestCase):
    def test_digit_gives_bonus(self) -> None:
        s_digit, _ = thumbnails._headline_score("1 MILLION DOLLARS")
        s_plain, _ = thumbnails._headline_score("ONE MILLION DOLLARS")
        self.assertLess(s_digit, s_plain)

    def test_dollar_gives_bonus(self) -> None:
        s_dollar, _ = thumbnails._headline_score("$500 GONE")
        s_plain, _ = thumbnails._headline_score("MONEY GONE")
        self.assertLess(s_dollar, s_plain)

    def test_shock_word_gives_bonus(self) -> None:
        s_shock, _ = thumbnails._headline_score("REFUSED TO PAY")
        s_plain, _ = thumbnails._headline_score("FAILED TO PAY")
        self.assertLessEqual(s_shock, s_plain)

    def test_shorter_headline_scores_lower(self) -> None:
        s_short, _ = thumbnails._headline_score("SHORT")
        s_long, _ = thumbnails._headline_score("THIS IS A MUCH LONGER HEADLINE")
        self.assertLessEqual(s_short, s_long)

    def test_length_tiebreaker(self) -> None:
        _, l1 = thumbnails._headline_score("ABC")
        _, l2 = thumbnails._headline_score("ABCD")
        self.assertLess(l1, l2)


class TestLongestTokenChars(unittest.TestCase):
    def test_empty_string_returns_zero(self) -> None:
        self.assertEqual(thumbnails._longest_token_chars(""), 0)

    def test_single_word(self) -> None:
        self.assertEqual(thumbnails._longest_token_chars("HELLO"), 5)

    def test_picks_longest(self) -> None:
        self.assertEqual(thumbnails._longest_token_chars("AB CDEF GHI"), 4)


class TestPickHeadline(unittest.TestCase):
    _STYLE = thumbnails._STYLES["aita"]

    def test_empty_script_returns_fallback(self) -> None:
        h = thumbnails.pick_headline(script={}, style=self._STYLE)
        self.assertEqual(h, self._STYLE.headline_fallback)

    def test_hook_used(self) -> None:
        h = thumbnails.pick_headline(script={"hook": "AITA for refusing to pay"}, style=self._STYLE)
        self.assertIsInstance(h, str)
        self.assertTrue(len(h) > 0)

    def test_title_options_tried(self) -> None:
        h = thumbnails.pick_headline(
            script={"title_options": ["REFUSING TO PAY", None, ""]},
            style=self._STYLE,
        )
        self.assertIn("REFUSING", h)

    def test_very_long_token_rejected_returns_fallback(self) -> None:
        long_word = "A" * 20  # 20 chars > 14 → rejected for both cap=4 and cap=5
        h = thumbnails.pick_headline(
            script={"hook": long_word},
            style=self._STYLE,
        )
        self.assertEqual(h, self._STYLE.headline_fallback)

    def test_multiple_candidates_sorted(self) -> None:
        # With digit bonus, the numeric headline should win
        h = thumbnails.pick_headline(
            script={"hook": "100 DOLLARS GONE", "title_options": ["SOME STORY"]},
            style=self._STYLE,
        )
        self.assertIsInstance(h, str)

    def test_none_title_options_skipped(self) -> None:
        h = thumbnails.pick_headline(
            script={"hook": "REFUSED", "title_options": [None]},
            style=self._STYLE,
        )
        self.assertIsInstance(h, str)


class TestListSceneFrames(unittest.TestCase):
    def test_empty_dir_returns_empty(self) -> None:
        scratch = Path(tempfile.mkdtemp(dir=str(_BASE)))
        try:
            result = thumbnails.list_scene_frames(scratch)
            self.assertEqual(result, [])
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_returns_sorted_img_pngs(self) -> None:
        scratch = Path(tempfile.mkdtemp(dir=str(_BASE)))
        try:
            (scratch / "img_02.png").write_bytes(b"")
            (scratch / "img_00.png").write_bytes(b"")
            (scratch / "img_01.png").write_bytes(b"")
            result = thumbnails.list_scene_frames(scratch)
            names = [f.name for f in result]
            self.assertEqual(names, ["img_00.png", "img_01.png", "img_02.png"])
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


class TestPickScene(unittest.TestCase):
    def setUp(self) -> None:
        self._scratch = Path(tempfile.mkdtemp(dir=str(_BASE)))
        for i in range(3):
            (self._scratch / f"img_0{i}.png").write_bytes(b"")

    def tearDown(self) -> None:
        shutil.rmtree(self._scratch, ignore_errors=True)

    def test_no_frames_returns_none(self) -> None:
        empty = Path(tempfile.mkdtemp(dir=str(_BASE)))
        try:
            self.assertIsNone(thumbnails.pick_scene(empty))
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_default_returns_first_frame(self) -> None:
        result = thumbnails.pick_scene(self._scratch)
        self.assertEqual(result.name, "img_00.png")

    def test_prefer_index_returns_matching_frame(self) -> None:
        result = thumbnails.pick_scene(self._scratch, prefer_index=2)
        self.assertEqual(result.name, "img_02.png")

    def test_prefer_index_no_match_returns_first(self) -> None:
        result = thumbnails.pick_scene(self._scratch, prefer_index=99)
        self.assertEqual(result.name, "img_00.png")

    def test_invalid_stem_catches_value_error(self) -> None:
        # Create a file whose stem after split("_",1) isn't a valid int
        scratch = Path(tempfile.mkdtemp(dir=str(_BASE)))
        try:
            (scratch / "img_notanint.png").write_bytes(b"")
            result = thumbnails.pick_scene(scratch, prefer_index=0)
            # Falls through to frames[0]
            self.assertIsNotNone(result)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


class TestFitCover(unittest.TestCase):
    def test_wider_source_crops_width(self) -> None:
        src = Image.new("RGB", (400, 100))  # ar=4.0
        result = thumbnails._fit_cover(src, 100, 100)
        self.assertEqual(result.size, (100, 100))

    def test_taller_source_crops_height(self) -> None:
        src = Image.new("RGB", (100, 400))  # ar=0.25
        result = thumbnails._fit_cover(src, 100, 100)
        self.assertEqual(result.size, (100, 100))

    def test_square_source_square_dst(self) -> None:
        src = Image.new("RGB", (200, 200))
        result = thumbnails._fit_cover(src, 100, 100)
        self.assertEqual(result.size, (100, 100))


class TestSplitLongWord(unittest.TestCase):
    def test_no_split_for_simple_word(self) -> None:
        self.assertEqual(thumbnails._split_long_word("HELLO"), ["HELLO"])

    def test_splits_on_hyphen(self) -> None:
        result = thumbnails._split_long_word("DAUGHTER-IN-LAW")
        self.assertEqual(result, ["DAUGHTER-", "IN-", "LAW"])

    def test_splits_on_slash(self) -> None:
        result = thumbnails._split_long_word("A/B")
        self.assertEqual(result, ["A/", "B"])

    def test_single_hyphen_at_end(self) -> None:
        # e.g. "WORD-" produces ["WORD-"] only — one chunk with trailing hyphen
        result = thumbnails._split_long_word("WORD-")
        # "WORD-" splits into ["WORD-", ""] → out has 2 chunks → returns list
        self.assertIsInstance(result, list)


class TestFitText(unittest.TestCase):
    def _font(self, width_per_char: int = 8):
        return _mock_font_factory(width_per_char)

    def test_short_text_fits_at_large_size(self) -> None:
        font = self._font(8)
        with patch("pipeline.thumbnails._find_font", return_value=font):
            result_font, lines = thumbnails._fit_text(
                "HI", max_w=200, max_h=200, max_size=50
            )
        self.assertIsNotNone(result_font)
        self.assertEqual(lines, ["HI"])

    def test_long_text_wraps_to_multiple_lines(self) -> None:
        font = self._font(10)
        with patch("pipeline.thumbnails._find_font", return_value=font):
            # "HELLO" (50px) and "WORLD" (50px) each fit in max_w=60.
            # Combined "HELLO WORLD" is 110px > 60 → wraps.
            _f, lines = thumbnails._fit_text(
                "HELLO WORLD", max_w=60, max_h=500, max_size=50
            )
        self.assertGreater(len(lines), 1)

    def test_fallthrough_when_nothing_fits(self) -> None:
        # Font always reports 9999px wide → nothing fits → fallthrough.
        fat_font = MagicMock()
        fat_font.getbbox.return_value = (0, 0, 9999, 20)
        with patch("pipeline.thumbnails._find_font", return_value=fat_font):
            _f, lines = thumbnails._fit_text(
                "BIG", max_w=10, max_h=10, max_size=50, min_size=44
            )
        self.assertEqual(lines, ["BIG"])

    def test_hyphenated_token_splits_and_joins(self) -> None:
        font = self._font(5)
        with patch("pipeline.thumbnails._find_font", return_value=font):
            # "DAUGHTER-IN-LAW" splits → tokens try to join without space
            _f, lines = thumbnails._fit_text(
                "DAUGHTER-IN-LAW", max_w=200, max_h=200, max_size=50
            )
        self.assertIsNotNone(lines)

    def test_lines_too_tall_triggers_smaller_size(self) -> None:
        # Font reports narrow widths but we'll make max_h very small.
        font = self._font(2)  # narrow → all fits in width, but height is tight
        with patch("pipeline.thumbnails._find_font", return_value=font):
            # max_h=1 → no size can fit; fall through to min_size
            _f, lines = thumbnails._fit_text(
                "ONE TWO THREE FOUR", max_w=1000, max_h=1,
                max_size=50, min_size=44
            )
        self.assertIsNotNone(lines)


class TestDrawOutlinedText(unittest.TestCase):
    def setUp(self) -> None:
        self._img = Image.new("RGBA", (400, 200))
        self._draw = ImageDraw.Draw(self._img)
        # Use a real font — MagicMock fonts fail inside PIL's draw.text()
        # because PIL unpacks font.getmask2() as (mask, offset).
        self._font = _REAL_FONT

    def test_center_align(self) -> None:
        # Should not raise — PIL draw.text() needs a real font object.
        thumbnails._draw_outlined_text(
            self._draw, (200, 50), "HELLO",
            font=self._font,
            fill=(255, 255, 255),
            stroke=(0, 0, 0),
            align="center",
        )

    def test_left_align(self) -> None:
        thumbnails._draw_outlined_text(
            self._draw, (10, 50), "HELLO",
            font=self._font,
            fill=(255, 255, 255),
            stroke=(0, 0, 0),
            align="left",
        )


class TestPasteLayer(unittest.TestCase):
    def test_returns_full_canvas_image(self) -> None:
        layer = Image.new("RGBA", (50, 30), (255, 0, 0, 128))
        result = thumbnails._paste_layer((200, 100), layer, (10, 20))
        self.assertEqual(result.size, (200, 100))
        self.assertEqual(result.mode, "RGBA")


class TestComposeThumbnail(unittest.TestCase):
    def setUp(self) -> None:
        self._scratch = Path(tempfile.mkdtemp(dir=str(_BASE)))
        # Write a tiny scene image.
        scene_img = Image.new("RGB", (50, 100), (100, 150, 200))
        self._scene = self._scratch / "img_00.png"
        scene_img.save(self._scene)
        self._out = self._scratch / "thumb.jpg"
        # Real font required — compose_thumbnail calls draw.text() internally.
        self._font = _REAL_FONT

    def tearDown(self) -> None:
        shutil.rmtree(self._scratch, ignore_errors=True)

    def test_produces_jpeg_file(self) -> None:
        with patch("pipeline.thumbnails._find_font", return_value=self._font):
            result = thumbnails.compose_thumbnail(
                scene_path=self._scene,
                headline="TEST HEADLINE",
                style=thumbnails._STYLES["aita"],
                out_path=self._out,
                canvas_w=200,
                canvas_h=300,
            )
        self.assertEqual(result, self._out)
        self.assertTrue(self._out.exists())
        # Verify it's a valid image
        img = Image.open(self._out)
        self.assertEqual(img.format, "JPEG")

    def test_creates_parent_dir_if_needed(self) -> None:
        nested_out = self._scratch / "subdir" / "thumb.jpg"
        with patch("pipeline.thumbnails._find_font", return_value=self._font):
            thumbnails.compose_thumbnail(
                scene_path=self._scene,
                headline="NESTED",
                style=thumbnails._STYLES["default"],
                out_path=nested_out,
                canvas_w=200,
                canvas_h=300,
            )
        self.assertTrue(nested_out.exists())

    def test_all_styles(self) -> None:
        for style_name, style in thumbnails._STYLES.items():
            out = self._scratch / f"thumb_{style_name}.jpg"
            with patch("pipeline.thumbnails._find_font", return_value=self._font):
                thumbnails.compose_thumbnail(
                    scene_path=self._scene,
                    headline="HELLO",
                    style=style,
                    out_path=out,
                    canvas_w=200,
                    canvas_h=300,
                )
            self.assertTrue(out.exists(), f"style {style_name} failed")


class TestAutoThumbnail(unittest.TestCase):
    def setUp(self) -> None:
        self._scratch = Path(tempfile.mkdtemp(dir=str(_BASE)))
        scene_img = Image.new("RGB", (50, 100), (100, 150, 200))
        self._scene = self._scratch / "img_00.png"
        scene_img.save(self._scene)
        self._out = self._scratch / "auto.jpg"
        # Real font required — auto_thumbnail calls compose_thumbnail → draw.text().
        self._font = _REAL_FONT

    def tearDown(self) -> None:
        shutil.rmtree(self._scratch, ignore_errors=True)

    def test_no_scene_frames_returns_none(self) -> None:
        empty = Path(tempfile.mkdtemp(dir=str(_BASE)))
        try:
            result = thumbnails.auto_thumbnail(
                slug="s",
                cache_dir=empty,
                script={},
                channel_yaml={},
                out_path=self._out,
            )
            self.assertIsNone(result)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_style_override_applied(self) -> None:
        with patch("pipeline.thumbnails._find_font", return_value=self._font):
            result = thumbnails.auto_thumbnail(
                slug="s",
                cache_dir=self._scratch,
                script={"hook": "AITA FOR LEAVING"},
                channel_yaml={},
                out_path=self._out,
                style_override="tifu",
            )
        self.assertIsNotNone(result)

    def test_headline_override_used(self) -> None:
        with patch("pipeline.thumbnails._find_font", return_value=self._font):
            result = thumbnails.auto_thumbnail(
                slug="s",
                cache_dir=self._scratch,
                script={},
                channel_yaml={},
                out_path=self._out,
                headline_override="CUSTOM HEADLINE",
            )
        self.assertIsNotNone(result)

    def test_scene_index_override(self) -> None:
        # Create a second frame
        Image.new("RGB", (50, 100)).save(self._scratch / "img_01.png")
        with patch("pipeline.thumbnails._find_font", return_value=self._font):
            result = thumbnails.auto_thumbnail(
                slug="s",
                cache_dir=self._scratch,
                script={},
                channel_yaml={},
                out_path=self._out,
                scene_index_override=1,
            )
        self.assertIsNotNone(result)

    def test_unknown_style_override_falls_through_to_channel(self) -> None:
        with patch("pipeline.thumbnails._find_font", return_value=self._font):
            result = thumbnails.auto_thumbnail(
                slug="s",
                cache_dir=self._scratch,
                script={},
                channel_yaml={},
                out_path=self._out,
                style_override="does_not_exist",
            )
        self.assertIsNotNone(result)

    def test_auto_thumbnail_no_overrides(self) -> None:
        with patch("pipeline.thumbnails._find_font", return_value=self._font):
            result = thumbnails.auto_thumbnail(
                slug="s",
                cache_dir=self._scratch,
                script={"hook": "AITA for asking twice"},
                channel_yaml={"name": "AITA stories"},
                out_path=self._out,
                channel_dir="reddit_amitheasshole",
            )
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
