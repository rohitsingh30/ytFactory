"""Tests for pipeline.social.x_screenshot — 100% line coverage.

Playwright and yt-dlp are fully mocked; no real browser sessions or downloads.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest.mock import MagicMock, call, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

import pipeline.social.x_screenshot as x_screenshot
import pipeline.footage.yt_dlp_cloudrun as _ytdlp_real  # real module — needed for real exception classes
from playwright.sync_api import TimeoutError as PWTimeoutError


# ---------------------------------------------------------------------------
# Helpers: playwright mock chain
# ---------------------------------------------------------------------------

def _make_pw_mocks(*, media_eval=None):
    mock_pw = MagicMock()
    mock_browser = MagicMock()
    mock_ctx = MagicMock()
    mock_page = MagicMock()
    mock_article = MagicMock()

    mock_pw.chromium.launch.return_value = mock_browser
    mock_browser.new_context.return_value = mock_ctx
    mock_ctx.new_page.return_value = mock_page

    mock_article.bounding_box.return_value = {"width": 600, "height": 400}
    mock_page.locator.return_value.first = mock_article

    mock_page.evaluate.return_value = media_eval  # None → tested via `or` fallback
    return mock_pw, mock_browser, mock_ctx, mock_page, mock_article


# ---------------------------------------------------------------------------
# _slug_from_url
# ---------------------------------------------------------------------------

class SlugFromUrlTest(unittest.TestCase):
    def test_with_handle_and_status(self):
        url = "https://x.com/someone/status/1234567890"
        self.assertEqual(x_screenshot._slug_from_url(url), "someone_1234567890")

    def test_fallback_for_non_matching_url(self):
        slug = x_screenshot._slug_from_url("https://example.com/no/match")
        # Should be <= 48 chars, all word-chars-or-underscore
        self.assertLessEqual(len(slug), 48)
        self.assertRegex(slug, r"^\w+$")


# ---------------------------------------------------------------------------
# _normalize_tweet_url
# ---------------------------------------------------------------------------

class NormalizeTweetUrlTest(unittest.TestCase):
    def test_x_com_url(self):
        url = "https://x.com/elonmusk/status/12345678"
        canonical, tid = x_screenshot._normalize_tweet_url(url)
        self.assertEqual(canonical, "https://x.com/elonmusk/status/12345678")
        self.assertEqual(tid, "12345678")

    def test_twitter_com_url(self):
        url = "https://twitter.com/user/status/99999/photo/1"
        canonical, tid = x_screenshot._normalize_tweet_url(url)
        self.assertEqual(canonical, "https://x.com/user/status/99999")
        self.assertEqual(tid, "99999")

    def test_invalid_url_raises_value_error(self):
        with self.assertRaises(ValueError):
            x_screenshot._normalize_tweet_url("https://example.com/no/tweet/here")


# ---------------------------------------------------------------------------
# _download_tweet_video
# ---------------------------------------------------------------------------

class DownloadTweetVideoTest(unittest.TestCase):
    """Tests for _download_tweet_video use patch.object on the real yt_dlp_cloudrun
    module so the lazy `from pipeline.footage import yt_dlp_cloudrun` inside the
    function gets the real module with real exception classes — this is the only
    way the `except CloudRunYtDlpFailed/Unavailable` clauses can be exercised."""

    def _mock_path(self, *, exists=True, size=2048):
        p = MagicMock(spec=pathlib.Path)
        p.exists.return_value = exists
        stat = MagicMock()
        stat.st_size = size
        p.stat.return_value = stat
        return p

    def test_success_returns_true(self):
        out = self._mock_path(exists=True, size=2048)
        with patch.object(_ytdlp_real, "download"):  # download succeeds (no exception)
            result = x_screenshot._download_tweet_video("https://x.com/u/status/1", out)
        self.assertTrue(result)

    def test_file_too_small_returns_false(self):
        out = self._mock_path(exists=True, size=100)  # < 1024
        with patch.object(_ytdlp_real, "download"):
            result = x_screenshot._download_tweet_video("https://x.com/u/status/1", out)
        self.assertFalse(result)

    def test_file_not_exists_returns_false(self):
        out = self._mock_path(exists=False)
        with patch.object(_ytdlp_real, "download"):
            result = x_screenshot._download_tweet_video("https://x.com/u/status/1", out)
        self.assertFalse(result)

    def test_cloudrun_failed_returns_false(self):
        out = self._mock_path(exists=False)
        with patch.object(
            _ytdlp_real, "download",
            side_effect=_ytdlp_real.CloudRunYtDlpFailed("cloud fail"),
        ):
            result = x_screenshot._download_tweet_video("https://x.com/u/status/1", out)
        self.assertFalse(result)

    def test_cloudrun_unavailable_returns_false(self):
        out = self._mock_path(exists=False)
        with patch.object(
            _ytdlp_real, "download",
            side_effect=_ytdlp_real.CloudRunYtDlpUnavailable("unavailable"),
        ):
            result = x_screenshot._download_tweet_video("https://x.com/u/status/1", out)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# screenshot_tweets()
# ---------------------------------------------------------------------------

class ScreenshotTweetsTest(unittest.TestCase):
    _VALID_URL = "https://x.com/tester/status/111222333"
    _INVALID_URL = "https://example.com/not-a-tweet"

    def _run(
        self,
        urls,
        mock_pw,
        *,
        out_dir=pathlib.Path("/mock/out"),
        chrome_profile=None,
        dark=True,
        session_exists=False,
        pull_cookies_return=None,
    ):
        mock_session_state = MagicMock(spec=pathlib.Path)
        mock_session_state.exists.return_value = session_exists

        with patch("pipeline.social.x_screenshot.sync_playwright") as mock_sp, \
             patch("pipeline.social.x_screenshot._pull_chrome_cookies",
                   return_value=pull_cookies_return or []) as mock_pull, \
             patch("pipeline.social.x_screenshot.SESSION_STATE", mock_session_state), \
             patch("pathlib.Path.mkdir"):
            mock_sp.return_value.__enter__.return_value = mock_pw
            results = x_screenshot.screenshot_tweets(
                urls,
                out_dir,
                chrome_profile=chrome_profile,
                headless=True,
                dark=dark,
            )
        return results, mock_pull

    def test_success_no_media(self):
        mock_pw, _, _, mock_page, mock_article = _make_pw_mocks(media_eval={"kind": "none"})
        results, _ = self._run([self._VALID_URL], mock_pw)
        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0]["png"])
        self.assertEqual(results[0]["media_kind"], "none")

    def test_success_image_media(self):
        media = {"kind": "image", "region_frac": [0, 0.5, 1, 0.5]}
        mock_pw, _, _, mock_page, _ = _make_pw_mocks(media_eval=media)
        results, _ = self._run([self._VALID_URL], mock_pw)
        self.assertEqual(results[0]["media_kind"], "image")
        self.assertIsNone(results[0]["video_path"])

    def test_success_video_media_download_succeeds(self):
        media = {"kind": "video", "region_frac": [0, 0.5, 1, 0.5]}
        mock_pw, _, _, mock_page, _ = _make_pw_mocks(media_eval=media)
        with patch("pipeline.social.x_screenshot._download_tweet_video", return_value=True):
            results, _ = self._run([self._VALID_URL], mock_pw)
        self.assertEqual(results[0]["media_kind"], "video")
        self.assertIsNotNone(results[0]["video_path"])

    def test_success_video_media_download_fails(self):
        media = {"kind": "video", "region_frac": [0, 0.5, 1, 0.5]}
        mock_pw, _, _, mock_page, _ = _make_pw_mocks(media_eval=media)
        with patch("pipeline.social.x_screenshot._download_tweet_video", return_value=False):
            results, _ = self._run([self._VALID_URL], mock_pw)
        self.assertIsNone(results[0]["video_path"])

    def test_media_eval_none_falls_back_to_no_media(self):
        """page.evaluate returning None triggers the `or {"kind": "none"}` fallback."""
        mock_pw, _, _, mock_page, _ = _make_pw_mocks(media_eval=None)
        results, _ = self._run([self._VALID_URL], mock_pw)
        self.assertEqual(results[0]["media_kind"], "none")

    def test_invalid_url_skipped(self):
        mock_pw, _, _, _, _ = _make_pw_mocks()
        results, _ = self._run([self._INVALID_URL], mock_pw)
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["png"])
        self.assertIn("error", results[0])

    def test_timeout_appended_as_error(self):
        mock_pw, _, _, mock_page, _ = _make_pw_mocks()
        mock_page.goto.side_effect = PWTimeoutError("timeout")
        results, _ = self._run([self._VALID_URL], mock_pw)
        self.assertEqual(results[0]["error"], "timeout")
        self.assertIsNone(results[0]["png"])

    def test_generic_exception_appended_as_error(self):
        mock_pw, _, _, mock_page, _ = _make_pw_mocks()
        mock_page.goto.side_effect = Exception("some network error")
        results, _ = self._run([self._VALID_URL], mock_pw)
        self.assertIn("error", results[0])
        self.assertIsNone(results[0]["png"])

    def test_chrome_profile_pulls_cookies(self):
        mock_pw, _, mock_ctx, _, _ = _make_pw_mocks()
        fake_cookies = [{"name": "auth_token", "value": "tok", "domain": ".x.com",
                         "path": "/", "secure": True, "httpOnly": True,
                         "sameSite": "Lax", "expires": -1}]
        results, mock_pull = self._run(
            [self._VALID_URL], mock_pw,
            chrome_profile="Profile 3",
            pull_cookies_return=fake_cookies,
        )
        mock_pull.assert_called_once_with("Profile 3")
        mock_ctx.add_cookies.assert_called_once_with(fake_cookies)

    def test_session_state_used_when_no_profile(self):
        mock_pw, _, mock_browser_ctx, _, _ = _make_pw_mocks()
        self._run([self._VALID_URL], mock_pw, session_exists=True, chrome_profile=None)
        ctx_kwargs = mock_pw.chromium.launch.return_value.new_context.call_args[1]
        self.assertIn("storage_state", ctx_kwargs)

    def test_light_theme(self):
        mock_pw, _, _, _, _ = _make_pw_mocks()
        self._run([self._VALID_URL], mock_pw, dark=False)
        ctx_kwargs = mock_pw.chromium.launch.return_value.new_context.call_args[1]
        self.assertEqual(ctx_kwargs["color_scheme"], "light")

    def test_bounding_box_none_defaults_to_zero(self):
        mock_pw, _, _, mock_page, mock_article = _make_pw_mocks(media_eval={"kind": "none"})
        mock_article.bounding_box.return_value = None
        results, _ = self._run([self._VALID_URL], mock_pw)
        self.assertEqual(results[0]["width"], 0)
        self.assertEqual(results[0]["height"], 0)

    def test_multiple_urls_processed(self):
        mock_pw, _, _, _, _ = _make_pw_mocks(media_eval={"kind": "none"})
        urls = [
            "https://x.com/user1/status/111",
            "https://x.com/user2/status/222",
            self._INVALID_URL,
        ]
        results, _ = self._run(urls, mock_pw)
        self.assertEqual(len(results), 3)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class MainTest(unittest.TestCase):
    @patch("pipeline.social.x_screenshot.screenshot_tweets")
    def test_main_with_url_flag(self, mock_ss):
        mock_ss.return_value = [{"png": "out.png"}]
        with patch("sys.argv", [
            "prog", "--url", "https://x.com/u/status/1",
            "--out-dir", "_test_out_dir",
        ]):
            x_screenshot.main()
        mock_ss.assert_called_once()
        call_kwargs = mock_ss.call_args
        self.assertIn("https://x.com/u/status/1", call_kwargs[0][0])

    @patch("pipeline.social.x_screenshot.screenshot_tweets")
    def test_main_urls_file_list_of_strings(self, mock_ss):
        mock_ss.return_value = []
        data = ["https://x.com/u/status/1", "https://x.com/u/status/2"]
        with patch("sys.argv", ["prog", "--urls-file", "urls.json", "--out-dir", "_out"]):
            with patch("pathlib.Path.read_text", return_value=json.dumps(data)):
                x_screenshot.main()
        urls_arg = mock_ss.call_args[0][0]
        self.assertEqual(urls_arg, data)

    @patch("pipeline.social.x_screenshot.screenshot_tweets")
    def test_main_urls_file_list_of_dicts(self, mock_ss):
        mock_ss.return_value = []
        data = [{"url": "https://x.com/u/status/1"}, {"url": "https://x.com/u/status/2"}]
        with patch("sys.argv", ["prog", "--urls-file", "urls.json", "--out-dir", "_out"]):
            with patch("pathlib.Path.read_text", return_value=json.dumps(data)):
                x_screenshot.main()
        urls_arg = mock_ss.call_args[0][0]
        self.assertEqual(urls_arg, ["https://x.com/u/status/1", "https://x.com/u/status/2"])

    @patch("pipeline.social.x_screenshot.screenshot_tweets")
    def test_main_urls_file_tweets_dict(self, mock_ss):
        mock_ss.return_value = []
        data = {"tweets": [{"url": "https://x.com/u/status/99"}]}
        with patch("sys.argv", ["prog", "--urls-file", "urls.json", "--out-dir", "_out"]):
            with patch("pathlib.Path.read_text", return_value=json.dumps(data)):
                x_screenshot.main()
        urls_arg = mock_ss.call_args[0][0]
        self.assertEqual(urls_arg, ["https://x.com/u/status/99"])

    @patch("pipeline.social.x_screenshot.screenshot_tweets")
    def test_main_urls_file_tweets_dict_skips_missing_url(self, mock_ss):
        """Tweets without 'url' key are excluded."""
        mock_ss.return_value = []
        data = {"tweets": [{"url": "https://x.com/u/status/1"}, {"text": "no url here"}]}
        with patch("sys.argv", ["prog", "--urls-file", "urls.json", "--out-dir", "_out"]):
            with patch("pathlib.Path.read_text", return_value=json.dumps(data)):
                x_screenshot.main()
        urls_arg = mock_ss.call_args[0][0]
        self.assertEqual(len(urls_arg), 1)

    def test_main_urls_file_unknown_shape_raises(self):
        data = {"completely_wrong": "shape"}
        with patch("sys.argv", ["prog", "--urls-file", "urls.json", "--out-dir", "_out"]):
            with patch("pathlib.Path.read_text", return_value=json.dumps(data)):
                with self.assertRaises(SystemExit):
                    x_screenshot.main()

    @patch("pipeline.social.x_screenshot.screenshot_tweets")
    def test_main_writes_manifest(self, mock_ss):
        results = [{"url": "https://x.com/u/status/1", "png": "out.png"}]
        mock_ss.return_value = results
        with patch("sys.argv", [
            "prog",
            "--url", "https://x.com/u/status/1",
            "--out-dir", "_out",
            "--manifest", "_manifest.json",
        ]):
            with patch("pathlib.Path.mkdir"), patch("pathlib.Path.write_text") as mock_wt:
                x_screenshot.main()
        mock_wt.assert_called_once_with(json.dumps(results, indent=2))

    @patch("pipeline.social.x_screenshot.screenshot_tweets")
    def test_main_light_flag(self, mock_ss):
        mock_ss.return_value = []
        with patch("sys.argv", [
            "prog", "--url", "https://x.com/u/status/1",
            "--out-dir", "_out", "--light",
        ]):
            x_screenshot.main()
        call_kwargs = mock_ss.call_args[1]
        self.assertFalse(call_kwargs["dark"])

    @patch("pipeline.social.x_screenshot.screenshot_tweets")
    def test_main_no_headless_flag(self, mock_ss):
        mock_ss.return_value = []
        with patch("sys.argv", [
            "prog", "--url", "https://x.com/u/status/1",
            "--out-dir", "_out", "--no-headless",
        ]):
            x_screenshot.main()
        call_kwargs = mock_ss.call_args[1]
        self.assertFalse(call_kwargs["headless"])


if __name__ == "__main__":
    unittest.main()
