"""Tests for pipeline.social.reddit_card — 100% line coverage.

PIL rendering is tested with Image.save mocked to avoid disk writes.
Utility functions are tested directly.
"""

from __future__ import annotations

import json
import math
import pathlib
import time
import unittest
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from PIL import Image, ImageDraw, ImageFont
import pipeline.social.reddit_card as rc


# ---------------------------------------------------------------------------
# _strip_urls
# ---------------------------------------------------------------------------

class StripUrlsTest(unittest.TestCase):
    def test_strips_https_url(self):
        text = "Check this out https://reddit.com/r/aita and also www.example.com"
        result = rc._strip_urls(text)
        self.assertNotIn("https://", result)
        self.assertNotIn("www.", result)
        self.assertIn("Check this out", result)

    def test_no_urls_unchanged(self):
        self.assertEqual(rc._strip_urls("No URLs here!"), "No URLs here!")

    def test_empty_string(self):
        self.assertEqual(rc._strip_urls(""), "")

    def test_none_treated_as_empty(self):
        result = rc._strip_urls(None)  # type: ignore[arg-type]
        self.assertEqual(result, "")


# ---------------------------------------------------------------------------
# _truncate_at_sentence
# ---------------------------------------------------------------------------

class TruncateAtSentenceTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        text = "Hello!"
        self.assertEqual(rc._truncate_at_sentence(text, max_chars=100), text)

    def test_truncate_at_sentence_boundary(self):
        # Sentence boundary is at position 12 ("Hello world!") which is >= 0.4*20=8
        text = "Hello world! " + "x" * 100
        result = rc._truncate_at_sentence(text, max_chars=20)
        self.assertTrue(result.endswith("…"))
        self.assertIn("Hello world", result)

    def test_truncate_falls_back_to_space(self):
        # Put the sentence boundary very early: "X. " followed by many chars
        # rfind(".") → position 1, 1 < 0.4 * 30 = 12 → fall back to space
        text = "X. " + "hello world " * 5
        result = rc._truncate_at_sentence(text, max_chars=30)
        self.assertTrue(result.endswith("…"))

    def test_truncate_hard_cut_no_space_or_sentence(self):
        # Long token with no spaces or sentence markers
        text = "A" * 200
        result = rc._truncate_at_sentence(text, max_chars=50)
        self.assertTrue(result.endswith("…"))
        self.assertEqual(len(result), 51)  # 50 chars + "…"

    def test_empty_string(self):
        self.assertEqual(rc._truncate_at_sentence("", max_chars=100), "")

    def test_exclamation_boundary(self):
        text = "Wow! " + "y" * 100
        result = rc._truncate_at_sentence(text, max_chars=10)
        # "!" is at index 3, max_chars*0.4 = 4.0 → 3 < 4 → fall back to space
        # Space is at index 4 → cut there
        self.assertTrue(result.endswith("…"))

    def test_question_mark_boundary(self):
        text = "AITA? " + "y" * 100
        result = rc._truncate_at_sentence(text, max_chars=10)
        self.assertTrue(result.endswith("…"))


# ---------------------------------------------------------------------------
# _font
# ---------------------------------------------------------------------------

class FontTest(unittest.TestCase):
    def test_returns_a_font_object(self):
        font = rc._font(30)
        self.assertIsNotNone(font)

    def test_bold_returns_a_font_object(self):
        font = rc._font(30, bold=True)
        self.assertIsNotNone(font)

    def test_falls_back_to_default_when_all_paths_fail(self):
        """When all candidate font paths raise OSError, load_default() is used."""
        _real_truetype = ImageFont.truetype

        def _truetype_se(path, *args, **kwargs):
            if isinstance(path, str):
                raise OSError("not found")
            return _real_truetype(path, *args, **kwargs)

        with patch.object(ImageFont, "truetype", side_effect=_truetype_se):
            font = rc._font(30)
        self.assertIsNotNone(font)

    def test_bold_falls_back_to_default(self):
        _real_truetype = ImageFont.truetype

        def _truetype_se(path, *args, **kwargs):
            if isinstance(path, str):
                raise OSError("not found")
            return _real_truetype(path, *args, **kwargs)

        with patch.object(ImageFont, "truetype", side_effect=_truetype_se):
            font = rc._font(30, bold=True)
        self.assertIsNotNone(font)


# ---------------------------------------------------------------------------
# _wrap
# ---------------------------------------------------------------------------

class WrapTest(unittest.TestCase):
    def test_empty_text_returns_empty_list(self):
        mock_draw = MagicMock()
        mock_font = MagicMock()
        self.assertEqual(rc._wrap("", mock_font, 100, mock_draw), [])

    def test_single_word_fits(self):
        mock_draw = MagicMock()
        mock_draw.textbbox.return_value = (0, 0, 50, 20)
        result = rc._wrap("hello", MagicMock(), 100, mock_draw)
        self.assertEqual(result, ["hello"])

    def test_multi_word_no_wrap(self):
        mock_draw = MagicMock()
        mock_draw.textbbox.return_value = (0, 0, 80, 20)
        result = rc._wrap("hello world", MagicMock(), 100, mock_draw)
        self.assertEqual(result, ["hello world"])

    def test_wraps_when_line_too_wide(self):
        mock_draw = MagicMock()
        call_count = [0]

        def textbbox_se(pos, text, font):
            # Multi-word candidates exceed max, single words fit
            if " " in text:
                return (0, 0, 200, 20)
            return (0, 0, 40, 20)

        mock_draw.textbbox.side_effect = textbbox_se
        result = rc._wrap("hello world test", MagicMock(), 100, mock_draw)
        self.assertGreater(len(result), 1)

    def test_single_oversized_word_still_appended(self):
        """A word wider than max_width with empty cur is still appended (no and cur guard)."""
        mock_draw = MagicMock()
        mock_draw.textbbox.return_value = (0, 0, 9999, 20)  # always exceeds
        result = rc._wrap("LongWord", MagicMock(), 100, mock_draw)
        self.assertEqual(result, ["LongWord"])


# ---------------------------------------------------------------------------
# _humanize_score
# ---------------------------------------------------------------------------

class HumanizeScoreTest(unittest.TestCase):
    def test_below_thousand(self):
        self.assertEqual(rc._humanize_score(999), "999")

    def test_thousands_with_decimal(self):
        self.assertEqual(rc._humanize_score(1_500), "1.5K")

    def test_exact_thousand_no_decimal(self):
        self.assertEqual(rc._humanize_score(1_000), "1K")

    def test_millions(self):
        self.assertEqual(rc._humanize_score(2_500_000), "2.5M")

    def test_exact_million_no_decimal(self):
        self.assertEqual(rc._humanize_score(1_000_000), "1M")


# ---------------------------------------------------------------------------
# _humanize_age
# ---------------------------------------------------------------------------

class HumanizeAgeTest(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(rc._humanize_age(None), "")

    def test_zero_returns_empty(self):
        self.assertEqual(rc._humanize_age(0), "")

    def test_minutes(self):
        ts = time.time() - 30 * 60  # 30 minutes ago
        result = rc._humanize_age(ts)
        self.assertRegex(result, r"^\d+m$")

    def test_hours(self):
        ts = time.time() - 5 * 3600  # 5 hours ago
        result = rc._humanize_age(ts)
        self.assertRegex(result, r"^\d+h$")

    def test_days(self):
        ts = time.time() - 3 * 86_400  # 3 days ago
        result = rc._humanize_age(ts)
        self.assertRegex(result, r"^\d+d$")

    def test_months(self):
        ts = time.time() - 35 * 86_400  # 35 days ago → months
        result = rc._humanize_age(ts)
        self.assertRegex(result, r"^\d+mo$")


# ---------------------------------------------------------------------------
# _draw_subreddit_pill, _draw_meta_line, _draw_engagement
# ---------------------------------------------------------------------------

class DrawHelpersTest(unittest.TestCase):
    def setUp(self):
        self.img = Image.new("RGB", (1080, 200), (26, 26, 27))
        self.draw = ImageDraw.Draw(self.img)

    def test_draw_subreddit_pill(self):
        right_x = rc._draw_subreddit_pill(self.img, self.draw, "AmItheAsshole", 60, 60)
        self.assertGreater(right_x, 60)

    def test_draw_meta_line_with_age(self):
        # Should not raise
        rc._draw_meta_line(self.draw, 60, 60, author="testuser", age="2h")

    def test_draw_meta_line_without_age(self):
        rc._draw_meta_line(self.draw, 60, 60, author="testuser", age="")

    def test_draw_engagement(self):
        rc._draw_engagement(self.draw, 60, 100, score=12500, comments=350)


# ---------------------------------------------------------------------------
# render_post_card
# ---------------------------------------------------------------------------

class RenderPostCardTest(unittest.TestCase):
    def _out(self):
        return pathlib.Path("_test_post_card.png")

    def test_basic_render_no_body(self):
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            result = rc.render_post_card(
                subreddit="AmItheAsshole",
                title="AITA for not sharing my food?",
                author="testuser",
                score=10000,
                num_comments=500,
                age="2h",
                out_path=self._out(),
            )
        self.assertEqual(result, self._out())

    def test_render_with_body_excerpt_no_ellipsis(self):
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            rc.render_post_card(
                subreddit="TIFU",
                title="TIFU by doing something",
                author="user2",
                score=8000,
                num_comments=300,
                age="4h",
                body_excerpt="Short story here.",
                out_path=self._out(),
            )

    def test_render_with_long_body_excerpt_ellipsis(self):
        """Body excerpt longer than max_body_lines triggers ellipsis rendering."""
        long_body = ("This is a very long sentence that goes on and on. " * 20)
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            rc.render_post_card(
                subreddit="AITA",
                title="Long post",
                author="user3",
                score=5000,
                num_comments=200,
                age="1d",
                body_excerpt=long_body,
                max_body_lines=2,
                out_path=self._out(),
            )

    def test_body_excerpt_with_urls_stripped(self):
        body = "Check this out https://example.com/very/long/url/here okay"
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            rc.render_post_card(
                subreddit="test",
                title="URL test",
                author="u",
                score=1000,
                num_comments=50,
                age="1h",
                body_excerpt=body,
                out_path=self._out(),
            )


# ---------------------------------------------------------------------------
# render_body_card
# ---------------------------------------------------------------------------

class RenderBodyCardTest(unittest.TestCase):
    def _out(self):
        return pathlib.Path("_test_body_card.png")

    def test_page_num_zero_no_indicator(self):
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            result = rc.render_body_card(
                subreddit="AmItheAsshole",
                title="My post title",
                body_chunk="Here is the first part of my story.",
                page_num=0,
                out_path=self._out(),
            )
        self.assertEqual(result, self._out())

    def test_page_num_positive_renders_indicator(self):
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            rc.render_body_card(
                subreddit="AmItheAsshole",
                title="My post title",
                body_chunk="Continuation of the story.",
                page_num=2,
                out_path=self._out(),
            )

    def test_title_longer_than_two_lines_truncated(self):
        """Title > 2 lines triggers the '...' truncation path."""
        long_title = " ".join(["word"] * 50)  # many words → wraps > 2 lines
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            rc.render_body_card(
                subreddit="AITA",
                title=long_title,
                body_chunk="Some body text.",
                page_num=1,
                out_path=self._out(),
            )

    def test_body_with_urls_stripped(self):
        body = "See https://example.com for details. The story continues."
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            rc.render_body_card(
                subreddit="test",
                title="URL in body",
                body_chunk=body,
                page_num=0,
                out_path=self._out(),
            )


# ---------------------------------------------------------------------------
# render_comment_card
# ---------------------------------------------------------------------------

class RenderCommentCardTest(unittest.TestCase):
    def _out(self):
        return pathlib.Path("_test_comment_card.png")

    def test_basic_render(self):
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            result = rc.render_comment_card(
                author="commenter",
                body="This is a great comment!",
                score=5000,
                age="1h",
                out_path=self._out(),
            )
        self.assertEqual(result, self._out())

    def test_with_age_empty(self):
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            rc.render_comment_card(
                author="anon",
                body="Short comment.",
                score=100,
                age="",
                out_path=self._out(),
            )

    def test_long_body_triggers_ellipsis(self):
        """Body text that wraps beyond max_body_lines triggers the has_ellipsis path."""
        long_body = " ".join(["interesting"] * 60)
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            rc.render_comment_card(
                author="user",
                body=long_body,
                score=200,
                age="3h",
                max_body_lines=2,
                out_path=self._out(),
            )

    def test_body_with_url_stripped(self):
        body = "Check https://example.com out. Interesting thread."
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            rc.render_comment_card(
                author="user",
                body=body,
                score=50,
                age="2h",
                out_path=self._out(),
            )


# ---------------------------------------------------------------------------
# render_verdict_card
# ---------------------------------------------------------------------------

class RenderVerdictCardTest(unittest.TestCase):
    def test_basic_render(self):
        out = pathlib.Path("_test_verdict_card.png")
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            result = rc.render_verdict_card(
                verdict="NTA",
                caption="The verdict is in",
                out_path=out,
            )
        self.assertEqual(result, out)

    def test_yta_verdict(self):
        out = pathlib.Path("_test_verdict_yta.png")
        with patch("pathlib.Path.mkdir"), patch.object(Image.Image, "save"):
            rc.render_verdict_card(
                verdict="YTA",
                caption="You were wrong",
                out_path=out,
            )


# ---------------------------------------------------------------------------
# paginate_text
# ---------------------------------------------------------------------------

class PaginateTextTest(unittest.TestCase):
    def test_empty_string_returns_empty_list(self):
        self.assertEqual(rc.paginate_text(""), [])

    def test_short_text_single_page(self):
        text = "Short sentence."
        pages = rc.paginate_text(text, chars_per_page=500)
        self.assertEqual(len(pages), 1)
        self.assertIn("Short sentence", pages[0])

    def test_long_text_multiple_pages(self):
        # Build a text with enough sentences to span multiple pages.
        sentence = "This is a sentence that has some length. "
        text = sentence * 20  # ~820 chars → at least 2 pages at chars_per_page=400
        pages = rc.paginate_text(text, chars_per_page=200)
        self.assertGreater(len(pages), 1)

    def test_single_long_sentence_stays_on_one_page(self):
        """A single sentence with no period separator goes on one page."""
        text = "word " * 100  # Many words but split by ". " produces one chunk
        pages = rc.paginate_text(text, chars_per_page=50)
        # All words end up in one page since there's no ". " separator
        self.assertEqual(len(pages), 1)

    def test_newlines_converted_to_spaces(self):
        text = "First sentence.\nSecond sentence."
        pages = rc.paginate_text(text)
        # Should not crash; content preserved
        combined = " ".join(pages)
        self.assertIn("First sentence", combined)
        self.assertIn("Second sentence", combined)

    def test_blank_sentences_skipped(self):
        text = "Good. . . Also good."
        pages = rc.paginate_text(text)
        # Should complete without error; at least one page
        self.assertGreater(len(pages), 0)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class MainTest(unittest.TestCase):
    def _bundle(self, *, with_body=True, num_comments=3):
        post = {
            "id": "abc123",
            "subreddit": "AmItheAsshole",
            "title": "AITA for doing X?",
            "author": "testuser",
            "score": 10000,
            "num_comments": 500,
            "upvote_ratio": 0.95,
            "created_utc": time.time() - 3600,
            "is_nsfw": False,
            "is_locked": False,
        }
        if with_body:
            post["selftext"] = "Here is my story. It goes on for a while."
        else:
            post["selftext"] = ""
        comments = [
            {"id": f"c{i}", "author": f"user{i}", "body": f"Comment {i}.", "score": i * 100}
            for i in range(num_comments)
        ]
        return {"post": post, "top_comments": comments}

    def test_main_renders_all_card_kinds(self):
        bundle_data = self._bundle()
        bundle_json = json.dumps(bundle_data)

        with patch("sys.argv", [
            "prog",
            "--bundle", "fake_bundle.json",
            "--out-dir", "_test_cards_out",
        ]):
            with patch("pathlib.Path.read_text", return_value=bundle_json), \
                 patch("pathlib.Path.mkdir"), \
                 patch("pathlib.Path.iterdir", return_value=[MagicMock()] * 5), \
                 patch.object(Image.Image, "save"):
                rc.main()

    def test_main_with_custom_verdict(self):
        bundle_data = self._bundle()
        bundle_json = json.dumps(bundle_data)

        with patch("sys.argv", [
            "prog",
            "--bundle", "fake_bundle.json",
            "--out-dir", "_test_cards_out",
            "--verdict", "YTA",
            "--verdict-caption", "You were the jerk",
        ]):
            with patch("pathlib.Path.read_text", return_value=bundle_json), \
                 patch("pathlib.Path.mkdir"), \
                 patch("pathlib.Path.iterdir", return_value=[MagicMock()] * 5), \
                 patch.object(Image.Image, "save"):
                rc.main()

    def test_main_post_without_body(self):
        bundle_data = self._bundle(with_body=False)
        bundle_json = json.dumps(bundle_data)

        with patch("sys.argv", [
            "prog",
            "--bundle", "fake_bundle.json",
            "--out-dir", "_test_cards_out",
        ]):
            with patch("pathlib.Path.read_text", return_value=bundle_json), \
                 patch("pathlib.Path.mkdir"), \
                 patch("pathlib.Path.iterdir", return_value=[]), \
                 patch.object(Image.Image, "save"):
                rc.main()

    def test_main_no_comments(self):
        bundle_data = self._bundle(num_comments=0)
        bundle_json = json.dumps(bundle_data)

        with patch("sys.argv", [
            "prog",
            "--bundle", "fake_bundle.json",
            "--out-dir", "_test_cards_out",
        ]):
            with patch("pathlib.Path.read_text", return_value=bundle_json), \
                 patch("pathlib.Path.mkdir"), \
                 patch("pathlib.Path.iterdir", return_value=[]), \
                 patch.object(Image.Image, "save"):
                rc.main()


if __name__ == "__main__":
    unittest.main()
