"""Tests for pipeline.cross_engage.cross_engage_via_playwright — 100% line coverage."""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests._helpers import FakePath, make_fake_playwright

import pipeline.cross_engage.cross_engage_via_playwright as _mod
from pipeline.cross_engage.cross_engage_via_playwright import (
    COOKIE_FILES,
    LIKE_SELECTORS,
    NETWORK_COOKIE_FILES,
    SUBSCRIBE_SELECTORS,
    _clear_singleton,
    _probe_like,
    _probe_subscribe,
    assert_chrome_closed,
    bridge_cookies,
    discover_profiles,
    profile_email_map,
)


# ── helpers ─────────────────────────────────────────────────────────────────

def _profile_dir(name: str, is_dir: bool = True) -> FakePath:
    p = FakePath(name, exists=is_dir)
    return p


def _make_popen_mock(cdp_port: str = "12345") -> MagicMock:
    mock_proc = MagicMock()
    mock_proc.pid = 999
    mock_proc.terminate.return_value = None
    mock_proc.wait.return_value = 0
    mock_proc.kill.return_value = None
    return mock_proc, cdp_port


# ── discover_profiles ───────────────────────────────────────────────────────

class TestDiscoverProfiles(unittest.TestCase):
    def _make_base(self, names: list[str]) -> FakePath:
        base = FakePath("base", exists=True)
        for n in names:
            child = FakePath(n, exists=True, is_dir=True)
            base._children[n] = child
        return base

    def test_real_only_true_filters_profile_dirs(self):
        base = self._make_base(["Profile 1", "Default", "Profile 10", "System Profile"])
        with patch.object(_mod, "REAL_CHROME", base):
            result = discover_profiles(real_only=True)
        self.assertEqual(result, ["Profile 1", "Profile 10"])

    def test_real_only_false_uses_chrome_debug(self):
        base = self._make_base(["Profile 2", "Foo"])
        with patch.object(_mod, "CHROME_DEBUG", base):
            result = discover_profiles(real_only=False)
        self.assertEqual(result, ["Profile 2"])

    def test_non_dir_skipped(self):
        base = FakePath("base", exists=True)
        child = FakePath("Profile 1", exists=False)
        base._children["Profile 1"] = child
        with patch.object(_mod, "CHROME_DEBUG", base):
            result = discover_profiles()
        self.assertEqual(result, [])


# ── profile_email_map ───────────────────────────────────────────────────────

class TestProfileEmailMap(unittest.TestCase):
    def test_returns_mapping(self):
        local_state = {
            "profile": {
                "info_cache": {
                    "Profile 1": {"user_name": "user@example.com"},
                    "Profile 2": {"user_name": ""},
                    "Profile 3": {},
                }
            }
        }
        fake_ls = FakePath("Local State", exists=True)
        fake_ls._text_data = json.dumps(local_state)
        fake_base = FakePath("Chrome", exists=True)
        fake_base._children["Local State"] = fake_ls
        with patch.object(_mod, "REAL_CHROME", fake_base):
            result = profile_email_map()
        self.assertEqual(result, {"Profile 1": "user@example.com"})


# ── bridge_cookies ──────────────────────────────────────────────────────────

class TestBridgeCookies(unittest.TestCase):
    def _setup(self, src_exists: bool = True, files_exist: bool = True):
        src = FakePath("Chrome", exists=True)
        dst = FakePath("Chrome-Debug", exists=True)
        # Profile dir
        src_p = FakePath("Profile 1", exists=src_exists)
        src._children["Profile 1"] = src_p
        # Local State at top-level
        ls = FakePath("Local State", exists=True)
        ls._bytes_data = b"state"
        src._children["Local State"] = ls
        # Cookie files inside profile
        for f in list(COOKIE_FILES) + ["Network"]:
            child = FakePath(f, exists=files_exist)
            child._bytes_data = b"data"
            src_p._children[f] = child
        # Network subfolder cookie files
        net = FakePath("Network", exists=True)
        for f in NETWORK_COOKIE_FILES:
            net._children[f] = FakePath(f, exists=files_exist)
            net._children[f]._bytes_data = b"cookie"
        src_p._children["Network"] = net
        return src, dst

    def test_success_all_files_copied(self):
        src, dst = self._setup(src_exists=True, files_exist=True)
        with patch.object(_mod, "REAL_CHROME", src), \
             patch.object(_mod, "CHROME_DEBUG", dst):
            bridge_cookies("Profile 1")
        # No exception = success

    def test_src_not_found_raises(self):
        src, dst = self._setup(src_exists=False)
        with patch.object(_mod, "REAL_CHROME", src), \
             patch.object(_mod, "CHROME_DEBUG", dst):
            with self.assertRaises(FileNotFoundError):
                bridge_cookies("Profile 1")

    def test_no_cookie_files_skipped_when_missing(self):
        src, dst = self._setup(src_exists=True, files_exist=False)
        # Local State also missing
        src._children["Local State"]._exists = False
        with patch.object(_mod, "REAL_CHROME", src), \
             patch.object(_mod, "CHROME_DEBUG", dst):
            bridge_cookies("Profile 1")  # no exception

    def test_custom_dst_dir_used(self):
        src, _ = self._setup(src_exists=True, files_exist=True)
        custom_dst = FakePath("custom-dst", exists=True)
        with patch.object(_mod, "REAL_CHROME", src):
            bridge_cookies("Profile 1", dst_dir=custom_dst)
        # dst_dir was used instead of CHROME_DEBUG


# ── assert_chrome_closed ────────────────────────────────────────────────────

class TestAssertChromeClosed(unittest.TestCase):
    def test_no_chrome_passes(self):
        mock_result = MagicMock()
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            assert_chrome_closed()  # should not raise

    def test_chrome_running_raises(self):
        mock_result = MagicMock()
        mock_result.stdout = "12345\n67890\n"
        with patch("subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError):
                assert_chrome_closed()

    def test_whitespace_only_stdout_passes(self):
        mock_result = MagicMock()
        mock_result.stdout = "   \n  \n"
        with patch("subprocess.run", return_value=mock_result):
            assert_chrome_closed()


# ── _clear_singleton ────────────────────────────────────────────────────────

class TestClearSingleton(unittest.TestCase):
    def test_default_dst(self):
        base = FakePath("Chrome-Debug", exists=True)
        with patch.object(_mod, "CHROME_DEBUG", base):
            _clear_singleton()
        # No exception

    def test_custom_dst(self):
        custom = FakePath("custom", exists=True)
        _clear_singleton(dst_dir=custom)
        # No exception


# ── launch_chrome_for ───────────────────────────────────────────────────────

class TestLaunchChromeFor(unittest.TestCase):
    def test_success_parses_cdp_port(self):
        from pipeline.cross_engage.cross_engage_via_playwright import launch_chrome_for

        work_dir = FakePath("work", exists=True)
        stderr_file = FakePath("chrome.stderr", exists=True)
        stderr_file._text_data = "DevTools listening on ws://127.0.0.1:55123/..."
        stderr_file.write_text = lambda data, **kw: None  # prevent clear by launch_chrome_for
        work_dir._children["chrome.stderr"] = stderr_file

        mock_proc = MagicMock()
        mock_proc.kill.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("time.sleep"), \
             patch("builtins.open", return_value=MagicMock()):
            proc, port = launch_chrome_for("Profile 1", work_dir=work_dir)

        self.assertEqual(port, "55123")
        self.assertIs(proc, mock_proc)
        mock_popen.assert_called_once()

    def test_no_cdp_port_raises(self):
        from pipeline.cross_engage.cross_engage_via_playwright import launch_chrome_for

        work_dir = FakePath("work", exists=True)
        stderr_file = FakePath("chrome.stderr", exists=True)
        stderr_file._text_data = "no port info here"
        stderr_file.write_text = lambda data, **kw: None  # prevent clear by launch_chrome_for
        work_dir._children["chrome.stderr"] = stderr_file

        mock_proc = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("time.sleep"), \
             patch("builtins.open", return_value=MagicMock()):
            with self.assertRaises(RuntimeError):
                launch_chrome_for("Profile 1", work_dir=work_dir)
        mock_proc.kill.assert_called_once()

    def test_custom_user_data_dir(self):
        from pipeline.cross_engage.cross_engage_via_playwright import launch_chrome_for

        work_dir = FakePath("work", exists=True)
        stderr_file = FakePath("chrome.stderr", exists=True)
        stderr_file._text_data = "ws://127.0.0.1:44444/"
        stderr_file.write_text = lambda data, **kw: None  # prevent clear by launch_chrome_for
        work_dir._children["chrome.stderr"] = stderr_file

        custom_udd = FakePath("custom-udd", exists=True)
        mock_proc = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("time.sleep"), \
             patch("builtins.open", return_value=MagicMock()):
            proc, port = launch_chrome_for(
                "Profile 1", work_dir=work_dir, user_data_dir=custom_udd
            )

        cmd = mock_popen.call_args[0][0]
        self.assertTrue(any("custom-udd" in str(arg) for arg in cmd))
        self.assertEqual(port, "44444")


# ── _probe_like ─────────────────────────────────────────────────────────────

class TestProbeLike(unittest.TestCase):
    def _page_with(self, aria_pressed):
        page = MagicMock()
        btn = MagicMock()
        btn.get_attribute.return_value = aria_pressed
        page.locator.return_value.first = btn
        return page, btn

    def test_already_liked(self):
        page, btn = self._page_with("true")
        state, result_btn = _probe_like(page)
        self.assertEqual(state, "liked")
        self.assertIs(result_btn, btn)

    def test_unliked(self):
        page, btn = self._page_with("false")
        state, result_btn = _probe_like(page)
        self.assertEqual(state, "unliked")
        self.assertIs(result_btn, btn)

    def test_unknown_when_none(self):
        page, _ = self._page_with(None)
        state, btn = _probe_like(page)
        self.assertEqual(state, "unknown")
        self.assertIsNone(btn)

    def test_exception_on_all_selectors_returns_unknown(self):
        page = MagicMock()
        page.locator.return_value.first.get_attribute.side_effect = Exception("timeout")
        state, btn = _probe_like(page)
        self.assertEqual(state, "unknown")
        self.assertIsNone(btn)


# ── _probe_subscribe ────────────────────────────────────────────────────────

class TestProbeSubscribe(unittest.TestCase):
    def _page_with(self, txt: str, label: str = ""):
        page = MagicMock()
        btn = MagicMock()
        btn.text_content.return_value = txt
        btn.get_attribute.return_value = label
        page.locator.return_value.first = btn
        return page, btn

    def test_subscribed_by_text(self):
        page, btn = self._page_with("Subscribed")
        state, result = _probe_subscribe(page)
        self.assertEqual(state, "subscribed")

    def test_subscribed_by_unsubscribe_label(self):
        page, btn = self._page_with("", "unsubscribe from this channel")
        state, _ = _probe_subscribe(page)
        self.assertEqual(state, "subscribed")

    def test_unsubscribed(self):
        page, btn = self._page_with("Subscribe")
        state, result = _probe_subscribe(page)
        self.assertEqual(state, "unsubscribed")
        self.assertIs(result, btn)

    def test_unknown(self):
        page, _ = self._page_with("Watch later")
        state, btn = _probe_subscribe(page)
        self.assertEqual(state, "unknown")
        self.assertIsNone(btn)

    def test_exception_on_all_returns_unknown(self):
        page = MagicMock()
        page.locator.return_value.first.text_content.side_effect = Exception("err")
        state, btn = _probe_subscribe(page)
        self.assertEqual(state, "unknown")
        self.assertIsNone(btn)


# ── engage_from_profile ─────────────────────────────────────────────────────

class TestEngageFromProfile(unittest.TestCase):
    def _run(self, page_url, probe_like=("unliked", True), probe_sub=("unsubscribed", True),
             after_like="liked", after_sub="subscribed",
             do_like=True, do_subscribe=True):
        """Helper: run engage_from_profile with mocked Chrome + Playwright."""
        from pipeline.cross_engage.cross_engage_via_playwright import engage_from_profile

        mock_sp, pw, browser, ctx, page = make_fake_playwright(page_url)
        page.url = page_url

        # Screenshot
        page.screenshot.return_value = None

        # Probe selectors for like
        like_btn = MagicMock()
        like_state, like_btn_present = probe_like
        like_btn.get_attribute.return_value = (
            "false" if like_state == "unliked" else
            "true" if like_state == "liked" else None
        )
        page.locator.return_value.first = like_btn

        # After-click probe
        call_count = {"like": 0, "sub": 0}

        def _probe_like_impl(p):
            call_count["like"] += 1
            if call_count["like"] == 1:
                return like_state, like_btn if like_btn_present else (like_state, None)
            return after_like, MagicMock()

        def _probe_sub_impl(p):
            call_count["sub"] += 1
            sub_state, sub_btn_present = probe_sub
            if call_count["sub"] == 1:
                sub_btn = MagicMock()
                return sub_state, sub_btn if sub_btn_present else None
            return after_sub, MagicMock()

        work_dir = FakePath("work", exists=True)
        screen_file = FakePath("Profile_1-01.png", exists=True)
        work_dir._children["Profile_1-01.png"] = screen_file

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        with patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_like",
                   side_effect=_probe_like_impl), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
                   side_effect=_probe_sub_impl), \
             patch("time.sleep"):
            result = engage_from_profile(
                "Profile 1", "https://youtube.com/watch?v=abc",
                work_dir=work_dir, do_like=do_like, do_subscribe=do_subscribe,
            )
        return result

    def test_already_liked_and_subscribed(self):
        r = self._run(
            "https://www.youtube.com/watch?v=abc",
            probe_like=("liked", True),
            probe_sub=("subscribed", True),
        )
        self.assertEqual(r["like"], "already_liked")
        self.assertEqual(r["subscribe"], "already_subscribed")

    def test_like_ok_subscribe_ok(self):
        r = self._run(
            "https://www.youtube.com/watch?v=abc",
            probe_like=("unliked", True),
            probe_sub=("unsubscribed", True),
            after_like="liked",
            after_sub="subscribed",
        )
        self.assertEqual(r["like"], "OK")
        self.assertEqual(r["subscribe"], "OK")

    def test_like_fail_after_click(self):
        r = self._run(
            "https://www.youtube.com/watch?v=abc",
            probe_like=("unliked", True),
            after_like="unknown",
        )
        self.assertIn("FAIL_state_after_click", r["like"])

    def test_like_no_button(self):
        r = self._run(
            "https://www.youtube.com/watch?v=abc",
            probe_like=("unknown", False),
        )
        self.assertEqual(r["like"], "FAIL_no_button")

    def test_sub_no_button(self):
        r = self._run(
            "https://www.youtube.com/watch?v=abc",
            probe_like=("liked", True),
            probe_sub=("unknown", False),
        )
        self.assertEqual(r["subscribe"], "FAIL_no_button")

    def test_sub_fail_after_click(self):
        r = self._run(
            "https://www.youtube.com/watch?v=abc",
            probe_like=("liked", True),
            probe_sub=("unsubscribed", True),
            after_sub="unknown",
        )
        self.assertIn("FAIL_state_after_click", r["subscribe"])

    def test_redirected_to_signin(self):
        from pipeline.cross_engage.cross_engage_via_playwright import engage_from_profile

        mock_sp, pw, browser, ctx, page = make_fake_playwright()
        page.url = "https://accounts.google.com/signin/v2/identifier"

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        work_dir = FakePath("work", exists=True)

        with patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("time.sleep"):
            result = engage_from_profile(
                "Profile 1", "https://youtube.com/watch?v=abc", work_dir=work_dir,
            )
        self.assertTrue(any("sign-in" in e for e in result["errors"]))

    def test_consent_modal_click_exceptions_continue(self):
        from pipeline.cross_engage.cross_engage_via_playwright import engage_from_profile

        mock_sp, pw, browser, ctx, page = make_fake_playwright(
            "https://www.youtube.com/watch?v=abc"
        )
        consent_btn = MagicMock()
        consent_btn.click.side_effect = Exception("modal blocked")
        page.locator.return_value.first = consent_btn

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        work_dir = FakePath("work", exists=True)

        with patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_like",
                   return_value=("liked", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
                   return_value=("subscribed", MagicMock())), \
             patch("time.sleep"):
            result = engage_from_profile(
                "Profile 1", "https://youtube.com/watch?v=abc", work_dir=work_dir,
            )
        self.assertEqual(result["like"], "already_liked")

    def test_like_click_raises_exception(self):
        from pipeline.cross_engage.cross_engage_via_playwright import engage_from_profile

        mock_sp, pw, browser, ctx, page = make_fake_playwright(
            "https://www.youtube.com/watch?v=abc"
        )
        call_n = {"n": 0}

        def probe_like_impl(p):
            call_n["n"] += 1
            like_btn = MagicMock()
            like_btn.dispatch_event.side_effect = Exception("click error")
            if call_n["n"] == 1:
                return "unliked", like_btn
            return "unknown", None

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        work_dir = FakePath("work", exists=True)

        with patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_like",
                   side_effect=probe_like_impl), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
                   return_value=("subscribed", MagicMock())), \
             patch("time.sleep"):
            result = engage_from_profile(
                "Profile 1", "https://youtube.com/watch?v=abc", work_dir=work_dir,
            )
        self.assertTrue(any("like-click" in e for e in result["errors"]))

    def test_sub_click_raises_exception(self):
        from pipeline.cross_engage.cross_engage_via_playwright import engage_from_profile

        mock_sp, pw, browser, ctx, page = make_fake_playwright(
            "https://www.youtube.com/watch?v=abc"
        )

        def probe_sub_impl(p):
            sub_btn = MagicMock()
            sub_btn.scroll_into_view_if_needed.side_effect = Exception("scroll err")
            return "unsubscribed", sub_btn

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        work_dir = FakePath("work", exists=True)

        with patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_like",
                   return_value=("liked", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
                   side_effect=probe_sub_impl), \
             patch("time.sleep"):
            result = engage_from_profile(
                "Profile 1", "https://youtube.com/watch?v=abc", work_dir=work_dir,
            )
        self.assertTrue(any("sub-click" in e for e in result["errors"]))

    def test_do_like_false_do_subscribe_false(self):
        from pipeline.cross_engage.cross_engage_via_playwright import engage_from_profile

        mock_sp, pw, browser, ctx, page = make_fake_playwright(
            "https://www.youtube.com/watch?v=abc"
        )
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        work_dir = FakePath("work", exists=True)

        with patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("time.sleep"):
            result = engage_from_profile(
                "Profile 1", "https://youtube.com/watch?v=abc", work_dir=work_dir,
                do_like=False, do_subscribe=False,
            )
        self.assertIsNone(result["like"])
        self.assertIsNone(result["subscribe"])

    def test_proc_terminate_wait_fails_falls_back_to_kill(self):
        from pipeline.cross_engage.cross_engage_via_playwright import engage_from_profile

        mock_sp, pw, browser, ctx, page = make_fake_playwright(
            "https://www.youtube.com/watch?v=abc"
        )
        mock_proc = MagicMock()
        mock_proc.wait.side_effect = Exception("timeout")

        work_dir = FakePath("work", exists=True)

        with patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_like",
                   return_value=("liked", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
                   return_value=("subscribed", MagicMock())), \
             patch("time.sleep"):
            result = engage_from_profile(
                "Profile 1", "https://youtube.com/watch?v=abc", work_dir=work_dir,
            )
        mock_proc.kill.assert_called()


# ── fanout ──────────────────────────────────────────────────────────────────

class TestFanout(unittest.TestCase):
    def test_fanout_success(self):
        from pipeline.cross_engage.cross_engage_via_playwright import fanout

        work_dir = FakePath("work", exists=True)
        summary_file = FakePath("fanout-summary.json", exists=False)
        work_dir._children["fanout-summary.json"] = summary_file

        engage_result = {"profile": "Profile 2", "like": "OK", "subscribe": "OK", "errors": []}

        with patch("pipeline.cross_engage.cross_engage_via_playwright.assert_chrome_closed"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.discover_profiles",
                   return_value=["Profile 1", "Profile 2"]), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.profile_email_map",
                   return_value={"Profile 1": "src@e.com", "Profile 2": "p2@e.com"}), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.engage_from_profile",
                   return_value=engage_result):
            results = fanout(
                "https://youtube.com/watch?v=abc",
                source_profile="Profile 1",
                work_dir=work_dir,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["profile"], "Profile 2")

    def test_fanout_bridge_exception(self):
        from pipeline.cross_engage.cross_engage_via_playwright import fanout

        work_dir = FakePath("work", exists=True)
        summary_file = FakePath("fanout-summary.json", exists=False)
        work_dir._children["fanout-summary.json"] = summary_file

        with patch("pipeline.cross_engage.cross_engage_via_playwright.assert_chrome_closed"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.profile_email_map",
                   return_value={"Profile 2": "p2@e.com"}), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies",
                   side_effect=Exception("bridge fail")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.engage_from_profile"):
            results = fanout(
                "https://youtube.com/watch?v=abc",
                source_profile="Profile 1",
                work_dir=work_dir,
                profiles=["Profile 2"],
            )
        self.assertEqual(len(results), 1)
        self.assertTrue(any("bridge" in e for e in results[0]["errors"]))

    def test_fanout_engage_exception(self):
        from pipeline.cross_engage.cross_engage_via_playwright import fanout

        work_dir = FakePath("work", exists=True)
        summary_file = FakePath("fanout-summary.json", exists=False)
        work_dir._children["fanout-summary.json"] = summary_file

        with patch("pipeline.cross_engage.cross_engage_via_playwright.assert_chrome_closed"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.profile_email_map",
                   return_value={}), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.engage_from_profile",
                   side_effect=Exception("engage fail")):
            results = fanout(
                "https://youtube.com/watch?v=abc",
                source_profile="Profile 1",
                work_dir=work_dir,
                profiles=["Profile 2"],
            )
        self.assertTrue(any("engage" in e for e in results[0]["errors"]))

    def test_fanout_no_bridge(self):
        from pipeline.cross_engage.cross_engage_via_playwright import fanout

        work_dir = FakePath("work", exists=True)
        summary_file = FakePath("fanout-summary.json", exists=False)
        work_dir._children["fanout-summary.json"] = summary_file

        engage_result = {"profile": "Profile 2", "like": "OK", "subscribe": "OK", "errors": []}
        mock_bridge = MagicMock()

        with patch("pipeline.cross_engage.cross_engage_via_playwright.assert_chrome_closed"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.profile_email_map",
                   return_value={}), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies", mock_bridge), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.engage_from_profile",
                   return_value=engage_result):
            fanout(
                "https://youtube.com/watch?v=abc",
                source_profile="Profile 1",
                work_dir=work_dir,
                profiles=["Profile 2"],
                bridge_cookies_first=False,
            )
        mock_bridge.assert_not_called()

    def test_fanout_default_work_dir_and_profiles(self):
        """Cover the None-default branches for work_dir and profiles."""
        from pipeline.cross_engage.cross_engage_via_playwright import fanout

        fake_work = FakePath("pw-cross-engage", exists=True)
        fake_work._children["fanout-summary.json"] = FakePath("fanout-summary.json")

        with patch("pipeline.cross_engage.cross_engage_via_playwright.assert_chrome_closed"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.discover_profiles",
                   return_value=["Profile 1"]), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.profile_email_map",
                   return_value={"Profile 1": "src@e.com"}), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.engage_from_profile",
                   return_value={"profile": "Profile 1", "errors": []}), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_text"):
            # profiles=None triggers discover_profiles; work_dir=None uses default
            fanout(
                "https://youtube.com/watch?v=abc",
                source_profile="Profile 0",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
