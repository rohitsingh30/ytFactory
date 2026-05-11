"""Tests for pipeline.cross_engage.cross_engage_burner_attached — 100% line coverage.

Patch targets: functions imported INTO cross_engage_burner_attached are patched
under pipeline.cross_engage.cross_engage_burner_attached.<name>, NOT under
their source module.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests._helpers import FakePath, make_fake_playwright

import pipeline.cross_engage.cross_engage_burner_attached as _mod
from pipeline.cross_engage.cross_engage_burner_attached import (
    _human_pause,
    _new_window_page,
    _open_avatar_menu,
    clone_chrome_debug_to_engage_udd,
    engage_video,
    harvest_cookies_via_cdp,
    list_known_burners,
    resolve_burner,
    switch_to_burner_brand,
)

# Shortcut for patch path
_P = "pipeline.cross_engage.cross_engage_burner_attached"


# ── clone_chrome_debug_to_engage_udd ────────────────────────────────────────

class TestCloneChromeDebugToEngageUdd(unittest.TestCase):
    def test_src_not_found_raises(self):
        src = FakePath("Chrome-Debug")
        profile_dir = FakePath("Profile 1", exists=False)
        src._children["Profile 1"] = profile_dir
        dst = FakePath("Chrome-Debug-engage")
        with self.assertRaises(FileNotFoundError):
            clone_chrome_debug_to_engage_udd("Profile 1", src_udd=src, dst_udd=dst)

    def test_copies_profile(self):
        import shutil as shutil_mod

        src = FakePath("Chrome-Debug")
        profile_src = FakePath("Profile 1", exists=True, is_dir=True)
        ls_src = FakePath("Local State", exists=True, is_dir=False)
        ls_src._bytes_data = b"state"
        src._children["Local State"] = ls_src
        src._children["Profile 1"] = profile_src

        # One dir entry, one file entry inside the profile
        dir_entry = FakePath("Cache", exists=True, is_dir=True)
        dir_entry._children["data"] = FakePath("data", exists=True)
        file_entry = FakePath("Preferences", exists=True, is_dir=False)
        profile_src._children["Cache"] = dir_entry
        profile_src._children["Preferences"] = file_entry

        dst = FakePath("Chrome-Debug-engage")

        with patch("shutil.copytree") as mock_copytree, \
             patch("shutil.copy2") as mock_copy2:
            clone_chrome_debug_to_engage_udd("Profile 1", src_udd=src, dst_udd=dst)

        mock_copytree.assert_called_once()
        mock_copy2.assert_called_once()

    def test_oserror_on_entries_skipped(self):
        src = FakePath("Chrome-Debug")
        profile_src = FakePath("Profile 1", exists=True, is_dir=True)
        ls_src = FakePath("Local State", exists=True, is_dir=False)
        ls_src._bytes_data = b"state"
        src._children["Local State"] = ls_src
        src._children["Profile 1"] = profile_src

        file_entry = FakePath("Cookies", exists=True, is_dir=False)
        profile_src._children["Cookies"] = file_entry
        dst = FakePath("Chrome-Debug-engage")

        with patch("shutil.copy2", side_effect=OSError("locked")):
            # Should not raise — OSError is swallowed per entry
            clone_chrome_debug_to_engage_udd("Profile 1", src_udd=src, dst_udd=dst)

    def test_no_local_state(self):
        src = FakePath("Chrome-Debug")
        profile_src = FakePath("Profile 1", exists=True, is_dir=True)
        # No "Local State" child → exists() returns False via auto-create
        src._children["Profile 1"] = profile_src
        # Make sure "Local State" doesn't exist
        ls = FakePath("Local State", exists=False)
        src._children["Local State"] = ls

        dst = FakePath("Chrome-Debug-engage")
        clone_chrome_debug_to_engage_udd("Profile 1", src_udd=src, dst_udd=dst)

    def test_local_state_write_oserror(self):
        src = FakePath("Chrome-Debug")
        profile_src = FakePath("Profile 1", exists=True, is_dir=True)
        ls_src = FakePath("Local State", exists=True, is_dir=False)
        ls_src._bytes_data = b"state"
        src._children["Local State"] = ls_src
        src._children["Profile 1"] = profile_src

        dst = FakePath("Chrome-Debug-engage")
        # Override write_bytes on the dst/Local State child to raise
        ls_dst = FakePath("Local State", exists=False)
        ls_dst.write_bytes = lambda data: (_ for _ in ()).throw(OSError("locked"))  # type: ignore[assignment]
        dst._children["Local State"] = ls_dst

        clone_chrome_debug_to_engage_udd("Profile 1", src_udd=src, dst_udd=dst)


# ── harvest_cookies_via_cdp ──────────────────────────────────────────────────

class TestHarvestCookiesViaCdp(unittest.TestCase):
    def test_returns_cookies(self):
        mock_sp, pw, browser, ctx, page = make_fake_playwright()
        cookies = [{"name": "SID", "value": "abc"}]
        ctx.cookies.return_value = cookies

        with patch("playwright.sync_api.sync_playwright", mock_sp):
            result = harvest_cookies_via_cdp(55000)

        self.assertEqual(result, cookies)
        browser.close.assert_called_once()

    def test_close_exception_swallowed(self):
        mock_sp, pw, browser, ctx, page = make_fake_playwright()
        ctx.cookies.return_value = [{"name": "x"}]
        browser.close.side_effect = Exception("close error")

        with patch("playwright.sync_api.sync_playwright", mock_sp):
            result = harvest_cookies_via_cdp(55001)
        self.assertEqual(len(result), 1)


# ── list_known_burners ────────────────────────────────────────────────────────

class TestListKnownBurners(unittest.TestCase):
    def test_returns_burners_not_in_production(self):
        registry = [{"key": "prod1"}]
        ids = {
            "prod1": {"channel_id": "UC_prod", "title": "Prod"},
            "burner1": {"channel_id": "UC_b1", "title": "Burner1"},
        }
        pmap = {"burner1": {"email": "b@example.com"}}
        with patch("pipeline.schemas.customization.CHANNEL_REGISTRY", registry), \
             patch(f"{_P}._load_json", side_effect=[ids, pmap]):
            result = list_known_burners()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["slug"], "burner1")

    def test_no_email_excluded(self):
        registry = []
        ids = {"b1": {"channel_id": "UC_b1"}}
        pmap = {}  # no email mapping
        with patch("pipeline.schemas.customization.CHANNEL_REGISTRY", registry), \
             patch(f"{_P}._load_json", side_effect=[ids, pmap]):
            result = list_known_burners()
        self.assertEqual(result, [])

    def test_prod_slug_excluded(self):
        registry = [{"key": "b1"}]
        ids = {"b1": {"channel_id": "UC_b1"}}
        pmap = {"b1": {"email": "x@e.com"}}
        with patch("pipeline.schemas.customization.CHANNEL_REGISTRY", registry), \
             patch(f"{_P}._load_json", side_effect=[ids, pmap]):
            result = list_known_burners()
        self.assertEqual(result, [])


# ── resolve_burner ────────────────────────────────────────────────────────────

class TestResolveBurner(unittest.TestCase):
    def test_no_burners_raises(self):
        with patch.object(_mod, "list_known_burners", return_value=[]):
            with self.assertRaises(SystemExit):
                resolve_burner(None)

    def test_slug_not_found_raises(self):
        burners = [{"slug": "b1", "email": "e@e.com", "channel_id": "UC"}]
        with patch.object(_mod, "list_known_burners", return_value=burners):
            with self.assertRaises(SystemExit):
                resolve_burner("nope")

    def test_slug_found(self):
        burners = [{"slug": "b1", "email": "e@e.com", "channel_id": "UC"}]
        with patch.object(_mod, "list_known_burners", return_value=burners):
            result = resolve_burner("b1")
        self.assertEqual(result["slug"], "b1")

    def test_single_burner_auto_select(self):
        burners = [{"slug": "b1", "email": "e@e.com", "channel_id": "UC"}]
        with patch.object(_mod, "list_known_burners", return_value=burners):
            result = resolve_burner(None)
        self.assertEqual(result["slug"], "b1")

    def test_multiple_no_slug_raises(self):
        burners = [{"slug": "b1", "email": "e@e.com"}, {"slug": "b2", "email": "e2@e.com"}]
        with patch.object(_mod, "list_known_burners", return_value=burners):
            with self.assertRaises(SystemExit):
                resolve_burner(None)


# ── _open_avatar_menu ─────────────────────────────────────────────────────────

class TestOpenAvatarMenu(unittest.TestCase):
    def test_clicks_avatar(self):
        # Post 2026-05-10 _open_avatar_menu first wait_for's the
        # masthead container, then calls _dismiss_consent (which would
        # also click via the same MagicMock-shared locator), then
        # iterates avatar selectors. Patch _dismiss_consent out so
        # the only click counted is the avatar click itself.
        page = MagicMock()
        avatar = MagicMock()
        page.locator.return_value.first = avatar
        with patch(f"{_P}._dismiss_consent"), patch("time.sleep"):
            _open_avatar_menu(page)
        # wait_for fires for the masthead gate (timeout=15000) AND for
        # the first avatar selector (timeout=3500); both share the
        # same Mock since page.locator returns the same object.
        self.assertEqual(avatar.wait_for.call_count, 2)
        avatar.click.assert_called_once()


# ── _human_pause ──────────────────────────────────────────────────────────────

class TestHumanPause(unittest.TestCase):
    def test_sleeps(self):
        with patch("time.sleep") as mock_sleep:
            _human_pause(0.0, 0.0)
        mock_sleep.assert_called_once()


# ── _new_window_page ──────────────────────────────────────────────────────────

class TestNewWindowPage(unittest.TestCase):
    def test_finds_by_url_prefix(self):
        browser = MagicMock()
        ctx = MagicMock()
        page = MagicMock()
        page.url = "https://www.youtube.com/watch?v=abc123&extra=1"
        page._impl_obj = None

        session = MagicMock()
        session.send.return_value = {"targetId": "T1"}
        browser.new_browser_cdp_session.return_value = session
        ctx.pages = [page]

        with patch("time.time", side_effect=[0, 0, 0.1, 0.2]):
            result = _new_window_page(browser, ctx, "https://www.youtube.com/watch?v=abc123")

        self.assertIs(result, page)

    def test_target_id_match(self):
        browser = MagicMock()
        ctx = MagicMock()
        page = MagicMock()

        impl = MagicMock()
        impl._initializer = {"targetId": "T2"}
        page._impl_obj = impl
        page.url = "https://www.youtube.com/"
        ctx.pages = [page]

        session = MagicMock()
        session.send.return_value = {"targetId": "T2"}
        browser.new_browser_cdp_session.return_value = session

        with patch("time.time", side_effect=[0, 0, 0.1]):
            result = _new_window_page(browser, ctx, "https://www.youtube.com/")

        self.assertIs(result, page)

    def test_returns_last_page_on_timeout(self):
        browser = MagicMock()
        ctx = MagicMock()
        page = MagicMock()
        page._impl_obj = None
        page.url = "https://other.com/"
        ctx.pages = [page]

        session = MagicMock()
        session.send.return_value = {"targetId": "T_NONE"}
        browser.new_browser_cdp_session.return_value = session

        with patch("time.time", side_effect=[0, 100]):
            result = _new_window_page(browser, ctx, "https://youtube.com/watch?v=xyz", timeout_ms=1)

        self.assertIs(result, page)

    def test_returns_none_on_timeout_no_pages(self):
        browser = MagicMock()
        ctx = MagicMock()
        ctx.pages = []

        session = MagicMock()
        session.send.return_value = {"targetId": "T"}
        session.detach.side_effect = Exception("err")
        browser.new_browser_cdp_session.return_value = session

        with patch("time.time", side_effect=[0, 100]):
            result = _new_window_page(browser, ctx, "https://x.com/", timeout_ms=1)

        self.assertIsNone(result)

    def test_page_url_exception_in_loop(self):
        """URL raises during loop iteration → continue to next page."""
        browser = MagicMock()
        ctx = MagicMock()

        bad_page = MagicMock()
        bad_page._impl_obj = None
        type(bad_page).url = property(lambda self: (_ for _ in ()).throw(Exception("dead")))  # type: ignore

        good_page = MagicMock()
        good_page._impl_obj = None
        good_page.url = "https://www.youtube.com/watch?v=X"
        ctx.pages = [bad_page, good_page]

        session = MagicMock()
        session.send.return_value = {"targetId": "TX"}
        browser.new_browser_cdp_session.return_value = session

        # Allow one loop iteration before timeout
        with patch("time.time", side_effect=[0, 0.1, 0.2, 100]):
            result = _new_window_page(browser, ctx, "https://www.youtube.com/watch?v=X")

        self.assertIs(result, good_page)

    def test_impl_initializer_get_exception_falls_back_to_url(self):
        browser = MagicMock()
        ctx = MagicMock()

        class BadInitializer:
            def get(self, key):
                raise Exception("bad initializer")

        bad_page = MagicMock()
        bad_impl = MagicMock()
        bad_impl._initializer = BadInitializer()
        bad_page._impl_obj = bad_impl
        bad_page.url = "https://other.example/"

        good_page = MagicMock()
        good_page._impl_obj = None
        good_page.url = "https://www.youtube.com/watch?v=X"
        ctx.pages = [bad_page, good_page]

        session = MagicMock()
        session.send.return_value = {"targetId": "TX"}
        browser.new_browser_cdp_session.return_value = session

        with patch("time.time", side_effect=[0, 0.1, 100]):
            result = _new_window_page(browser, ctx, "https://www.youtube.com/watch?v=X")
        self.assertIs(result, good_page)

    def test_poll_loop_sleeps_before_timeout(self):
        browser = MagicMock()
        ctx = MagicMock()
        page = MagicMock()
        page._impl_obj = None
        page.url = "https://other.example/"
        ctx.pages = [page]

        session = MagicMock()
        session.send.return_value = {"targetId": "T_NONE"}
        browser.new_browser_cdp_session.return_value = session

        with patch("time.time", side_effect=[0, 0.1, 100]), \
             patch("time.sleep") as mock_sleep:
            result = _new_window_page(browser, ctx, "https://youtube.com/watch?v=xyz")
        self.assertIs(result, page)
        mock_sleep.assert_called_with(0.2)


# ── switch_to_burner_brand ────────────────────────────────────────────────────

class TestSwitchToBurnerBrand(unittest.TestCase):
    def _make_burner(self, channel_id="UC_burner", title="BurnerCh"):
        return {"slug": "b1", "channel_id": channel_id, "title": title}

    def test_already_active(self):
        """When the verify page URL already contains the burner's UC ID, return True immediately."""
        page = MagicMock()
        ctx = MagicMock()
        page.context = ctx
        burner = self._make_burner("UC12345678901234567890")

        verify_page = MagicMock()
        # URL contains the burner's UC ID
        verify_page.url = "https://studio.youtube.com/channel/UC12345678901234567890"
        ctx.new_page.return_value = verify_page
        verify_page.locator.return_value.first.is_visible.return_value = True

        work_dir = FakePath("work")
        with patch("time.sleep"), patch.object(_mod, "_open_avatar_menu"):
            result = switch_to_burner_brand(page, work_dir, burner=burner)
        self.assertTrue(result)

    def test_already_active_verify_exceptions_swallowed(self):
        page = MagicMock()
        ctx = MagicMock()
        page.context = ctx
        burner = self._make_burner("UC12345678901234567890")

        verify_page = MagicMock()
        verify_page.url = "https://studio.youtube.com/channel/UC12345678901234567890"
        verify_page.wait_for_url.side_effect = Exception("url timeout")
        continue_btn = MagicMock()
        continue_btn.click.side_effect = Exception("no modal")
        verify_page.locator.return_value.first = continue_btn
        verify_page.close.side_effect = Exception("close failed")
        ctx.new_page.return_value = verify_page

        with patch("time.sleep"):
            result = switch_to_burner_brand(page, FakePath("work"), burner=burner)
        self.assertTrue(result)

    def test_switch_not_visible_raises(self):
        """Switch-account button not visible → SystemExit."""
        page = MagicMock()
        ctx = MagicMock()
        page.context = ctx
        burner = self._make_burner("UCdifferent00000000000")

        # Each _read_active_uc call may open 2 pages (studio probe +
        # /account fallback when studio.url has no UC). Two probe calls
        # × 2 pages = 4 mock pages.
        def _mock_page(url):
            p = MagicMock()
            p.url = url
            p.content.return_value = "<html><body>no UC here</body></html>"
            return p
        ctx.new_page.side_effect = [
            _mock_page("https://studio.youtube.com/channel/UC_OTHER0000000000000"),
            _mock_page("https://www.youtube.com/account"),
            _mock_page("https://studio.youtube.com/channel/UC_OTHER0000000000000"),
            _mock_page("https://www.youtube.com/account"),
        ]

        switch = MagicMock()
        switch.is_visible.return_value = False
        page.locator.return_value.first = switch

        work_dir = FakePath("work")
        with patch("time.sleep"), patch.object(_mod, "_open_avatar_menu"):
            with self.assertRaises(SystemExit):
                switch_to_burner_brand(page, work_dir, burner=burner)

    def test_burner_row_not_visible_raises(self):
        """Burner title row not visible → SystemExit."""
        page = MagicMock()
        ctx = MagicMock()
        page.context = ctx
        burner = self._make_burner("UCdifferent00000000000", "MyBurner")

        # Same pattern — 2 probe calls × up to 2 pages.
        def _mock_page(url):
            p = MagicMock()
            p.url = url
            p.content.return_value = "<html><body>no UC here</body></html>"
            return p
        ctx.new_page.side_effect = [
            _mock_page("https://studio.youtube.com/channel/UC_OTHER0000000000000"),
            _mock_page("https://www.youtube.com/account"),
            _mock_page("https://studio.youtube.com/channel/UC_OTHER0000000000000"),
            _mock_page("https://www.youtube.com/account"),
        ]

        switch = MagicMock()
        switch.is_visible.return_value = True
        target_row = MagicMock()
        target_row.is_visible.return_value = False

        call_n = {"n": 0}
        def locator_side(sel):
            lm = MagicMock()
            call_n["n"] += 1
            lm.first = switch if "Switch account" in sel else target_row
            return lm
        page.locator.side_effect = locator_side

        work_dir = FakePath("work")
        with patch("time.sleep"), patch.object(_mod, "_open_avatar_menu"):
            with self.assertRaises(SystemExit):
                switch_to_burner_brand(page, work_dir, burner=burner)

    def test_switch_success(self):
        """Switch succeeds: verify page URL shows correct UC ID after click."""
        page = MagicMock()
        ctx = MagicMock()
        page.context = ctx
        burner = self._make_burner("UC12345678901234567890", "MyBurner")

        # First verify: wrong UC. Second verify: correct UC (post-switch).
        verify_p1 = MagicMock()
        verify_p1.url = "https://studio.youtube.com/channel/UCtotally_wrong_0000000"
        verify_p2 = MagicMock()
        verify_p2.url = "https://studio.youtube.com/channel/UC12345678901234567890"
        ctx.new_page.side_effect = [verify_p1, verify_p2]

        switch = MagicMock()
        switch.is_visible.return_value = True
        target_row = MagicMock()
        target_row.is_visible.return_value = True

        def locator_side(sel):
            lm = MagicMock()
            lm.first = switch if "Switch account" in sel else target_row
            return lm
        page.locator.side_effect = locator_side

        work_dir = FakePath("work")
        with patch("time.sleep"), patch.object(_mod, "_open_avatar_menu"):
            result = switch_to_burner_brand(page, work_dir, burner=burner)
        self.assertTrue(result)

    def test_switch_verification_failure_after_wait_exception(self):
        page = MagicMock()
        ctx = MagicMock()
        page.context = ctx
        burner = self._make_burner("UC11111111111111111111", "MyBurner")

        verify_pages = []
        for _ in range(3):
            vp = MagicMock()
            vp.url = "https://studio.youtube.com/channel/UC00000000000000000000"
            verify_pages.append(vp)
        ctx.new_page.side_effect = verify_pages

        switch = MagicMock()
        switch.is_visible.return_value = True
        target = MagicMock()
        target.count.return_value = 1
        target.is_visible.return_value = True
        page.wait_for_load_state.side_effect = Exception("network idle timeout")

        def locator_side(sel):
            lm = MagicMock()
            lm.first = switch if "Switch account" in sel else target
            return lm
        page.locator.side_effect = locator_side

        with patch("time.sleep"), patch.object(_mod, "_open_avatar_menu"):
            result = switch_to_burner_brand(page, FakePath("work"), burner=burner)
        self.assertFalse(result)

    def test_switch_uses_fallback_target_and_verifies(self):
        page = MagicMock()
        ctx = MagicMock()
        page.context = ctx
        burner = self._make_burner("UC11111111111111111111", "MyBurner")

        verify_p1 = MagicMock()
        verify_p1.url = "https://studio.youtube.com/channel/UC00000000000000000000"
        verify_p2 = MagicMock()
        verify_p2.url = "https://studio.youtube.com/channel/UC00000000000000000000"
        verify_p3 = MagicMock()
        verify_p3.url = "https://studio.youtube.com/channel/UC11111111111111111111"
        ctx.new_page.side_effect = [verify_p1, verify_p2, verify_p3]

        switch = MagicMock()
        switch.is_visible.return_value = True
        exact_target = MagicMock()
        exact_target.count.return_value = 0
        fallback_target = MagicMock()
        fallback_target.is_visible.return_value = True

        def locator_side(sel):
            lm = MagicMock()
            if "Switch account" in sel:
                lm.first = switch
            elif ":has(yt-formatted-string" in sel:
                lm.first = exact_target
            elif "ytd-account-item-renderer:has-text" in sel:
                lm.first = fallback_target
            else:
                lm.first = MagicMock()
            return lm
        page.locator.side_effect = locator_side

        with patch("time.sleep"), patch.object(_mod, "_open_avatar_menu"):
            result = switch_to_burner_brand(page, FakePath("work"), burner=burner)
        self.assertTrue(result)
        fallback_target.click.assert_called_once()


# ── engage_video ──────────────────────────────────────────────────────────────

class TestEngageVideo(unittest.TestCase):
    _PROBE_LIKE = f"{_P}._probe_like"
    _PROBE_SUB = f"{_P}._probe_subscribe"

    def _video(self):
        return {"video_id": "v1", "channel": "ch1", "title": "T1",
                "url": "https://youtube.com/watch?v=v1"}

    def _make_page(self, url="https://www.youtube.com/watch?v=v1", unavail=False):
        page = MagicMock()
        page.goto.return_value = None
        page.wait_for_selector.return_value = None
        page.url = url
        unavail_loc = MagicMock()
        unavail_loc.is_visible.return_value = unavail
        page.locator.return_value.first = unavail_loc
        return page

    def test_redirect_to_signin(self):
        page = self._make_page(url="https://accounts.google.com/signin")
        with patch("time.sleep"):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertTrue(any("sign-in" in e for e in r["errors"]))

    def test_video_unavailable(self):
        page = self._make_page(unavail=True)
        with patch("time.sleep"):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertEqual(r["like"], "unavailable")

    def test_already_liked_and_subscribed(self):
        page = self._make_page()
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, return_value=("liked", MagicMock())), \
             patch(self._PROBE_SUB, return_value=("subscribed", MagicMock())):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertEqual(r["like"], "already_liked")
        self.assertEqual(r["subscribe"], "already_subscribed")

    def test_like_ok_after_click(self):
        page = self._make_page()
        n = {"n": 0}
        def probe_like(p):
            n["n"] += 1
            return ("unliked", MagicMock()) if n["n"] == 1 else ("liked", MagicMock())
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, side_effect=probe_like), \
             patch(self._PROBE_SUB, return_value=("subscribed", MagicMock())):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertEqual(r["like"], "OK")

    def test_like_fail_after_click(self):
        page = self._make_page()
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, return_value=("unliked", MagicMock())), \
             patch(self._PROBE_SUB, return_value=("subscribed", MagicMock())):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertIn("FAIL", r["like"])

    def test_like_dry_run(self):
        page = self._make_page()
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, return_value=("unliked", MagicMock())), \
             patch(self._PROBE_SUB, return_value=("unsubscribed", MagicMock())):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=True, subscribed_channels=set())
        self.assertEqual(r["like"], "DRY_RUN_would_click")
        self.assertEqual(r["subscribe"], "DRY_RUN_would_click")

    def test_sub_ok_after_click(self):
        page = self._make_page()
        n = {"n": 0}
        def probe_sub(p):
            n["n"] += 1
            return ("unsubscribed", MagicMock()) if n["n"] == 1 else ("subscribed", MagicMock())
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, return_value=("liked", MagicMock())), \
             patch(self._PROBE_SUB, side_effect=probe_sub):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertEqual(r["subscribe"], "OK")

    def test_sub_fail_after_click(self):
        page = self._make_page()
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, return_value=("liked", MagicMock())), \
             patch(self._PROBE_SUB, return_value=("unsubscribed", MagicMock())):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertIn("FAIL", r["subscribe"])

    def test_sub_exception(self):
        page = self._make_page()
        sub_btn = MagicMock()
        sub_btn.scroll_into_view_if_needed.side_effect = Exception("scroll err")
        n = {"n": 0}
        def probe_sub(p):
            n["n"] += 1
            return ("unsubscribed", sub_btn)
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, return_value=("liked", MagicMock())), \
             patch(self._PROBE_SUB, side_effect=probe_sub):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertEqual(r["subscribe"], "FAIL_exception")

    def test_like_exception(self):
        page = self._make_page()
        like_btn = MagicMock()
        like_btn.dispatch_event.side_effect = Exception("click err")
        n = {"n": 0}
        def probe_like(p):
            n["n"] += 1
            return ("unliked", like_btn) if n["n"] <= 4 else ("unknown", None)
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, side_effect=probe_like), \
             patch(self._PROBE_SUB, return_value=("subscribed", MagicMock())):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertEqual(r["like"], "FAIL_exception")

    def test_like_no_button(self):
        page = self._make_page()
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, return_value=("unknown", None)), \
             patch(self._PROBE_SUB, return_value=("subscribed", MagicMock())):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertIn("FAIL_no_button", r["like"])

    def test_sub_no_button(self):
        page = self._make_page()
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, return_value=("liked", MagicMock())), \
             patch(self._PROBE_SUB, return_value=("unknown", None)):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertIn("FAIL_no_button", r["subscribe"])

    def test_channel_already_subscribed_skipped(self):
        page = self._make_page()
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, return_value=("liked", MagicMock())):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels={"ch1"})
        self.assertEqual(r["subscribe"], "skipped_channel_already_subscribed_this_run")

    def test_goto_raises(self):
        page = MagicMock()
        page.goto.side_effect = Exception("network error")
        with patch("time.sleep"):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertTrue(any("goto" in e for e in r["errors"]))

    def test_wait_for_selector_exception_swallowed(self):
        page = self._make_page()
        page.wait_for_selector.side_effect = Exception("timeout")
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, return_value=("liked", MagicMock())), \
             patch(self._PROBE_SUB, return_value=("subscribed", MagicMock())):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertEqual(r["like"], "already_liked")

    def test_unavailable_check_exception_swallowed(self):
        page = self._make_page()
        page.locator.return_value.first.is_visible.side_effect = Exception("err")
        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, return_value=("liked", MagicMock())), \
             patch(self._PROBE_SUB, return_value=("subscribed", MagicMock())):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertEqual(r["like"], "already_liked")

    def test_consent_button_click_exceptions_continue(self):
        page = self._make_page()
        unavailable = MagicMock()
        unavailable.is_visible.return_value = False
        consent_btn = MagicMock()
        consent_btn.click.side_effect = Exception("modal blocked")

        def locator_side(sel):
            lm = MagicMock()
            lm.first = unavailable if "Video unavailable" in sel else consent_btn
            return lm
        page.locator.side_effect = locator_side

        with patch("time.sleep"), \
             patch(self._PROBE_LIKE, return_value=("liked", MagicMock())), \
             patch(self._PROBE_SUB, return_value=("subscribed", MagicMock())):
            r = engage_video(page, video=self._video(), do_like=True, do_subscribe=True,
                             dry_run=False, subscribed_channels=set())
        self.assertEqual(r["subscribe"], "already_subscribed")


# ── subscribe_to_channel() ─────────────────────────────────────────────────────

class TestSubscribeToChannel(unittest.TestCase):
    """Coverage for the subscribe-only fast path helper added 2026-05-11."""

    _PROBE_SUB = f"{_P}._probe_subscribe"

    def _make_page(self, url="https://www.youtube.com/channel/UC_X", unavail=False):
        page = MagicMock()
        page.goto.return_value = None
        page.wait_for_selector.return_value = None
        page.url = url
        unavail_loc = MagicMock()
        unavail_loc.is_visible.return_value = unavail
        page.locator.return_value.first = unavail_loc
        return page

    def _call(self, page, *, dry_run=False):
        from pipeline.cross_engage.cross_engage_burner_attached import subscribe_to_channel
        return subscribe_to_channel(
            page,
            channel_id="UC_X",
            channel_slug="ch_x",
            channel_label="Channel X",
            dry_run=dry_run,
        )

    def test_already_subscribed(self):
        page = self._make_page()
        with patch("time.sleep"), \
             patch(self._PROBE_SUB, return_value=("subscribed", MagicMock())):
            r = self._call(page)
        self.assertEqual(r["subscribe"], "already_subscribed")
        self.assertEqual(r["channel_id"], "UC_X")
        self.assertEqual(r["channel"], "ch_x")

    def test_dry_run(self):
        page = self._make_page()
        with patch("time.sleep"), \
             patch(self._PROBE_SUB, return_value=("unsubscribed", MagicMock())):
            r = self._call(page, dry_run=True)
        self.assertEqual(r["subscribe"], "DRY_RUN_would_click")

    def test_click_ok(self):
        page = self._make_page()
        n = {"n": 0}
        def probe(p):
            n["n"] += 1
            return ("unsubscribed", MagicMock()) if n["n"] == 1 else ("subscribed", MagicMock())
        with patch("time.sleep"), patch(self._PROBE_SUB, side_effect=probe):
            r = self._call(page)
        self.assertEqual(r["subscribe"], "OK")

    def test_click_state_unchanged(self):
        page = self._make_page()
        with patch("time.sleep"), \
             patch(self._PROBE_SUB, return_value=("unsubscribed", MagicMock())):
            r = self._call(page)
        self.assertIn("FAIL_state_after_click", r["subscribe"])

    def test_click_exception(self):
        page = self._make_page()
        btn = MagicMock()
        btn.scroll_into_view_if_needed.side_effect = Exception("scroll bad")
        with patch("time.sleep"), \
             patch(self._PROBE_SUB, return_value=("unsubscribed", btn)):
            r = self._call(page)
        self.assertEqual(r["subscribe"], "FAIL_exception")
        self.assertTrue(any("scroll bad" in e for e in r["errors"]))

    def test_no_button_after_retries(self):
        page = self._make_page()
        with patch("time.sleep"), \
             patch(self._PROBE_SUB, return_value=("unknown", None)):
            r = self._call(page)
        self.assertIn("FAIL_no_button", r["subscribe"])

    def test_redirect_to_signin(self):
        page = self._make_page(url="https://accounts.google.com/signin")
        with patch("time.sleep"):
            r = self._call(page)
        self.assertTrue(any("sign-in" in e for e in r["errors"]))

    def test_goto_fails(self):
        page = self._make_page()
        page.goto.side_effect = Exception("net err")
        with patch("time.sleep"):
            r = self._call(page)
        self.assertTrue(any("goto" in e for e in r["errors"]))

    def test_wait_for_selector_failure_swallowed(self):
        """Hydration timeout is non-fatal — we still try to probe."""
        page = self._make_page()
        page.wait_for_selector.side_effect = Exception("timeout")
        with patch("time.sleep"), \
             patch(self._PROBE_SUB, return_value=("subscribed", MagicMock())):
            r = self._call(page)
        self.assertEqual(r["subscribe"], "already_subscribed")


# ── run() ──────────────────────────────────────────────────────────────────────

class TestRun(unittest.TestCase):
    def _make_args(self, **kwargs):
        args = argparse.Namespace(
            slug="b1", email="host@e.com", profile=None,
            work_dir=FakePath("work"),
            user_data_dir=_mod.DEFAULT_USER_DATA_DIR,
            limit=0, include_self=False,
            no_like=False, no_subscribe=False,
            force_fresh=False, dry_run=False,
        )
        for k, v in kwargs.items():
            setattr(args, k, v)
        return args

    def _catalog(self, n=2):
        entries = []
        for i in range(n):
            e = MagicMock()
            e.video_id = f"v{i}"
            e.channel = f"ch{i}"
            e.channel_label = f"ch{i}"
            e.title = f"Title {i}"
            e.url = f"https://youtube.com/watch?v=v{i}"
            entries.append(e)
        return entries

    def _run(self, args, burner=None, *, switched=True,
             canonical=None, force_fresh_flag=False,
             harvest_side_effect=None):
        if burner is None:
            burner = {"slug": "b1", "channel_id": "UC_B",
                      "title": "Burner", "email": "host@e.com"}
        catalog = self._catalog()
        mock_sp, pw, browser, ctx, page = make_fake_playwright("https://www.youtube.com/")
        ctx.new_page.return_value = page

        proc = MagicMock()
        proc.pid = 9999

        engage_results = [{"like": "OK", "subscribe": "OK", "errors": []}
                          for _ in catalog]

        from pipeline.cross_engage.cross_engage_burner_attached import run as _run_fn

        harvest_se = harvest_side_effect
        if harvest_se is None:
            harvest_se = [{"name": "SID"}]  # returned from harvest_cookies_via_cdp

        with patch.object(_mod, "resolve_burner", return_value=burner), \
             patch(f"{_P}.resolve_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch(f"{_P}.find_running_chrome_debug",
                   side_effect=[None, canonical or None, None]), \
             patch(f"{_P}.clone_chrome_debug_to_engage_udd"), \
             patch(f"{_P}.harvest_cookies_via_cdp",
                   side_effect=[harvest_se] if isinstance(harvest_se, Exception) else None,
                   return_value=harvest_se if not isinstance(harvest_se, Exception) else None), \
             patch(f"{_P}.bridge_cookies"), \
             patch(f"{_P}._clear_singleton"), \
             patch(f"{_P}.launch_chrome_for", return_value=(proc, 12345)), \
             patch(f"{_P}.switch_to_burner_brand", return_value=switched), \
             patch(f"{_P}.engage_video", side_effect=engage_results), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0):
            return _run_fn(args)

    def test_run_success_no_canonical(self):
        """Fresh Chrome, no canonical running → bridge_cookies path."""
        args = self._make_args()
        summary = self._run(args, canonical=None)
        self.assertTrue(summary["switched_to_burner"])
        self.assertEqual(summary["n_catalog"], 2)

    def test_run_with_canonical_running(self):
        """Canonical Chrome running → clone + harvest + inject cookies."""
        args = self._make_args()
        summary = self._run(args, canonical=(42, 9999))
        self.assertTrue(summary["switched_to_burner"])

    def test_run_cookie_injection_close_exception_swallowed(self):
        args = self._make_args()
        burner = {"slug": "b1", "channel_id": "UC_B",
                  "title": "Burner", "email": "host@e.com"}
        catalog = self._catalog()
        mock_sp, pw, browser, ctx, page = make_fake_playwright("https://www.youtube.com/")
        ctx.new_page.return_value = page
        browser.close.side_effect = Exception("close failed")
        proc = MagicMock()
        proc.pid = 9999
        engage_results = [{"like": "OK", "subscribe": "OK", "errors": []}
                          for _ in catalog]

        from pipeline.cross_engage.cross_engage_burner_attached import run as _run_fn
        with patch.object(_mod, "resolve_burner", return_value=burner), \
             patch(f"{_P}.resolve_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch(f"{_P}.find_running_chrome_debug",
                   side_effect=[None, (42, 9999), None]), \
             patch(f"{_P}.clone_chrome_debug_to_engage_udd"), \
             patch(f"{_P}.harvest_cookies_via_cdp", return_value=[{"name": "SID"}]), \
             patch(f"{_P}.bridge_cookies"), \
             patch(f"{_P}._clear_singleton"), \
             patch(f"{_P}.launch_chrome_for", return_value=(proc, 12345)), \
             patch(f"{_P}.switch_to_burner_brand", return_value=True), \
             patch(f"{_P}.engage_video", side_effect=engage_results), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0):
            summary = _run_fn(args)
        self.assertTrue(summary["switched_to_burner"])

    def test_run_harvest_exception_swallowed(self):
        """Harvest raises → swallowed, continue without injection."""
        args = self._make_args()
        summary = self._run(args, canonical=(42, 9999),
                            harvest_side_effect=Exception("CDP fail"))
        self.assertTrue(summary["switched_to_burner"])

    def test_run_switch_fails_raises(self):
        args = self._make_args()
        with self.assertRaises(SystemExit):
            self._run(args, switched=False)

    def test_run_limit(self):
        args = self._make_args(limit=1)
        summary = self._run(args)
        self.assertEqual(summary["n_catalog"], 1)

    def test_run_empty_catalog_raises(self):
        from pipeline.cross_engage.cross_engage_burner_attached import run as _run_fn
        args = self._make_args()
        burner = {"slug": "b1", "channel_id": "UC_B", "title": "B", "email": "host@e.com"}
        with patch.object(_mod, "resolve_burner", return_value=burner), \
             patch(f"{_P}.resolve_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=[]):
            with self.assertRaises(SystemExit):
                _run_fn(args)

    def test_run_existing_engaged_chrome(self):
        """Existing engage-Chrome running → attach path (no bridge/clone/launch)."""
        args = self._make_args()
        burner = {"slug": "b1", "channel_id": "UC_B", "title": "Burner", "email": "host@e.com"}
        catalog = self._catalog()
        mock_sp, pw, browser, ctx, page = make_fake_playwright("https://www.youtube.com/")
        ctx.new_page.return_value = page
        engage_results = [{"like": "OK", "subscribe": "OK", "errors": []} for _ in catalog]

        from pipeline.cross_engage.cross_engage_burner_attached import run as _run_fn
        with patch.object(_mod, "resolve_burner", return_value=burner), \
             patch(f"{_P}.resolve_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch(f"{_P}.find_running_chrome_debug", return_value=(99, 12345)), \
             patch(f"{_P}.switch_to_burner_brand", return_value=True), \
             patch(f"{_P}.engage_video", side_effect=engage_results), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0):
            summary = _run_fn(args)
        self.assertTrue(summary["switched_to_burner"])

    def test_run_include_self(self):
        args = self._make_args(include_self=True)
        # burner title matches catalog channel_label for item 0
        burner = {"slug": "b1", "channel_id": "UC_B", "title": "ch0", "email": "host@e.com"}
        # With include_self=True, filter is bypassed → both items in catalog
        summary = self._run(args, burner=burner)
        self.assertEqual(summary["n_catalog"], 2)

    def test_run_custom_user_data_dir(self):
        args = self._make_args(user_data_dir=FakePath("custom-udd"))
        summary = self._run(args)
        self.assertIsNotNone(summary)

    def test_run_vpage_close_exception_swallowed(self):
        """Exception in vpage.close() is swallowed by the finally block."""
        args = self._make_args(limit=1)
        burner = {"slug": "b1", "channel_id": "UC_B", "title": "Burner", "email": "host@e.com"}
        catalog = self._catalog(1)
        mock_sp, pw, browser, ctx, page = make_fake_playwright("https://www.youtube.com/")
        vpage = MagicMock()
        vpage.set_default_timeout.return_value = None
        vpage.close.side_effect = Exception("close err")
        ctx.new_page.return_value = vpage
        engage_results = [{"like": "OK", "subscribe": "OK", "errors": []}]

        from pipeline.cross_engage.cross_engage_burner_attached import run as _run_fn
        with patch.object(_mod, "resolve_burner", return_value=burner), \
             patch(f"{_P}.resolve_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch(f"{_P}.find_running_chrome_debug", return_value=None), \
             patch(f"{_P}.bridge_cookies"), \
             patch(f"{_P}._clear_singleton"), \
             patch(f"{_P}.launch_chrome_for", return_value=(MagicMock(pid=9), 12345)), \
             patch(f"{_P}.switch_to_burner_brand", return_value=True), \
             patch(f"{_P}.engage_video", side_effect=engage_results), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0):
            summary = _run_fn(args)
        # close exception swallowed; results populated
        self.assertEqual(len(summary["results"]), 1)

    def test_run_subscribe_only_fast_path(self):
        """--no-like activates the fast path: subscribe_to_channel called per
        unique source channel (not engage_video per video)."""
        args = self._make_args(no_like=True, no_subscribe=False)
        burner = {"slug": "b1", "channel_id": "UC_B",
                  "title": "Burner", "email": "host@e.com"}
        # 4 catalog videos, only 2 unique source channels (ch0, ch1).
        catalog = []
        for i, ch in enumerate(["ch0", "ch1", "ch0", "ch1"]):
            e = MagicMock()
            e.video_id = f"v{i}"
            e.channel = ch
            e.channel_label = f"{ch}_label"
            e.title = f"Title {i}"
            e.url = f"https://youtube.com/watch?v=v{i}"
            catalog.append(e)
        mock_sp, pw, browser, ctx, page = make_fake_playwright("https://www.youtube.com/")
        ctx.new_page.return_value = page
        # Both unique channels resolve in channel_ids.json.
        ch_ids = {
            "ch0": {"channel_id": "UC0", "title": "Channel Zero"},
            "ch1": {"channel_id": "UC1", "title": "Channel One"},
        }
        sub_results = [
            {"channel_id": "UC0", "channel": "ch0", "channel_label": "ch0_label",
             "subscribe": "OK", "errors": []},
            {"channel_id": "UC1", "channel": "ch1", "channel_label": "ch1_label",
             "subscribe": "already_subscribed", "errors": []},
        ]

        from pipeline.cross_engage.cross_engage_burner_attached import run as _run_fn
        engage_video_mock = MagicMock()
        sub_to_ch_mock = MagicMock(side_effect=sub_results)
        with patch.object(_mod, "resolve_burner", return_value=burner), \
             patch(f"{_P}.resolve_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch(f"{_P}.find_running_chrome_debug", return_value=None), \
             patch(f"{_P}.bridge_cookies"), \
             patch(f"{_P}._clear_singleton"), \
             patch(f"{_P}.launch_chrome_for",
                   return_value=(MagicMock(pid=9), 12345)), \
             patch(f"{_P}.switch_to_burner_brand", return_value=True), \
             patch(f"{_P}.engage_video", engage_video_mock), \
             patch(f"{_P}.subscribe_to_channel", sub_to_ch_mock), \
             patch(f"{_P}._load_json", return_value=ch_ids), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0):
            summary = _run_fn(args)

        # Fast path: engage_video must NOT be called even once.
        engage_video_mock.assert_not_called()
        # Exactly one subscribe call per unique source channel.
        self.assertEqual(sub_to_ch_mock.call_count, 2)
        called_ids = [c.kwargs["channel_id"] for c in sub_to_ch_mock.call_args_list]
        self.assertEqual(sorted(called_ids), ["UC0", "UC1"])
        # Summary surfaces the dedup count.
        self.assertEqual(summary["unique_channels_total"], 2)
        self.assertEqual(summary["unique_channels_missing_id"], 0)
        self.assertEqual(len(summary["results"]), 2)

    def test_run_subscribe_only_skips_channels_without_id(self):
        """Channels missing from channel_ids.json are warned + skipped, not crashed."""
        args = self._make_args(no_like=True, no_subscribe=False)
        burner = {"slug": "b1", "channel_id": "UC_B",
                  "title": "Burner", "email": "host@e.com"}
        catalog = []
        for ch in ["known", "unknown"]:
            e = MagicMock()
            e.video_id = f"v_{ch}"
            e.channel = ch
            e.channel_label = ch
            e.title = ch
            e.url = "https://youtube.com/"
            catalog.append(e)
        mock_sp, pw, browser, ctx, page = make_fake_playwright("https://www.youtube.com/")
        ctx.new_page.return_value = page
        ch_ids = {"known": {"channel_id": "UC_KNOWN", "title": "Known"}}

        from pipeline.cross_engage.cross_engage_burner_attached import run as _run_fn
        sub_to_ch_mock = MagicMock(return_value={
            "channel_id": "UC_KNOWN", "channel": "known", "channel_label": "known",
            "subscribe": "OK", "errors": [],
        })
        with patch.object(_mod, "resolve_burner", return_value=burner), \
             patch(f"{_P}.resolve_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch(f"{_P}.find_running_chrome_debug", return_value=None), \
             patch(f"{_P}.bridge_cookies"), \
             patch(f"{_P}._clear_singleton"), \
             patch(f"{_P}.launch_chrome_for",
                   return_value=(MagicMock(pid=9), 12345)), \
             patch(f"{_P}.switch_to_burner_brand", return_value=True), \
             patch(f"{_P}.subscribe_to_channel", sub_to_ch_mock), \
             patch(f"{_P}._load_json", return_value=ch_ids), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0):
            summary = _run_fn(args)

        self.assertEqual(sub_to_ch_mock.call_count, 1)
        self.assertEqual(summary["unique_channels_total"], 1)
        self.assertEqual(summary["unique_channels_missing_id"], 1)

    def test_run_no_like_with_no_subscribe_falls_back_to_video_path(self):
        """--no-like AND --no-subscribe → fast path NOT taken (nothing to do
        either way; default per-video loop runs and produces 'skipped' rows)."""
        args = self._make_args(no_like=True, no_subscribe=True)
        burner = {"slug": "b1", "channel_id": "UC_B",
                  "title": "Burner", "email": "host@e.com"}
        catalog = self._catalog()
        mock_sp, pw, browser, ctx, page = make_fake_playwright("https://www.youtube.com/")
        ctx.new_page.return_value = page
        engage_video_mock = MagicMock(return_value={
            "like": "skipped", "subscribe": "skipped", "errors": [],
        })
        sub_to_ch_mock = MagicMock()

        from pipeline.cross_engage.cross_engage_burner_attached import run as _run_fn
        with patch.object(_mod, "resolve_burner", return_value=burner), \
             patch(f"{_P}.resolve_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch(f"{_P}.find_running_chrome_debug", return_value=None), \
             patch(f"{_P}.bridge_cookies"), \
             patch(f"{_P}._clear_singleton"), \
             patch(f"{_P}.launch_chrome_for",
                   return_value=(MagicMock(pid=9), 12345)), \
             patch(f"{_P}.switch_to_burner_brand", return_value=True), \
             patch(f"{_P}.engage_video", engage_video_mock), \
             patch(f"{_P}.subscribe_to_channel", sub_to_ch_mock), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0):
            _run_fn(args)
        # Per-video path used; fast path NOT used.
        sub_to_ch_mock.assert_not_called()
        self.assertEqual(engage_video_mock.call_count, len(catalog))


# ── main() ─────────────────────────────────────────────────────────────────────

class TestMain(unittest.TestCase):
    def test_mutually_exclusive_raises(self):
        from pipeline.cross_engage.cross_engage_burner_attached import main
        with patch("sys.argv", ["prog", "--all-burners", "--slug", "b1"]):
            with self.assertRaises(SystemExit):
                main()

    def test_single_burner_run_success(self):
        from pipeline.cross_engage.cross_engage_burner_attached import main
        with patch("sys.argv", ["prog", "--slug", "b1"]), \
             patch(f"{_P}.run",
                   return_value={"switched_to_burner": True, "slug": "b1"}):
            rc = main()
        self.assertEqual(rc, 0)

    def test_single_burner_not_switched_nonzero(self):
        from pipeline.cross_engage.cross_engage_burner_attached import main
        with patch("sys.argv", ["prog", "--slug", "b1"]), \
             patch(f"{_P}.run", return_value={"switched_to_burner": False}):
            rc = main()
        self.assertEqual(rc, 1)

    def test_all_burners_success(self):
        from pipeline.cross_engage.cross_engage_burner_attached import main
        burners = [{"slug": "b1"}, {"slug": "b2"}]
        with patch("sys.argv", ["prog", "--all-burners"]), \
             patch.object(_mod, "list_known_burners", return_value=burners), \
             patch(f"{_P}.run",
                   return_value={"switched_to_burner": True, "slug": "bX"}), \
             patch("pathlib.Path.write_text"):
            rc = main()
        self.assertEqual(rc, 0)

    def test_all_burners_no_burners(self):
        from pipeline.cross_engage.cross_engage_burner_attached import main
        with patch("sys.argv", ["prog", "--all-burners"]), \
             patch.object(_mod, "list_known_burners", return_value=[]):
            with self.assertRaises(SystemExit):
                main()

    def test_all_burners_one_fails_exception(self):
        from pipeline.cross_engage.cross_engage_burner_attached import main
        burners = [{"slug": "b1"}, {"slug": "b2"}]
        with patch("sys.argv", ["prog", "--all-burners"]), \
             patch.object(_mod, "list_known_burners", return_value=burners), \
             patch(f"{_P}.run", side_effect=[Exception("b1 fail"),
                                              {"switched_to_burner": True}]), \
             patch("pathlib.Path.write_text"):
            rc = main()
        self.assertEqual(rc, 1)

    def test_all_burners_switch_failed(self):
        from pipeline.cross_engage.cross_engage_burner_attached import main
        burners = [{"slug": "b1"}]
        with patch("sys.argv", ["prog", "--all-burners"]), \
             patch.object(_mod, "list_known_burners", return_value=burners), \
             patch(f"{_P}.run", return_value={"switched_to_burner": False}), \
             patch("pathlib.Path.write_text"):
            rc = main()
        self.assertEqual(rc, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
