"""Tests for pipeline.social.x_scrape — 100% line coverage.

All network and browser I/O is mocked via playwright mock + sys.modules injection.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

import pipeline.social.x_scrape as x_scrape
from playwright.sync_api import TimeoutError as PWTimeoutError


# ---------------------------------------------------------------------------
# Helpers to build a full playwright mock chain
# ---------------------------------------------------------------------------

def _make_pw_mocks(
    *,
    articles_count: int = 2,
    sidenav_count: int = 1,
    evaluate_return=None,
):
    """Return (mock_pw_instance, mock_browser, mock_ctx, mock_page)."""
    mock_pw = MagicMock()
    mock_browser = MagicMock()
    mock_ctx = MagicMock()
    mock_page = MagicMock()

    mock_pw.chromium.launch.return_value = mock_browser
    mock_browser.new_context.return_value = mock_ctx
    mock_ctx.new_page.return_value = mock_page

    # Locator side-effect: different count() based on selector
    def _locator_side_effect(selector):
        loc = MagicMock()
        if "SideNav_AccountSwitcher" in selector:
            loc.count.return_value = sidenav_count
        else:
            loc.count.return_value = articles_count
        return loc

    mock_page.locator.side_effect = _locator_side_effect

    if evaluate_return is None:
        evaluate_return = [
            {
                "id": "tweet_001",
                "text": "Hello world",
                "likes": 100,
                "retweets": 50,
                "replies": 10,
                "views": 1000,
                "author": "Tester",
                "handle": "@tester",
                "posted_at": "2024-01-01T00:00:00Z",
                "is_verified": False,
                "url": "https://x.com/tester/status/tweet_001",
            }
        ]
    mock_page.evaluate.return_value = evaluate_return
    return mock_pw, mock_browser, mock_ctx, mock_page


# ---------------------------------------------------------------------------
# _pull_chrome_cookies
# ---------------------------------------------------------------------------

class PullChromeCookiesTest(unittest.TestCase):
    def _make_fake_cookie(self, *, name="auth_token", value="tok123",
                           domain=".x.com", path="/", secure=True,
                           expires=1999999999.0, same_site=None, http_only=True):
        c = MagicMock()
        c.name = name
        c.value = value
        c.domain = domain
        c.path = path
        c.secure = secure
        c.expires = expires
        c._rest = {}
        if same_site is not None:
            c._rest["SameSite"] = same_site
        if http_only:
            c._rest["HttpOnly"] = True
        return c

    def _mock_chrome_root_and_bc3(self, cookies, *, db_exists=True):
        mock_bc3 = MagicMock()
        mock_bc3.chrome.return_value = cookies

        mock_root = MagicMock()
        mock_db = MagicMock()
        mock_db.exists.return_value = db_exists
        mock_root.__truediv__.return_value.__truediv__.return_value = mock_db

        return mock_root, mock_bc3

    def test_basic_cookies_returned(self):
        cookie = self._make_fake_cookie(same_site="Lax")
        mock_root, mock_bc3 = self._mock_chrome_root_and_bc3([cookie])
        with patch("pipeline.social.x_scrape.CHROME_ROOT", mock_root):
            with patch.dict(sys.modules, {"browser_cookie3": mock_bc3}):
                result = x_scrape._pull_chrome_cookies("Profile 3")
        # Returns cookies for two domains (x.com + twitter.com) × 1 cookie each
        self.assertGreaterEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "auth_token")

    def test_db_not_found_raises_system_exit(self):
        mock_root, mock_bc3 = self._mock_chrome_root_and_bc3([], db_exists=False)
        with patch("pipeline.social.x_scrape.CHROME_ROOT", mock_root):
            with patch.dict(sys.modules, {"browser_cookie3": mock_bc3}):
                with self.assertRaises(SystemExit):
                    x_scrape._pull_chrome_cookies("Profile 3")

    def test_samesite_strict(self):
        cookie = self._make_fake_cookie(same_site="Strict")
        mock_root, mock_bc3 = self._mock_chrome_root_and_bc3([cookie])
        with patch("pipeline.social.x_scrape.CHROME_ROOT", mock_root):
            with patch.dict(sys.modules, {"browser_cookie3": mock_bc3}):
                result = x_scrape._pull_chrome_cookies("Profile 3")
        self.assertEqual(result[0]["sameSite"], "Strict")

    def test_samesite_none(self):
        cookie = self._make_fake_cookie(same_site="None")
        mock_root, mock_bc3 = self._mock_chrome_root_and_bc3([cookie])
        with patch("pipeline.social.x_scrape.CHROME_ROOT", mock_root):
            with patch.dict(sys.modules, {"browser_cookie3": mock_bc3}):
                result = x_scrape._pull_chrome_cookies("Profile 3")
        self.assertEqual(result[0]["sameSite"], "None")

    def test_samesite_unknown_defaults_to_lax(self):
        cookie = self._make_fake_cookie(same_site="WeirdValue")
        mock_root, mock_bc3 = self._mock_chrome_root_and_bc3([cookie])
        with patch("pipeline.social.x_scrape.CHROME_ROOT", mock_root):
            with patch.dict(sys.modules, {"browser_cookie3": mock_bc3}):
                result = x_scrape._pull_chrome_cookies("Profile 3")
        self.assertEqual(result[0]["sameSite"], "Lax")

    def test_samesite_from_lowercase_key(self):
        cookie = self._make_fake_cookie()
        cookie._rest = {"samesite": "Strict"}  # lowercase key
        mock_root, mock_bc3 = self._mock_chrome_root_and_bc3([cookie])
        with patch("pipeline.social.x_scrape.CHROME_ROOT", mock_root):
            with patch.dict(sys.modules, {"browser_cookie3": mock_bc3}):
                result = x_scrape._pull_chrome_cookies("Profile 3")
        self.assertEqual(result[0]["sameSite"], "Strict")

    def test_samesite_non_string_defaults_to_lax(self):
        cookie = self._make_fake_cookie()
        cookie._rest = {"SameSite": 42}  # non-string
        mock_root, mock_bc3 = self._mock_chrome_root_and_bc3([cookie])
        with patch("pipeline.social.x_scrape.CHROME_ROOT", mock_root):
            with patch.dict(sys.modules, {"browser_cookie3": mock_bc3}):
                result = x_scrape._pull_chrome_cookies("Profile 3")
        self.assertEqual(result[0]["sameSite"], "Lax")

    def test_httponly_from_lowercase_key(self):
        cookie = self._make_fake_cookie(http_only=False)
        cookie._rest = {"httponly": True}
        mock_root, mock_bc3 = self._mock_chrome_root_and_bc3([cookie])
        with patch("pipeline.social.x_scrape.CHROME_ROOT", mock_root):
            with patch.dict(sys.modules, {"browser_cookie3": mock_bc3}):
                result = x_scrape._pull_chrome_cookies("Profile 3")
        self.assertTrue(result[0]["httpOnly"])

    def test_expires_none_becomes_minus_one(self):
        cookie = self._make_fake_cookie(expires=None)
        mock_root, mock_bc3 = self._mock_chrome_root_and_bc3([cookie])
        with patch("pipeline.social.x_scrape.CHROME_ROOT", mock_root):
            with patch.dict(sys.modules, {"browser_cookie3": mock_bc3}):
                result = x_scrape._pull_chrome_cookies("Profile 3")
        self.assertEqual(result[0]["expires"], -1)

    def test_value_none_becomes_empty_string(self):
        cookie = self._make_fake_cookie(value=None)
        mock_root, mock_bc3 = self._mock_chrome_root_and_bc3([cookie])
        with patch("pipeline.social.x_scrape.CHROME_ROOT", mock_root):
            with patch.dict(sys.modules, {"browser_cookie3": mock_bc3}):
                result = x_scrape._pull_chrome_cookies("Profile 3")
        self.assertEqual(result[0]["value"], "")


# ---------------------------------------------------------------------------
# _logged_in
# ---------------------------------------------------------------------------

class LoggedInTest(unittest.TestCase):
    def test_returns_true_when_account_button_present(self):
        mock_page = MagicMock()
        mock_page.locator.return_value.count.return_value = 1
        self.assertTrue(x_scrape._logged_in(mock_page))

    def test_returns_false_when_button_absent(self):
        mock_page = MagicMock()
        mock_page.locator.return_value.count.return_value = 0
        self.assertFalse(x_scrape._logged_in(mock_page))


# ---------------------------------------------------------------------------
# scrape()
# ---------------------------------------------------------------------------

class ScrapeTest(unittest.TestCase):
    def setUp(self):
        # Use a real temp dir path for out_path (we mock write_text)
        self._out = pathlib.Path("_test_x_scrape_out.json")

    def _run_scrape(self, mock_pw, *, query="test query", tweet_url=None,
                    chrome_profile=None, session_exists=False,
                    scroll_passes=1, max_tweets=10):
        mock_session_state = MagicMock(spec=pathlib.Path)
        mock_session_state.exists.return_value = session_exists

        mock_session_dir = MagicMock(spec=pathlib.Path)

        with patch("pipeline.social.x_scrape.sync_playwright") as mock_sp:
            mock_sp.return_value.__enter__.return_value = mock_pw
            with patch("pipeline.social.x_scrape.SESSION_STATE", mock_session_state):
                with patch("pipeline.social.x_scrape.SESSION_DIR", mock_session_dir):
                    with patch("pathlib.Path.mkdir"):
                        with patch("pathlib.Path.write_text"):
                            return x_scrape.scrape(
                                query,
                                self._out,
                                tweet_url=tweet_url,
                                headless=True,
                                max_tweets=max_tweets,
                                scroll_passes=scroll_passes,
                                chrome_profile=chrome_profile,
                            )

    def test_scrape_with_query(self):
        mock_pw, _, _, _ = _make_pw_mocks()
        payload = self._run_scrape(mock_pw, query="test query")
        self.assertEqual(payload["query"], "test query")
        self.assertIn("tweets", payload)

    def test_scrape_with_tweet_url(self):
        mock_pw, _, _, _ = _make_pw_mocks()
        url = "https://x.com/someone/status/12345"
        payload = self._run_scrape(mock_pw, query=None, tweet_url=url)
        self.assertEqual(payload["url"], url)
        self.assertEqual(payload["query"], url)

    def test_scrape_no_query_no_url_raises(self):
        mock_pw, _, _, _ = _make_pw_mocks()
        with self.assertRaises(SystemExit):
            with patch("pipeline.social.x_scrape.sync_playwright") as mock_sp:
                mock_sp.return_value.__enter__.return_value = mock_pw
                with patch("pathlib.Path.mkdir"), patch("pathlib.Path.write_text"):
                    x_scrape.scrape(None, self._out)

    def test_scrape_reuses_session_state_when_no_profile(self):
        mock_pw, _, mock_ctx, _ = _make_pw_mocks()
        self._run_scrape(mock_pw, session_exists=True, chrome_profile=None)
        # browser.new_context called with storage_state in kwargs
        ctx_kwargs = mock_pw.chromium.launch.return_value.new_context.call_args[1]
        self.assertIn("storage_state", ctx_kwargs)

    def test_scrape_does_not_use_session_state_with_chrome_profile(self):
        mock_pw, _, mock_ctx, _ = _make_pw_mocks()
        mock_root = MagicMock()
        mock_db = MagicMock()
        mock_db.exists.return_value = True
        mock_root.__truediv__.return_value.__truediv__.return_value = mock_db
        mock_bc3 = MagicMock()
        # Return 3 auth cookies to avoid the warning
        auth_cookies = [
            MagicMock(name=n, value="v", domain=".x.com", path="/",
                      secure=True, expires=None, _rest={})
            for n in ("auth_token", "ct0", "twid")
        ]
        for c in auth_cookies:
            c.name = ["auth_token", "ct0", "twid"][auth_cookies.index(c)]
        mock_bc3.chrome.return_value = auth_cookies
        with patch("pipeline.social.x_scrape.CHROME_ROOT", mock_root):
            with patch.dict(sys.modules, {"browser_cookie3": mock_bc3}):
                self._run_scrape(mock_pw, session_exists=True, chrome_profile="Profile 1")
        ctx_kwargs = mock_pw.chromium.launch.return_value.new_context.call_args[1]
        self.assertNotIn("storage_state", ctx_kwargs)

    def test_scrape_chrome_profile_insufficient_auth_cookies_warning(self, capsys=None):
        """< 3 auth cookies triggers a WARNING print — just ensure no crash."""
        mock_pw, _, _, _ = _make_pw_mocks()
        mock_root = MagicMock()
        mock_db = MagicMock()
        mock_db.exists.return_value = True
        mock_root.__truediv__.return_value.__truediv__.return_value = mock_db
        mock_bc3 = MagicMock()
        # Only 1 auth cookie
        one_cookie = MagicMock()
        one_cookie.name = "auth_token"
        one_cookie.value = "v"
        one_cookie.domain = ".x.com"
        one_cookie.path = "/"
        one_cookie.secure = True
        one_cookie.expires = None
        one_cookie._rest = {}
        mock_bc3.chrome.return_value = [one_cookie]
        with patch("pipeline.social.x_scrape.CHROME_ROOT", mock_root):
            with patch.dict(sys.modules, {"browser_cookie3": mock_bc3}):
                # Should complete without error (warning is printed to stderr)
                self._run_scrape(mock_pw, chrome_profile="Profile 1")

    def test_scrape_logged_in_saves_session(self):
        mock_pw, _, mock_ctx, _ = _make_pw_mocks(sidenav_count=1)
        mock_session_dir = MagicMock(spec=pathlib.Path)
        mock_session_state = MagicMock(spec=pathlib.Path)
        mock_session_state.exists.return_value = False

        with patch("pipeline.social.x_scrape.sync_playwright") as mock_sp:
            mock_sp.return_value.__enter__.return_value = mock_pw
            with patch("pipeline.social.x_scrape.SESSION_STATE", mock_session_state):
                with patch("pipeline.social.x_scrape.SESSION_DIR", mock_session_dir):
                    with patch("pathlib.Path.mkdir"), patch("pathlib.Path.write_text"):
                        x_scrape.scrape("test", self._out, scroll_passes=1)
        # storage_state should be saved
        mock_ctx.storage_state.assert_called_once()

    def test_scrape_not_logged_in_no_session_save(self):
        mock_pw, _, mock_ctx, _ = _make_pw_mocks(sidenav_count=0)
        mock_session_state = MagicMock(spec=pathlib.Path)
        mock_session_state.exists.return_value = False
        with patch("pipeline.social.x_scrape.sync_playwright") as mock_sp:
            mock_sp.return_value.__enter__.return_value = mock_pw
            with patch("pipeline.social.x_scrape.SESSION_STATE", mock_session_state):
                with patch("pipeline.social.x_scrape.SESSION_DIR", MagicMock()):
                    with patch("pathlib.Path.mkdir"), patch("pathlib.Path.write_text"):
                        x_scrape.scrape("test", self._out, scroll_passes=1)
        mock_ctx.storage_state.assert_not_called()

    def test_scrape_max_tweets_stops_scrolling(self):
        """If max_tweets reached after first scroll pass, no further scrolling."""
        tweets = [
            {"id": f"t{i}", "text": f"tweet {i}", "likes": i, "retweets": 0,
             "replies": 0, "views": 0, "author": "u", "handle": "@u",
             "posted_at": None, "is_verified": False,
             "url": f"https://x.com/u/status/t{i}"}
            for i in range(10)
        ]
        mock_pw, _, _, mock_page = _make_pw_mocks(evaluate_return=tweets)
        payload = self._run_scrape(mock_pw, max_tweets=5, scroll_passes=4)
        # Should stop early — page.mouse.wheel called fewer times
        self.assertLessEqual(len(payload["tweets"]), 5)

    def test_scrape_sign_in_modal_timeout_with_screenshot_success(self):
        """No tweets visible → wait_for_selector times out → screenshot saved."""
        mock_pw, _, _, mock_page = _make_pw_mocks(articles_count=0)
        mock_page.wait_for_selector.side_effect = PWTimeoutError("sign-in timeout")
        # screenshot call should succeed
        mock_page.screenshot.return_value = None
        with patch("pipeline.social.x_scrape.sync_playwright") as mock_sp:
            mock_sp.return_value.__enter__.return_value = mock_pw
            with patch("pipeline.social.x_scrape.SESSION_STATE", MagicMock(exists=lambda: False)):
                with patch("pipeline.social.x_scrape.SESSION_DIR", MagicMock()):
                    with patch("pathlib.Path.mkdir"), patch("pathlib.Path.write_text"):
                        with patch("pathlib.Path.with_suffix", return_value=pathlib.Path("diag.png")):
                            payload = x_scrape.scrape("test", self._out, scroll_passes=1)
        self.assertIn("tweets", payload)

    def test_scrape_sign_in_modal_timeout_screenshot_also_fails(self):
        """No tweets → wait_for_selector timeout → screenshot also raises → no crash."""
        mock_pw, _, _, mock_page = _make_pw_mocks(articles_count=0)
        mock_page.wait_for_selector.side_effect = PWTimeoutError("timeout")
        mock_page.screenshot.side_effect = Exception("screenshot failed")
        with patch("pipeline.social.x_scrape.sync_playwright") as mock_sp:
            mock_sp.return_value.__enter__.return_value = mock_pw
            with patch("pipeline.social.x_scrape.SESSION_STATE", MagicMock(exists=lambda: False)):
                with patch("pipeline.social.x_scrape.SESSION_DIR", MagicMock()):
                    with patch("pathlib.Path.mkdir"), patch("pathlib.Path.write_text"):
                        with patch("pathlib.Path.with_suffix", return_value=pathlib.Path("diag.png")):
                            payload = x_scrape.scrape("test", self._out, scroll_passes=1)
        self.assertIn("tweets", payload)

    def test_scrape_goto_interrupted_retries(self):
        """goto raising 'interrupted' error retries up to 3 times."""
        mock_pw, _, _, mock_page = _make_pw_mocks()
        call_count = [0]

        def goto_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 2:
                raise Exception("interrupted by something")

        mock_page.goto.side_effect = goto_side_effect
        payload = self._run_scrape(mock_pw, scroll_passes=1)
        self.assertGreaterEqual(call_count[0], 2)
        self.assertIn("tweets", payload)

    def test_scrape_goto_interrupted_exceeds_retries_raises(self):
        """goto raising 'interrupted' 3 times → re-raised on third attempt."""
        mock_pw, _, _, mock_page = _make_pw_mocks()

        def always_interrupted(*args, **kwargs):
            raise Exception("interrupted by something")

        mock_page.goto.side_effect = always_interrupted
        with self.assertRaises(Exception, msg="interrupted"):
            self._run_scrape(mock_pw, scroll_passes=1)

    def test_scrape_goto_other_exception_raises_immediately(self):
        """goto raising a non-interrupted error raises immediately (no retry)."""
        mock_pw, _, _, mock_page = _make_pw_mocks()
        mock_page.goto.side_effect = Exception("connection refused")
        with self.assertRaises(Exception):
            self._run_scrape(mock_pw, scroll_passes=1)

    def test_scrape_goto_timeout_error_raises(self):
        """PWTimeoutError from goto propagates immediately."""
        mock_pw, _, _, mock_page = _make_pw_mocks()
        mock_page.goto.side_effect = PWTimeoutError("timeout")
        with self.assertRaises(PWTimeoutError):
            self._run_scrape(mock_pw, scroll_passes=1)

    def test_scrape_tweet_url_sets_query(self):
        """If query is None but tweet_url given, query falls back to tweet_url."""
        mock_pw, _, _, _ = _make_pw_mocks()
        url = "https://x.com/someone/status/99999"
        payload = self._run_scrape(mock_pw, query=None, tweet_url=url)
        self.assertEqual(payload["query"], url)

    def test_scrape_tweet_url_query_overrides_url_as_query(self):
        """If both query and tweet_url given, query is preserved."""
        mock_pw, _, _, _ = _make_pw_mocks()
        url = "https://x.com/someone/status/99999"
        payload = self._run_scrape(mock_pw, query="my query", tweet_url=url)
        self.assertEqual(payload["query"], "my query")


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class MainTest(unittest.TestCase):
    @patch("pipeline.social.x_scrape.scrape")
    def test_main_with_query(self, mock_scrape):
        mock_scrape.return_value = {
            "tweet_count": 3, "logged_in": True, "tweets": []
        }
        with patch("sys.argv", ["prog", "--query", "test query", "--out", "_test.json"]):
            x_scrape.main()
        mock_scrape.assert_called_once()
        call_kwargs = mock_scrape.call_args
        self.assertEqual(call_kwargs[0][0], "test query")

    @patch("pipeline.social.x_scrape.scrape")
    def test_main_with_tweet_url(self, mock_scrape):
        mock_scrape.return_value = {
            "tweet_count": 1, "logged_in": False, "tweets": []
        }
        with patch("sys.argv", [
            "prog", "--tweet-url", "https://x.com/user/status/123", "--out", "_test.json"
        ]):
            x_scrape.main()
        mock_scrape.assert_called_once()
        call_kwargs = mock_scrape.call_args
        self.assertEqual(call_kwargs[1]["tweet_url"], "https://x.com/user/status/123")


if __name__ == "__main__":
    unittest.main()
