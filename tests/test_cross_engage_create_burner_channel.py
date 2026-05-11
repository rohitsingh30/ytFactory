"""Tests for pipeline.cross_engage.create_burner_channel — 100% line coverage."""
from __future__ import annotations

import argparse
import io
import json
import pathlib
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch, PropertyMock, call

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests._helpers import FakePath, make_fake_playwright

import pipeline.cross_engage.create_burner_channel as _mod
from pipeline.cross_engage.create_burner_channel import (
    _cdp_alive,
    _cdp_port_for_pid,
    _create_button,
    _dialog,
    _drive_oauth_consent,
    _dump_snapshot,
    _fill_modal,
    _load_json,
    _maybe_dismiss_overlays,
    _open_create_modal,
    _resolve_handle_collision,
    _save_json,
    _shoot,
    _wait_button_enabled,
    _wait_for_channel_id,
    assert_no_collision,
    derive_handle,
    derive_slug,
    drive_create,
    find_running_chrome_debug,
    oauth_in_attached_chrome,
    random_burner_name,
    register_burner,
    resolve_profile,
)


# ── Pure helpers ──────────────────────────────────────────────────────────────

class TestPureFunctions(unittest.TestCase):
    def test_random_burner_name_default_length(self):
        name = random_burner_name()
        self.assertTrue(name.isalpha())
        self.assertGreaterEqual(len(name), 6)
        self.assertLessEqual(len(name), 12)

    def test_random_burner_name_fixed_length(self):
        name = random_burner_name(length=8)
        self.assertEqual(len(name), 8)

    def test_derive_slug(self):
        self.assertEqual(derive_slug("Hello World 2!"), "helloworld2")

    def test_derive_slug_already_clean(self):
        self.assertEqual(derive_slug("abc123"), "abc123")

    def test_derive_handle(self):
        self.assertEqual(derive_handle("myslug"), "myslug")


# ── _cdp_port_for_pid ─────────────────────────────────────────────────────────

class TestCdpPortForPid(unittest.TestCase):
    def test_no_match_returns_none(self):
        result = MagicMock()
        result.stdout = "line without chrome.stderr\n"
        with patch("subprocess.run", return_value=result):
            self.assertIsNone(_cdp_port_for_pid(1234))

    def test_partial_parts_returns_none(self):
        # line matches but fewer than 9 parts
        result = MagicMock()
        result.stdout = "cmd 1234 user chrome.stderr\n"  # only 4 parts
        with patch("subprocess.run", return_value=result):
            self.assertIsNone(_cdp_port_for_pid(1234))

    def test_oserror_on_read(self):
        result = MagicMock()
        # 9 or more parts, last is the path
        result.stdout = "cmd 1234 user 3r REG DEV 0 0 0 /tmp/chrome.stderr\n"
        with patch("subprocess.run", return_value=result), \
             patch("pathlib.Path.read_text", side_effect=OSError("no file")):
            self.assertIsNone(_cdp_port_for_pid(1234))

    def test_port_found_in_stderr(self):
        result = MagicMock()
        result.stdout = "cmd 1234 user 3r REG DEV 0 0 0 /tmp/chrome.stderr\n"
        stderr_content = "some preamble\nws://127.0.0.1:55123/devtools\nmore"
        with patch("subprocess.run", return_value=result), \
             patch("pathlib.Path.read_text", return_value=stderr_content):
            self.assertEqual(_cdp_port_for_pid(1234), 55123)

    def test_no_port_in_stderr(self):
        result = MagicMock()
        result.stdout = "cmd 1234 user 3r REG DEV 0 0 0 /tmp/chrome.stderr\n"
        with patch("subprocess.run", return_value=result), \
             patch("pathlib.Path.read_text", return_value="no port here"):
            self.assertIsNone(_cdp_port_for_pid(1234))


# ── find_running_chrome_debug ─────────────────────────────────────────────────

class TestFindRunningChromeDebug(unittest.TestCase):
    def _ps_line(self, pid, udd, prof):
        return (
            f"  {pid}   /Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            f" --user-data-dir={udd} --profile-directory={prof} --remote-debugging-port=0"
        )

    def test_no_match_returns_none(self):
        out = MagicMock()
        out.stdout = "  1234  /usr/bin/python\n"
        with patch("subprocess.run", return_value=out):
            self.assertIsNone(find_running_chrome_debug("Profile 1"))

    def test_helper_process_skipped(self):
        udd = _mod.CHROME_DEBUG_DIR
        line = (
            f"  1234  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            f" --user-data-dir={udd}  --profile-directory=Profile 1 "
            f"--type=renderer"
        )
        out = MagicMock()
        out.stdout = line + "\n"
        with patch("subprocess.run", return_value=out):
            self.assertIsNone(find_running_chrome_debug("Profile 1"))

    def test_bad_pid_skipped(self):
        out = MagicMock()
        out.stdout = (
            "  badpid  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            f" --user-data-dir={_mod.CHROME_DEBUG_DIR}  --profile-directory=Profile 1 \n"
        )
        with patch("subprocess.run", return_value=out):
            self.assertIsNone(find_running_chrome_debug("Profile 1"))

    def test_multiple_matches_returns_none(self):
        udd = _mod.CHROME_DEBUG_DIR
        line = (
            f"  {{pid}}  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            f" --user-data-dir={udd} --profile-directory=Profile 1 \n"
        )
        out = MagicMock()
        out.stdout = line.format(pid=100) + line.format(pid=200)
        with patch("subprocess.run", return_value=out):
            self.assertIsNone(find_running_chrome_debug("Profile 1"))

    def test_single_match_cdp_alive(self):
        udd = _mod.CHROME_DEBUG_DIR
        line = (
            f"  5000  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            f" --user-data-dir={udd} --profile-directory=Profile 1 --remote-debugging-port=0\n"
        )
        out = MagicMock()
        out.stdout = line
        with patch("subprocess.run", return_value=out), \
             patch.object(_mod, "_cdp_port_for_pid", return_value=9222), \
             patch.object(_mod, "_cdp_alive", return_value=True):
            result = find_running_chrome_debug("Profile 1")
        self.assertEqual(result, (5000, 9222))

    def test_single_match_cdp_dead(self):
        udd = _mod.CHROME_DEBUG_DIR
        line = (
            f"  5000  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            f" --user-data-dir={udd} --profile-directory=Profile 1 \n"
        )
        out = MagicMock()
        out.stdout = line
        with patch("subprocess.run", return_value=out), \
             patch.object(_mod, "_cdp_port_for_pid", return_value=9222), \
             patch.object(_mod, "_cdp_alive", return_value=False):
            self.assertIsNone(find_running_chrome_debug("Profile 1"))

    def test_no_port_for_pid(self):
        udd = _mod.CHROME_DEBUG_DIR
        line = (
            f"  5000  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            f" --user-data-dir={udd} --profile-directory=Profile 1 \n"
        )
        out = MagicMock()
        out.stdout = line
        with patch("subprocess.run", return_value=out), \
             patch.object(_mod, "_cdp_port_for_pid", return_value=None):
            self.assertIsNone(find_running_chrome_debug("Profile 1"))

    def test_single_match_cdp_dead_with_trailing_profile_flag(self):
        udd = _mod.CHROME_DEBUG_DIR
        line = (
            f"  5000  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            f" --user-data-dir={udd} --profile-directory=Profile 1 --remote-debugging-port=0\n"
        )
        out = MagicMock()
        out.stdout = line
        with patch("subprocess.run", return_value=out), \
             patch.object(_mod, "_cdp_port_for_pid", return_value=9222), \
             patch.object(_mod, "_cdp_alive", return_value=False):
            self.assertIsNone(find_running_chrome_debug("Profile 1"))


# ── _cdp_alive ────────────────────────────────────────────────────────────────

class TestCdpAlive(unittest.TestCase):
    def test_200_returns_true(self):
        # Post 2026-05-11 _cdp_alive parses the /json/version response
        # body and only returns True when payload["Browser"] starts with
        # "Chrome/" or "Edge/" (rejects sibling node.js V8 inspectors).
        # Mock r.read() to return a real Chrome /json/version payload.
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = b'{"Browser": "Chrome/120.0.6099.71"}'
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            self.assertTrue(_cdp_alive(9222))

    def test_url_error_returns_false(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            self.assertFalse(_cdp_alive(9222))

    def test_os_error_returns_false(self):
        with patch("urllib.request.urlopen", side_effect=OSError("reset")):
            self.assertFalse(_cdp_alive(9222))


# ── resolve_profile ───────────────────────────────────────────────────────────

class TestResolveProfile(unittest.TestCase):
    def test_override(self):
        self.assertEqual(resolve_profile("any@e.com", "Profile 2"), "Profile 2")

    def test_email_found(self):
        pmap = {"Profile 1": "host@e.com"}
        with patch.object(_mod, "profile_email_map", return_value=pmap):
            self.assertEqual(resolve_profile("host@e.com", None), "Profile 1")

    def test_email_not_found(self):
        with patch.object(_mod, "profile_email_map", return_value={}):
            with self.assertRaises(SystemExit):
                resolve_profile("missing@e.com", None)


# ── assert_no_collision ───────────────────────────────────────────────────────

class TestAssertNoCollision(unittest.TestCase):
    def test_token_exists_raises(self):
        fake_token = FakePath("youtube_token_myslug.json", exists=True)
        with patch("pathlib.Path.exists", return_value=True), \
             patch.object(_mod, "_load_json", return_value={}):
            with self.assertRaises(SystemExit):
                assert_no_collision("myslug")

    def test_slug_in_ids_raises(self):
        with patch("pathlib.Path.exists", return_value=False), \
             patch.object(_mod, "_load_json", return_value={"myslug": {"channel_id": "UC"}}):
            with self.assertRaises(SystemExit):
                assert_no_collision("myslug")

    def test_allow_existing_bypasses(self):
        with patch("pathlib.Path.exists", return_value=True), \
             patch.object(_mod, "_load_json", return_value={"myslug": {}}):
            assert_no_collision("myslug", allow_existing=True)  # no raise

    def test_clean_slug_ok(self):
        with patch("pathlib.Path.exists", return_value=False), \
             patch.object(_mod, "_load_json", return_value={}):
            assert_no_collision("newslug")  # no raise


# ── _load_json / _save_json ───────────────────────────────────────────────────

class TestJsonHelpers(unittest.TestCase):
    def test_load_missing(self):
        p = FakePath("x.json", exists=False)
        self.assertEqual(_load_json(p, {"default": 1}), {"default": 1})

    def test_load_valid(self):
        p = FakePath("x.json", exists=True)
        p._text_data = '{"a": 1}'
        self.assertEqual(_load_json(p, {}), {"a": 1})

    def test_load_oserror(self):
        p = MagicMock()
        p.exists.return_value = True
        p.read_text.side_effect = OSError("locked")
        self.assertEqual(_load_json(p, "fallback"), "fallback")

    def test_load_bad_json(self):
        p = FakePath("x.json", exists=True)
        p._text_data = "not json"
        self.assertEqual(_load_json(p, []), [])

    def test_save(self):
        p = MagicMock()
        p.parent.mkdir = MagicMock()
        _save_json(p, {"k": "v"})
        p.write_text.assert_called_once()
        written = p.write_text.call_args[0][0]
        self.assertIn('"k"', written)


# ── _shoot / _dump_snapshot ───────────────────────────────────────────────────

class TestDebugArtifacts(unittest.TestCase):
    def test_shoot_success(self):
        page = MagicMock()
        work_dir = FakePath("work")
        _shoot(page, work_dir, "step01")
        page.screenshot.assert_called_once()

    def test_shoot_exception_swallowed(self):
        page = MagicMock()
        page.screenshot.side_effect = Exception("crash")
        work_dir = FakePath("work")
        _shoot(page, work_dir, "step01")  # should not raise

    def test_dump_snapshot_success(self):
        page = MagicMock()
        page.content.return_value = "<html/>"
        page.url = "https://x.com/"
        page.title.return_value = "X"
        work_dir = FakePath("work")
        html_file = FakePath("snap.html")
        meta_file = FakePath("snap.meta.json")
        work_dir._children["step01.html"] = html_file
        work_dir._children["step01.meta.json"] = meta_file
        with patch("pathlib.Path.write_text"):
            _dump_snapshot(page, work_dir, "step01")

    def test_dump_snapshot_exception_swallowed(self):
        page = MagicMock()
        page.content.side_effect = Exception("dead")
        work_dir = FakePath("work")
        _dump_snapshot(page, work_dir, "step01")  # should not raise


# ── _maybe_dismiss_overlays ───────────────────────────────────────────────────

class TestMaybeDismissOverlays(unittest.TestCase):
    def test_channel_switcher_visible_clicks(self):
        page = MagicMock()
        switcher = MagicMock()
        switcher.is_visible.return_value = True
        row = MagicMock()
        row.count.return_value = 1
        row.is_visible.return_value = True

        call_count = {"n": 0}
        def locator_side(sel):
            lm = MagicMock()
            if "channel-switcher-renderer" in sel and "account-item" not in sel:
                lm.first = switcher
            elif "account-item" in sel:
                lm.first = row
            else:
                # consent buttons — use a fresh independent mock (not row)
                lm.first = MagicMock()
            return lm
        page.locator.side_effect = locator_side

        with patch("time.sleep"):
            _maybe_dismiss_overlays(page)
        row.click.assert_called_once()

    def test_no_switcher_continues_to_consent_buttons(self):
        page = MagicMock()
        switcher = MagicMock()
        switcher.is_visible.return_value = False
        page.locator.return_value.first = switcher
        with patch("time.sleep"):
            _maybe_dismiss_overlays(page)

    def test_switcher_exception_swallowed(self):
        page = MagicMock()
        page.locator.side_effect = Exception("dead")
        with patch("time.sleep"):
            _maybe_dismiss_overlays(page)  # should not raise

    def test_consent_button_exception_swallowed(self):
        page = MagicMock()
        switcher = MagicMock()
        switcher.is_visible.return_value = False
        # First call (channel-switcher): OK, subsequent (consent buttons): raise
        call_count = {"n": 0}
        def locator_side(sel):
            lm = MagicMock()
            lm.first = switcher if "channel-switcher" in sel else MagicMock()
            if "Accept all" in sel:
                lm.first.click.side_effect = Exception("blocked")
            return lm
        page.locator.side_effect = locator_side
        with patch("time.sleep"):
            _maybe_dismiss_overlays(page)  # should not raise


# ── _open_create_modal ────────────────────────────────────────────────────────

class TestOpenCreateModal(unittest.TestCase):
    def _make_page_with_url(self, url="https://www.youtube.com/account"):
        page = MagicMock()
        page.url = url
        page.goto.return_value = None
        return page

    def test_happy_path_button_found_create_form(self):
        page = self._make_page_with_url("https://www.youtube.com/account")
        # Source does: btn_loc = page.locator(CREATE_CHANNEL_BTN_SEL).first
        # So the locator map entry must be a parent with .first set.
        btn_first = MagicMock()
        btn_first.count.return_value = 1
        btn_parent = MagicMock()
        btn_parent.first = btn_first

        work_dir = FakePath("work")
        # time.time: outer [deadline_base, loop_check], inner [modal_deadline_base, modal_check]
        times = [0, 0.1, 31, 0, 0.1]
        with patch("time.time", side_effect=times), \
             patch("time.sleep"), \
             patch.object(_mod, "_maybe_dismiss_overlays"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_dump_snapshot"):
            # Modal-kind loop: DIALOG_TITLE_TEXT has count()=1 → create_form
            create_form_loc = MagicMock()
            create_form_loc.count.return_value = 1
            phone_loc = MagicMock()
            phone_loc.count.return_value = 0
            verify_loc = MagicMock()
            verify_loc.count.return_value = 0

            locator_map = {
                _mod.CREATE_CHANNEL_BTN_SEL: btn_parent,
                f"text={_mod.DIALOG_TITLE_TEXT}": create_form_loc,
                "text='Get advanced features'": phone_loc,
                "button:has-text('Verify')": verify_loc,
            }
            def locator2(sel):
                return locator_map.get(sel, MagicMock())
            page.locator.side_effect = locator2

            _open_create_modal(page, work_dir, personal_handle="x", email="e@e.com")

    def test_service_login_raises(self):
        page = self._make_page_with_url("https://accounts.google.com/ServiceLogin")
        btn_loc = MagicMock()
        btn_loc.count.return_value = 0
        page.locator.return_value.first = btn_loc
        page.locator.return_value.count.return_value = 0

        work_dir = FakePath("work")
        with patch("time.time", side_effect=[0, 0.1, 31]), \
             patch("time.sleep"), \
             patch.object(_mod, "_maybe_dismiss_overlays"), \
             patch.object(_mod, "_dump_snapshot"):
            with self.assertRaises(SystemExit):
                _open_create_modal(page, work_dir, personal_handle="x", email="e@e.com")

    def test_button_not_found_timeout_raises(self):
        page = self._make_page_with_url("https://www.youtube.com/account")
        btn_loc = MagicMock()
        btn_loc.count.return_value = 0  # never found
        page.locator.return_value.first = btn_loc
        page.locator.return_value.count.return_value = 0

        work_dir = FakePath("work")
        # deadline exceeded immediately (second call > first + 30)
        with patch("time.time", side_effect=[0, 31, 32, 33, 34, 35]), \
             patch("time.sleep"), \
             patch.object(_mod, "_maybe_dismiss_overlays"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_dump_snapshot"):
            with self.assertRaises(SystemExit):
                _open_create_modal(page, work_dir, personal_handle="x", email="e@e.com")

    def test_accountchooser_click(self):
        """Cover the accountchooser interstitial path."""
        url_calls = {"n": 0}
        def get_url(self_=None):
            url_calls["n"] += 1
            if url_calls["n"] <= 2:
                return "https://accounts.google.com/v3/signin/accountchooser"
            return "https://www.youtube.com/account"

        page = MagicMock()
        type(page).url = PropertyMock(side_effect=get_url)
        page.goto.return_value = None

        btn_loc = MagicMock()
        # Found after accountchooser click
        btn_loc.count.side_effect = [0, 0, 1]

        email_row = MagicMock()
        email_row.count.return_value = 1
        email_row.is_visible.return_value = True

        def locator_side(sel):
            lm = MagicMock()
            if sel == _mod.CREATE_CHANNEL_BTN_SEL:
                lm.first = btn_loc
                return lm
            return lm
        page.locator.side_effect = locator_side
        page.get_by_text.return_value.first = email_row

        create_form_loc = MagicMock()
        create_form_loc.count.return_value = 1
        phone_loc = MagicMock()
        phone_loc.count.return_value = 0
        verify_loc = MagicMock()
        verify_loc.count.return_value = 0

        def locator2(sel):
            if sel == _mod.CREATE_CHANNEL_BTN_SEL:
                lm = MagicMock(); lm.first = btn_loc; return lm
            if f"text={_mod.DIALOG_TITLE_TEXT}" == sel:
                return create_form_loc
            if "Get advanced features" in sel:
                return phone_loc
            if "Verify" in sel:
                return verify_loc
            lm = MagicMock(); lm.count.return_value = 0; return lm
        page.locator.side_effect = locator2

        work_dir = FakePath("work")
        # time values: base=0, loop iters, modal loop
        times = [0] + [0.1]*10 + [31] + [0]*5 + [31]
        with patch("time.time", side_effect=times), \
             patch("time.sleep"), \
             patch.object(_mod, "_maybe_dismiss_overlays"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_dump_snapshot"):
            _open_create_modal(page, work_dir, personal_handle="x", email="user@e.com")

    def test_accountchooser_click_exception_continues(self):
        url_calls = {"n": 0}
        def get_url(self_=None):
            url_calls["n"] += 1
            if url_calls["n"] == 1:
                return "https://accounts.google.com/v3/signin/accountchooser"
            return "https://www.youtube.com/account"

        page = MagicMock()
        type(page).url = PropertyMock(side_effect=get_url)
        page.goto.return_value = None

        btn_loc = MagicMock()
        btn_loc.count.side_effect = [0, 1]
        email_row = MagicMock()
        email_row.count.return_value = 1
        email_row.is_visible.return_value = True
        email_row.click.side_effect = Exception("row detached")
        page.get_by_text.return_value.first = email_row

        create_form_loc = MagicMock()
        create_form_loc.count.return_value = 1
        phone_loc = MagicMock()
        phone_loc.count.return_value = 0
        verify_loc = MagicMock()
        verify_loc.count.return_value = 0

        def locator2(sel):
            if sel == _mod.CREATE_CHANNEL_BTN_SEL:
                lm = MagicMock(); lm.first = btn_loc; return lm
            if f"text={_mod.DIALOG_TITLE_TEXT}" == sel:
                return create_form_loc
            if "Get advanced features" in sel:
                return phone_loc
            if "Verify" in sel:
                return verify_loc
            lm = MagicMock(); lm.count.return_value = 0; return lm
        page.locator.side_effect = locator2

        work_dir = FakePath("work")
        with patch("time.time", side_effect=[0] + [0.1]*10 + [31] + [0, 0.1, 31]), \
             patch("time.sleep"), \
             patch.object(_mod, "_maybe_dismiss_overlays"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_dump_snapshot"):
            _open_create_modal(page, work_dir, personal_handle="x", email="user@e.com")

    def test_signin_prompt_timeout(self):
        url_calls = {"n": 0}
        def get_url(self_=None):
            return "https://www.youtube.com/signin_prompt"

        page = MagicMock()
        type(page).url = PropertyMock(side_effect=get_url)
        page.goto.return_value = None
        page.locator.return_value.first.count.return_value = 0
        page.locator.return_value.count.return_value = 0

        work_dir = FakePath("work")
        # Simulate: deadline=0+30, first iter time=0.1, signin_prompt_seen_at=0.1,
        # second iter time=13 → 13-0.1 > 12 → SystemExit
        time_vals = [0, 0.1, 0.1, 13, 31]
        with patch("time.time", side_effect=time_vals), \
             patch("time.sleep"), \
             patch.object(_mod, "_maybe_dismiss_overlays"), \
             patch.object(_mod, "_dump_snapshot"):
            with self.assertRaises(SystemExit):
                _open_create_modal(page, work_dir, personal_handle="x", email="e@e.com")

    def test_phone_verification_raises(self):
        page = self._make_page_with_url("https://www.youtube.com/account")
        btn_loc = MagicMock()
        btn_loc.count.return_value = 1  # found immediately

        phone_loc = MagicMock()
        phone_loc.count.return_value = 1  # phone verification modal
        create_form_loc = MagicMock()
        create_form_loc.count.return_value = 0
        verify_loc = MagicMock()
        verify_loc.count.return_value = 0

        def locator2(sel):
            if sel == _mod.CREATE_CHANNEL_BTN_SEL:
                lm = MagicMock(); lm.first = btn_loc; return lm
            if f"text={_mod.DIALOG_TITLE_TEXT}" == sel:
                return create_form_loc
            if "Get advanced features" in sel:
                return phone_loc
            if "Verify" in sel:
                return verify_loc
            lm = MagicMock(); lm.count.return_value = 0; return lm
        page.locator.side_effect = locator2

        work_dir = FakePath("work")
        times = [0, 0.1, 31, 0, 0.1, 31]
        with patch("time.time", side_effect=times), \
             patch("time.sleep"), \
             patch.object(_mod, "_maybe_dismiss_overlays"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_dump_snapshot"):
            with self.assertRaises(SystemExit):
                _open_create_modal(page, work_dir, personal_handle="x", email="e@e.com")

    def test_no_modal_raises(self):
        page = self._make_page_with_url("https://www.youtube.com/account")
        btn_loc = MagicMock()
        btn_loc.count.return_value = 1

        no_modal = MagicMock()
        no_modal.count.return_value = 0

        def locator2(sel):
            if sel == _mod.CREATE_CHANNEL_BTN_SEL:
                lm = MagicMock(); lm.first = btn_loc; return lm
            lm = MagicMock(); lm.count.return_value = 0; return lm
        page.locator.side_effect = locator2

        work_dir = FakePath("work")
        # Button loop: [deadline_base, loop_check]; Modal loop: [deadline_base, iter1, iter2, exit]
        times = [0, 0.1, 31, 0, 31, 50]
        with patch("time.time", side_effect=times), \
             patch("time.sleep"), \
             patch.object(_mod, "_maybe_dismiss_overlays"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_dump_snapshot"):
            with self.assertRaises(SystemExit):
                _open_create_modal(page, work_dir, personal_handle="x", email="e@e.com")


# ── _fill_modal ───────────────────────────────────────────────────────────────

class TestFillModal(unittest.TestCase):
    def test_success(self):
        page = MagicMock()
        inp1 = MagicMock()
        inp1.is_visible.return_value = True
        inp2 = MagicMock()
        inp2.is_visible.return_value = True
        dialog = MagicMock()
        dialog.locator.return_value.all.return_value = [inp1, inp2]
        page.locator.return_value.last = dialog

        work_dir = FakePath("work")
        with patch("time.sleep"):
            _fill_modal(page, work_dir, display_name="BurnerCh", handle="burnerch")
        inp1.fill.assert_called_once_with("")
        inp2.fill.assert_called_once_with("")

    def test_too_few_inputs_raises(self):
        page = MagicMock()
        inp1 = MagicMock()
        inp1.is_visible.return_value = True
        dialog = MagicMock()
        dialog.locator.return_value.all.return_value = [inp1]
        page.locator.return_value.last = dialog
        work_dir = FakePath("work")
        with patch("time.sleep"), \
             patch.object(_mod, "_dump_snapshot"):
            with self.assertRaises(SystemExit):
                _fill_modal(page, work_dir, display_name="X", handle="x")

    def test_hidden_inputs_excluded(self):
        page = MagicMock()
        inp1 = MagicMock()
        inp1.is_visible.return_value = True
        inp_hidden = MagicMock()
        inp_hidden.is_visible.return_value = False
        inp2 = MagicMock()
        inp2.is_visible.return_value = True
        dialog = MagicMock()
        dialog.locator.return_value.all.return_value = [inp1, inp_hidden, inp2]
        page.locator.return_value.last = dialog
        work_dir = FakePath("work")
        with patch("time.sleep"):
            _fill_modal(page, work_dir, display_name="X", handle="x")
        inp1.fill.assert_called_once()
        inp2.fill.assert_called_once()


# ── _resolve_handle_collision ─────────────────────────────────────────────────

class TestResolveHandleCollision(unittest.TestCase):
    def test_suggestion_visible_clicked(self):
        page = MagicMock()
        suggestion = MagicMock()
        suggestion.count.return_value = 1
        suggestion.is_visible.return_value = True
        suggestion.text_content.return_value = "@burner-x4m"
        dialog = MagicMock()
        dialog.locator.return_value.first = suggestion
        page.locator.return_value.last = dialog
        work_dir = FakePath("work")
        with patch("time.sleep"), patch.object(_mod, "_shoot"):
            result = _resolve_handle_collision(page, work_dir)
        self.assertEqual(result, "burner-x4m")

    def test_no_suggestion(self):
        page = MagicMock()
        suggestion = MagicMock()
        suggestion.count.return_value = 0
        dialog = MagicMock()
        dialog.locator.return_value.first = suggestion
        page.locator.return_value.last = dialog
        work_dir = FakePath("work")
        result = _resolve_handle_collision(page, work_dir)
        self.assertIsNone(result)

    def test_exception_returns_none(self):
        page = MagicMock()
        suggestion = MagicMock()
        suggestion.count.side_effect = Exception("crash")
        dialog = MagicMock()
        dialog.locator.return_value.first = suggestion
        page.locator.return_value.last = dialog
        result = _resolve_handle_collision(page, FakePath("work"))
        self.assertIsNone(result)


# ── _wait_button_enabled ──────────────────────────────────────────────────────

class TestCreateButton(unittest.TestCase):
    def test_create_button_returns_locator(self):
        page = MagicMock()
        button = MagicMock()
        dialog = MagicMock()
        dialog.get_by_role.return_value.first = button
        page.locator.return_value.last = dialog
        self.assertIs(_create_button(page), button)


class TestWaitButtonEnabled(unittest.TestCase):
    def test_returns_true_immediately(self):
        btn = MagicMock()
        btn.is_enabled.return_value = True
        with patch("time.time", side_effect=[0, 0.1, 13]):
            self.assertTrue(_wait_button_enabled(btn, timeout_s=12))

    def test_returns_false_on_timeout(self):
        btn = MagicMock()
        btn.is_enabled.return_value = False
        with patch("time.time", side_effect=[0, 13, 14]), \
             patch("time.sleep"):
            self.assertFalse(_wait_button_enabled(btn, timeout_s=12))

    def test_exception_in_is_enabled_skipped(self):
        btn = MagicMock()
        btn.is_enabled.side_effect = [Exception("crash"), True]
        with patch("time.time", side_effect=[0, 0.1, 0.2, 13]), \
             patch("time.sleep"):
            self.assertTrue(_wait_button_enabled(btn, timeout_s=12))


# ── _wait_for_channel_id ──────────────────────────────────────────────────────

class TestWaitForChannelId(unittest.TestCase):
    def test_id_from_url(self):
        page = MagicMock()
        page.url = "https://www.youtube.com/channel/UCabc1234567890abcdef12"
        page.wait_for_url.return_value = None
        result = _wait_for_channel_id(page, FakePath("work"))
        self.assertEqual(result, "UCabc1234567890abcdef12")

    def test_id_from_html_scrape(self):
        page = MagicMock()
        page.url = "https://www.youtube.com/account"  # no UC in URL
        page.wait_for_url.side_effect = Exception("timeout")
        page.content.return_value = '{"channelId":"UCabc1234567890abcdef12","other":"val"}'
        result = _wait_for_channel_id(page, FakePath("work"))
        self.assertEqual(result, "UCabc1234567890abcdef12")

    def test_no_id_returns_none(self):
        page = MagicMock()
        page.url = "https://www.youtube.com/account"
        page.wait_for_url.side_effect = Exception("timeout")
        page.content.return_value = "<html>no id here</html>"
        with patch.object(_mod, "_shoot"), patch.object(_mod, "_dump_snapshot"):
            result = _wait_for_channel_id(page, FakePath("work"))
        self.assertIsNone(result)

    def test_content_exception_handled(self):
        page = MagicMock()
        page.url = "https://www.youtube.com/account"
        page.wait_for_url.side_effect = Exception("timeout")
        page.content.side_effect = Exception("dead")
        with patch.object(_mod, "_shoot"), patch.object(_mod, "_dump_snapshot"):
            result = _wait_for_channel_id(page, FakePath("work"))
        self.assertIsNone(result)


# ── _drive_oauth_consent ──────────────────────────────────────────────────────

class TestDriveOauthConsent(unittest.TestCase):
    def test_callback_url_returns_true(self):
        page = MagicMock()
        page.url = "http://localhost:8089/?code=abc"
        page.title.return_value = ""
        page.wait_for_load_state.return_value = None
        with patch("time.sleep"), patch.object(_mod, "_shoot"):
            result = _drive_oauth_consent(page, FakePath("work"), host_email="e@e.com")
        self.assertTrue(result)

    def test_wait_for_load_state_exception_ignored(self):
        page = MagicMock()
        page.url = "http://localhost:8089/?code=abc"
        page.title.return_value = ""
        page.wait_for_load_state.side_effect = Exception("networkidle timeout")
        with patch("time.sleep"), patch.object(_mod, "_shoot"):
            result = _drive_oauth_consent(page, FakePath("work"), host_email="e@e.com")
        self.assertTrue(result)

    def test_account_chooser_click(self):
        url_calls = {"n": 0}
        def get_url(s=None):
            url_calls["n"] += 1
            if url_calls["n"] <= 2:
                return "https://accounts.google.com/accountchooser?..."
            return "http://localhost:8089/?code=abc"

        page = MagicMock()
        type(page).url = PropertyMock(side_effect=get_url)
        page.title.return_value = ""
        page.wait_for_load_state.return_value = None
        row = MagicMock()
        row.count.return_value = 1
        row.click.return_value = None
        page.locator.return_value.first = row
        with patch("time.sleep"), patch.object(_mod, "_shoot"):
            result = _drive_oauth_consent(page, FakePath("work"), host_email="e@e.com")
        self.assertTrue(result)

    def test_account_chooser_data_email_fallback(self):
        url_calls = {"n": 0}
        def get_url(s=None):
            url_calls["n"] += 1
            return "https://accounts.google.com/accountchooser" if url_calls["n"] <= 4 \
                else "http://localhost:8089/?code=abc"

        page = MagicMock()
        type(page).url = PropertyMock(side_effect=get_url)
        page.title.return_value = ""
        page.wait_for_load_state.return_value = None

        # first locator (data-email) returns count=0, second (text) returns 1
        call_count = {"n": 0}
        def locator_side(sel):
            lm = MagicMock()
            call_count["n"] += 1
            if "data-email" in sel:
                lm.first.count.return_value = 0
            else:
                lm.first.count.return_value = 1
            return lm
        page.locator.side_effect = locator_side

        with patch("time.sleep"), patch.object(_mod, "_shoot"):
            result = _drive_oauth_consent(page, FakePath("work"), host_email="e@e.com")
        self.assertTrue(result)

    def test_consent_summary_tick_and_continue(self):
        url_calls = {"n": 0}
        def get_url(s=None):
            url_calls["n"] += 1
            return "https://accounts.google.com/oauth/v2/consentsummary" if url_calls["n"] <= 2 \
                else "http://localhost:8089/?code=abc"

        page = MagicMock()
        type(page).url = PropertyMock(side_effect=get_url)
        page.title.return_value = ""
        page.wait_for_load_state.return_value = None

        checkbox = MagicMock()
        checkbox.count.return_value = 1
        checkbox.is_checked.return_value = False
        page.get_by_role.return_value.first = checkbox

        cont_btn = MagicMock()
        cont_btn.count.return_value = 1
        # Need to differentiate checkbox and Continue
        checkbox_call = {"n": 0}
        def get_role(role, **kwargs):
            name = kwargs.get("name", "")
            lm = MagicMock()
            if role == "checkbox":
                lm.first = checkbox
            elif role == "button":
                lm.first = cont_btn
            return lm
        page.get_by_role.side_effect = get_role

        with patch("time.sleep"), patch.object(_mod, "_shoot"):
            result = _drive_oauth_consent(page, FakePath("work"), host_email="e@e.com")
        self.assertTrue(result)

    def test_consent_summary_uses_input_checkbox_fallback(self):
        url_calls = {"n": 0}
        def get_url(s=None):
            url_calls["n"] += 1
            return "https://accounts.google.com/oauth/v2/consentsummary" if url_calls["n"] <= 1 \
                else "http://localhost:8089/?code=abc"

        page = MagicMock()
        type(page).url = PropertyMock(side_effect=get_url)
        page.title.return_value = ""
        page.wait_for_load_state.return_value = None

        empty_checkbox = MagicMock()
        empty_checkbox.count.return_value = 0
        fallback_checkbox = MagicMock()
        fallback_checkbox.count.return_value = 1
        fallback_checkbox.is_checked.return_value = False
        cont_btn = MagicMock()
        cont_btn.count.return_value = 1

        def get_role(role, **kwargs):
            lm = MagicMock()
            lm.first = empty_checkbox if role == "checkbox" else cont_btn
            return lm
        page.get_by_role.side_effect = get_role
        page.locator.return_value.first = fallback_checkbox

        with patch("time.sleep"), patch.object(_mod, "_shoot"):
            result = _drive_oauth_consent(page, FakePath("work"), host_email="e@e.com")
        self.assertTrue(result)
        fallback_checkbox.check.assert_called_once()

    def test_consent_summary_checkbox_exception_swallowed(self):
        url_calls = {"n": 0}
        def get_url(s=None):
            url_calls["n"] += 1
            return "https://accounts.google.com/oauth/v2/consentsummary" if url_calls["n"] <= 2 \
                else "http://localhost:8089/?code=abc"

        page = MagicMock()
        type(page).url = PropertyMock(side_effect=get_url)
        page.title.return_value = ""
        page.wait_for_load_state.return_value = None

        checkbox = MagicMock()
        checkbox.count.return_value = 1
        checkbox.is_checked.return_value = False
        checkbox.check.side_effect = Exception("element detached")

        cont_btn = MagicMock()
        cont_btn.count.return_value = 1

        def get_role(role, **kwargs):
            lm = MagicMock()
            lm.first = checkbox if role == "checkbox" else cont_btn
            return lm
        page.get_by_role.side_effect = get_role

        with patch("time.sleep"), patch.object(_mod, "_shoot"):
            result = _drive_oauth_consent(page, FakePath("work"), host_email="e@e.com")
        self.assertTrue(result)

    def test_generic_continue_button(self):
        url_calls = {"n": 0}
        def get_url(s=None):
            url_calls["n"] += 1
            return "https://accounts.google.com/oauth/v2/other" if url_calls["n"] <= 2 \
                else "http://localhost:8089/?code=abc"

        page = MagicMock()
        type(page).url = PropertyMock(side_effect=get_url)
        page.title.return_value = ""
        page.wait_for_load_state.return_value = None
        page.locator.return_value.first.count.return_value = 0

        cont_btn = MagicMock()
        cont_btn.count.return_value = 1
        cont_btn.is_visible.return_value = True

        def get_role(role, **kwargs):
            lm = MagicMock()
            lm.first = cont_btn
            return lm
        page.get_by_role.side_effect = get_role

        with patch("time.sleep"), patch.object(_mod, "_shoot"):
            result = _drive_oauth_consent(page, FakePath("work"), host_email="e@e.com")
        self.assertTrue(result)

    def test_no_actionable_element_returns_false(self):
        page = MagicMock()
        page.url = "https://accounts.google.com/oauth/v2/other"
        page.title.return_value = ""
        page.wait_for_load_state.return_value = None
        page.locator.return_value.first.count.return_value = 0

        no_btn = MagicMock()
        no_btn.count.return_value = 0
        page.get_by_role.return_value.first = no_btn

        with patch("time.sleep"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_dump_snapshot"):
            result = _drive_oauth_consent(page, FakePath("work"),
                                          host_email="e@e.com", max_steps=1)
        self.assertFalse(result)

    def test_max_steps_exhausted_returns_false(self):
        page = MagicMock()
        page.url = "https://accounts.google.com/oauth/v2/other"
        page.title.return_value = ""
        page.wait_for_load_state.return_value = None
        page.locator.return_value.first.count.return_value = 0

        btn = MagicMock()
        btn.count.return_value = 1
        btn.is_visible.return_value = True
        page.get_by_role.return_value.first = btn

        with patch("time.sleep"), patch.object(_mod, "_shoot"):
            result = _drive_oauth_consent(page, FakePath("work"),
                                          host_email="e@e.com", max_steps=2)
        # With max_steps=2, it'll click each step but never reach localhost
        self.assertFalse(result)

    def test_generic_button_exception_skipped(self):
        """Cover the except: continue in the generic button loop."""
        url_calls = {"n": 0}
        def get_url(s=None):
            url_calls["n"] += 1
            return "https://accounts.google.com/oauth/v2/other" if url_calls["n"] <= 2 \
                else "http://localhost:8089/?code=abc"

        page = MagicMock()
        type(page).url = PropertyMock(side_effect=get_url)
        page.title.return_value = ""
        page.wait_for_load_state.return_value = None
        page.locator.return_value.first.count.return_value = 0

        # First button raises (covered by except: continue), second is visible+clickable
        call_count = {"n": 0}
        def get_role(role, **kwargs):
            call_count["n"] += 1
            lm = MagicMock()
            btn = MagicMock()
            if call_count["n"] == 1:
                btn.count.return_value = 1
                btn.is_visible.side_effect = Exception("err")
            else:
                btn.count.return_value = 1
                btn.is_visible.return_value = True
            lm.first = btn
            return lm
        page.get_by_role.side_effect = get_role

        with patch("time.sleep"), patch.object(_mod, "_shoot"):
            result = _drive_oauth_consent(page, FakePath("work"), host_email="e@e.com")
        self.assertTrue(result)


# ── oauth_in_attached_chrome ──────────────────────────────────────────────────

class TestOauthInAttachedChrome(unittest.TestCase):
    def _make_proc(self, url_line="https://accounts.google.com/o/oauth2/auth?...", rc=0):
        proc = MagicMock()
        poll_calls = {"n": 0}
        def poll_side():
            poll_calls["n"] += 1
            return None if poll_calls["n"] <= 2 else rc
        proc.poll.side_effect = poll_side
        stdout_lines = [url_line + "\n", "", "token written\n"]
        stdout_call = {"n": 0}
        def readline():
            stdout_call["n"] += 1
            if stdout_call["n"] <= len(stdout_lines):
                return stdout_lines[stdout_call["n"] - 1]
            return ""
        proc.stdout.readline.side_effect = readline
        proc.stdout.readlines.return_value = []
        return proc

    def test_success(self):
        proc = self._make_proc()
        page = MagicMock()
        work_dir = FakePath("work")
        work_dir._children["oauth.log"] = FakePath("oauth.log")

        token_path = FakePath("youtube_token_b1.json", exists=True)

        with patch("subprocess.Popen", return_value=proc), \
             patch("time.time", side_effect=[0] + [0.5]*100), \
             patch("time.sleep"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_drive_oauth_consent", return_value=True), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.open", return_value=io.StringIO()):
            result = oauth_in_attached_chrome("b1", page, work_dir, host_email="e@e.com")
        self.assertTrue(result)

    def test_empty_readline_consent_false_and_leftover_lines(self):
        proc = MagicMock()
        proc.poll.side_effect = [None, None, 0, 0]
        proc.stdout.readline.side_effect = [
            "",
            "https://accounts.google.com/o/oauth2/auth?x=1\n",
        ]
        proc.stdout.readlines.return_value = ["leftover token line\n"]
        page = MagicMock()
        work_dir = FakePath("work")
        work_dir._children["oauth.log"] = FakePath("oauth.log")

        with patch("subprocess.Popen", return_value=proc), \
             patch("time.time", side_effect=[0, 0.5, 0.5, 1.0, 1.5]), \
             patch("time.sleep"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_drive_oauth_consent", return_value=False), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.open", return_value=io.StringIO()):
            result = oauth_in_attached_chrome("b1", page, work_dir, host_email="e@e.com")
        self.assertTrue(result)

    def test_no_auth_url_returns_false(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdout.readline.return_value = ""
        proc.terminate.return_value = None
        proc.wait.return_value = None
        work_dir = FakePath("work")
        work_dir._children["oauth.log"] = FakePath("oauth.log")

        with patch("subprocess.Popen", return_value=proc), \
             patch("time.time", side_effect=[0, 61, 62]), \
             patch("time.sleep"), \
             patch("pathlib.Path.open", return_value=io.StringIO()):
            result = oauth_in_attached_chrome("b1", MagicMock(), work_dir, host_email="e@e.com")
        self.assertFalse(result)

    def test_goto_exception_continues(self):
        proc = self._make_proc()
        page = MagicMock()
        page.goto.side_effect = Exception("nav error")
        work_dir = FakePath("work")
        work_dir._children["oauth.log"] = FakePath("oauth.log")

        with patch("subprocess.Popen", return_value=proc), \
             patch("time.time", side_effect=[0] + [0.5]*50), \
             patch("time.sleep"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_drive_oauth_consent", return_value=True), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.open", return_value=io.StringIO()):
            result = oauth_in_attached_chrome("b1", page, work_dir, host_email="e@e.com")
        self.assertTrue(result)

    def test_rc_nonzero_returns_false(self):
        proc = self._make_proc(rc=1)
        page = MagicMock()
        work_dir = FakePath("work")
        work_dir._children["oauth.log"] = FakePath("oauth.log")

        with patch("subprocess.Popen", return_value=proc), \
             patch("time.time", side_effect=[0] + [0.5]*50), \
             patch("time.sleep"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_drive_oauth_consent", return_value=True), \
             patch("pathlib.Path.open", return_value=io.StringIO()):
            result = oauth_in_attached_chrome("b1", page, work_dir, host_email="e@e.com")
        self.assertFalse(result)

    def test_timeout_returns_false(self):
        proc = MagicMock()
        poll_calls = {"n": 0}
        def poll_side():
            return None  # never exits
        proc.poll.side_effect = poll_side
        proc.stdout.readline.return_value = "https://accounts.google.com/o/oauth2/auth?...\n"
        proc.stdout.readlines.return_value = []

        work_dir = FakePath("work")
        work_dir._children["oauth.log"] = FakePath("oauth.log")

        # time.time: URL search deadline (0, 0.5...), then oauth drain (immediately timeout)
        times = [0] + [0.5]*5 + [0, 700]  # url found quickly, then poll loop exceeds timeout
        with patch("subprocess.Popen", return_value=proc), \
             patch("time.time", side_effect=times), \
             patch("time.sleep"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_drive_oauth_consent", return_value=True), \
             patch("pathlib.Path.open", return_value=io.StringIO()):
            result = oauth_in_attached_chrome("b1", MagicMock(), work_dir,
                                               host_email="e@e.com", timeout_s=600)
        self.assertFalse(result)

    def test_token_missing_returns_false(self):
        proc = self._make_proc(rc=0)
        work_dir = FakePath("work")
        work_dir._children["oauth.log"] = FakePath("oauth.log")

        with patch("subprocess.Popen", return_value=proc), \
             patch("time.time", side_effect=[0] + [0.5]*50), \
             patch("time.sleep"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_drive_oauth_consent", return_value=True), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.open", return_value=io.StringIO()):
            result = oauth_in_attached_chrome("b1", MagicMock(), work_dir, host_email="e@e.com")
        self.assertFalse(result)

    def test_no_auth_url_terminate_raises(self):
        """Cover proc.kill() when terminate+wait fails."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdout.readline.return_value = ""
        proc.terminate.return_value = None
        proc.wait.side_effect = Exception("timeout")

        work_dir = FakePath("work")
        work_dir._children["oauth.log"] = FakePath("oauth.log")

        with patch("subprocess.Popen", return_value=proc), \
             patch("time.time", side_effect=[0, 61, 62]), \
             patch("time.sleep"), \
             patch("pathlib.Path.open", return_value=io.StringIO()):
            result = oauth_in_attached_chrome("b1", MagicMock(), work_dir, host_email="e@e.com")
        self.assertFalse(result)
        proc.kill.assert_called_once()

    def test_timeout_terminate_raises(self):
        """Cover proc.kill() when terminate+wait fails in timeout path."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdout.readline.side_effect = [
            "https://accounts.google.com/auth?x=1\n", ""
        ]
        proc.stdout.readlines.return_value = []
        proc.terminate.return_value = None
        proc.wait.side_effect = Exception("stuck")

        work_dir = FakePath("work")
        work_dir._children["oauth.log"] = FakePath("oauth.log")

        times = [0, 0.5, 0.5, 0, 700]
        with patch("subprocess.Popen", return_value=proc), \
             patch("time.time", side_effect=times), \
             patch("time.sleep"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_drive_oauth_consent", return_value=True), \
             patch("pathlib.Path.open", return_value=io.StringIO()):
            result = oauth_in_attached_chrome("b1", MagicMock(), work_dir,
                                               host_email="e@e.com", timeout_s=600)
        self.assertFalse(result)
        proc.kill.assert_called()


# ── drive_create ──────────────────────────────────────────────────────────────

class TestDriveCreate(unittest.TestCase):
    def _common_mocks(self, page, work_dir):
        return dict(
            page=page, work_dir=work_dir,
            display_name="BurnerCh", handle="burnerch",
            personal_handle="newrtrudaj", email="host@e.com",
            dry_run=False,
        )

    def test_dry_run_stops_before_click(self):
        page = MagicMock()
        work_dir = FakePath("work")
        with patch.object(_mod, "_open_create_modal"), \
             patch.object(_mod, "_fill_modal"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_create_button"), \
             patch.object(_mod, "_wait_button_enabled", return_value=True), \
             patch("time.sleep"):
            result = drive_create(
                page, work_dir=work_dir, display_name="B", handle="b",
                personal_handle="x", email="e@e.com", dry_run=True,
            )
        self.assertIsNone(result["channel_id"])
        self.assertEqual(result["step_reached"], "modal_filled")

    def test_button_disabled_then_collision_fixed(self):
        page = MagicMock()
        work_dir = FakePath("work")
        btn = MagicMock()
        enabled_calls = {"n": 0}
        def wait_enabled(b, timeout_s=12):
            enabled_calls["n"] += 1
            # first call: disabled; second call: enabled after collision fix
            return enabled_calls["n"] >= 2
        # dialog mock: make err.count() = 0 so the "Failed to create" check passes
        dialog_mock = MagicMock()
        dialog_mock.locator.return_value.first.count.return_value = 0
        with patch.object(_mod, "_open_create_modal"), \
             patch.object(_mod, "_fill_modal"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_create_button", return_value=btn), \
             patch.object(_mod, "_wait_button_enabled", side_effect=wait_enabled), \
             patch.object(_mod, "_resolve_handle_collision", return_value="althandle"), \
             patch.object(_mod, "_wait_for_channel_id", return_value="UCnew1234567890abcdef12"), \
             patch.object(_mod, "_dialog", return_value=dialog_mock), \
             patch("time.sleep"):
            result = drive_create(
                page, work_dir=work_dir, display_name="B", handle="b",
                personal_handle="x", email="e@e.com", dry_run=False,
            )
        self.assertEqual(result["channel_id"], "UCnew1234567890abcdef12")
        self.assertEqual(result["handle_actual"], "althandle")

    def test_button_still_disabled_after_collision_fix(self):
        page = MagicMock()
        work_dir = FakePath("work")
        btn = MagicMock()
        err_loc = MagicMock()
        err_loc.count.return_value = 1
        err_loc.text_content.return_value = "not available"
        dialog = MagicMock()
        dialog.locator.return_value.first = err_loc
        page.locator.return_value.last = dialog
        with patch.object(_mod, "_open_create_modal"), \
             patch.object(_mod, "_fill_modal"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_create_button", return_value=btn), \
             patch.object(_mod, "_wait_button_enabled", return_value=False), \
             patch.object(_mod, "_resolve_handle_collision", return_value=None), \
             patch.object(_mod, "_dump_snapshot"), \
             patch.object(_mod, "_dialog", return_value=dialog), \
             patch("time.sleep"):
            result = drive_create(
                page, work_dir=work_dir, display_name="B", handle="b",
                personal_handle="x", email="e@e.com", dry_run=False,
            )
        self.assertTrue(result["errors"])

    def test_create_click_exception(self):
        page = MagicMock()
        work_dir = FakePath("work")
        btn = MagicMock()
        btn.click.side_effect = Exception("click failed")
        with patch.object(_mod, "_open_create_modal"), \
             patch.object(_mod, "_fill_modal"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_create_button", return_value=btn), \
             patch.object(_mod, "_wait_button_enabled", return_value=True), \
             patch("time.sleep"):
            result = drive_create(
                page, work_dir=work_dir, display_name="B", handle="b",
                personal_handle="x", email="e@e.com", dry_run=False,
            )
        self.assertTrue(any("click" in e.lower() for e in result["errors"]))

    def test_youtube_refused_error(self):
        page = MagicMock()
        work_dir = FakePath("work")
        btn = MagicMock()
        err_loc = MagicMock()
        err_loc.count.return_value = 1
        err_loc.is_visible.return_value = True
        err_loc.text_content.return_value = "Failed to create channel"
        dialog = MagicMock()
        dialog.locator.return_value.first = err_loc
        with patch.object(_mod, "_open_create_modal"), \
             patch.object(_mod, "_fill_modal"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_create_button", return_value=btn), \
             patch.object(_mod, "_wait_button_enabled", return_value=True), \
             patch.object(_mod, "_dialog", return_value=dialog), \
             patch.object(_mod, "_dump_snapshot"), \
             patch("time.sleep"):
            result = drive_create(
                page, work_dir=work_dir, display_name="B", handle="b",
                personal_handle="x", email="e@e.com", dry_run=False,
            )
        self.assertTrue(any("refused" in e.lower() for e in result["errors"]))

    def test_youtube_refused_exception_swallowed(self):
        """Cover except: pass in the err.is_visible check."""
        page = MagicMock()
        work_dir = FakePath("work")
        btn = MagicMock()
        err_loc = MagicMock()
        err_loc.count.return_value = 1
        err_loc.is_visible.side_effect = Exception("detached")
        dialog = MagicMock()
        dialog.locator.return_value.first = err_loc
        with patch.object(_mod, "_open_create_modal"), \
             patch.object(_mod, "_fill_modal"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_create_button", return_value=btn), \
             patch.object(_mod, "_wait_button_enabled", return_value=True), \
             patch.object(_mod, "_dialog", return_value=dialog), \
             patch.object(_mod, "_wait_for_channel_id", return_value="UCabc12345678901234567"), \
             patch("time.sleep"):
            result = drive_create(
                page, work_dir=work_dir, display_name="B", handle="b",
                personal_handle="x", email="e@e.com", dry_run=False,
            )
        self.assertEqual(result["channel_id"], "UCabc12345678901234567")

    def test_channel_id_recovered_from_final_url(self):
        page = MagicMock()
        page.url = "https://www.youtube.com/channel/UCabc1234567890abcdef12"
        work_dir = FakePath("work")
        btn = MagicMock()
        dialog = MagicMock()
        dialog.locator.return_value.first.count.return_value = 0
        with patch.object(_mod, "_open_create_modal"), \
             patch.object(_mod, "_fill_modal"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_create_button", return_value=btn), \
             patch.object(_mod, "_wait_button_enabled", return_value=True), \
             patch.object(_mod, "_dialog", return_value=dialog), \
             patch.object(_mod, "_wait_for_channel_id", return_value=None), \
             patch("time.sleep"):
            result = drive_create(
                page, work_dir=work_dir, display_name="B", handle="b",
                personal_handle="x", email="e@e.com", dry_run=False,
            )
        self.assertEqual(result["channel_id"], "UCabc1234567890abcdef12")

    def test_no_channel_id_at_all(self):
        page = MagicMock()
        page.url = "https://www.youtube.com/account"
        work_dir = FakePath("work")
        btn = MagicMock()
        dialog = MagicMock()
        dialog.locator.return_value.first.count.return_value = 0
        with patch.object(_mod, "_open_create_modal"), \
             patch.object(_mod, "_fill_modal"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_create_button", return_value=btn), \
             patch.object(_mod, "_wait_button_enabled", return_value=True), \
             patch.object(_mod, "_dialog", return_value=dialog), \
             patch.object(_mod, "_wait_for_channel_id", return_value=None), \
             patch("time.sleep"):
            result = drive_create(
                page, work_dir=work_dir, display_name="B", handle="b",
                personal_handle="x", email="e@e.com", dry_run=False,
            )
        self.assertIsNone(result["channel_id"])
        self.assertTrue(result["errors"])

    def test_err_count_zero_skips_refused_check(self):
        """err.count() == 0 path in the YouTube-refused check."""
        page = MagicMock()
        page.url = "https://www.youtube.com/channel/UCabc1234567890abcdef12"
        work_dir = FakePath("work")
        btn = MagicMock()
        err_loc = MagicMock()
        err_loc.count.return_value = 0  # no error
        dialog = MagicMock()
        dialog.locator.return_value.first = err_loc
        with patch.object(_mod, "_open_create_modal"), \
             patch.object(_mod, "_fill_modal"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_create_button", return_value=btn), \
             patch.object(_mod, "_wait_button_enabled", return_value=True), \
             patch.object(_mod, "_dialog", return_value=dialog), \
             patch.object(_mod, "_wait_for_channel_id", return_value="UCabc1234567890abcdef12"), \
             patch("time.sleep"):
            result = drive_create(
                page, work_dir=work_dir, display_name="B", handle="b",
                personal_handle="x", email="e@e.com", dry_run=False,
            )
        self.assertEqual(result["channel_id"], "UCabc1234567890abcdef12")

    def test_err_text_content_exception_swallowed(self):
        """Cover the except: msg = "" path in button-still-disabled branch."""
        page = MagicMock()
        work_dir = FakePath("work")
        btn = MagicMock()
        err_loc = MagicMock()
        err_loc.count.return_value = 1
        err_loc.text_content.side_effect = Exception("detached")
        dialog = MagicMock()
        dialog.locator.return_value.first = err_loc
        with patch.object(_mod, "_open_create_modal"), \
             patch.object(_mod, "_fill_modal"), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_create_button", return_value=btn), \
             patch.object(_mod, "_wait_button_enabled", return_value=False), \
             patch.object(_mod, "_resolve_handle_collision", return_value=None), \
             patch.object(_mod, "_dump_snapshot"), \
             patch.object(_mod, "_dialog", return_value=dialog), \
             patch("time.sleep"):
            result = drive_create(
                page, work_dir=work_dir, display_name="B", handle="b",
                personal_handle="x", email="e@e.com", dry_run=False,
            )
        self.assertTrue(result["errors"])


# ── register_burner ───────────────────────────────────────────────────────────

class TestRegisterBurner(unittest.TestCase):
    def test_new_slug(self):
        ids = {}
        pmap = {}
        with patch.object(_mod, "_load_json", side_effect=[ids, pmap]), \
             patch.object(_mod, "_save_json") as mock_save:
            register_burner("b1", channel_id="UC_X", title="Burner1", email="e@e.com")
        self.assertEqual(mock_save.call_count, 2)
        # Check the ids dict was mutated
        self.assertIn("b1", ids)
        self.assertEqual(ids["b1"]["channel_id"], "UC_X")

    def test_existing_slug_merged(self):
        ids = {"b1": {"channel_id": "OLD"}}
        pmap = {"b1": {"foo": "bar"}}  # dict → merge
        with patch.object(_mod, "_load_json", side_effect=[ids, pmap]), \
             patch.object(_mod, "_save_json") as mock_save:
            register_burner("b1", channel_id="NEW", title="B1", email="new@e.com")
        self.assertEqual(ids["b1"]["channel_id"], "NEW")
        self.assertEqual(pmap["b1"]["email"], "new@e.com")
        self.assertEqual(pmap["b1"]["foo"], "bar")  # merged

    def test_existing_slug_non_dict_profile_map(self):
        ids = {}
        pmap = {"b1": "oldstring"}  # not a dict → replaced
        with patch.object(_mod, "_load_json", side_effect=[ids, pmap]), \
             patch.object(_mod, "_save_json"):
            register_burner("b1", channel_id="UC_X", title="B", email="e@e.com")
        self.assertIsInstance(pmap["b1"], dict)


# ── run() ─────────────────────────────────────────────────────────────────────

class TestRun(unittest.TestCase):
    def _args(self, **kwargs):
        base = argparse.Namespace(
            display_name=None, slug=None, handle=None,
            email=_mod.DEFAULT_EMAIL, personal_handle="newrtrudaj",
            profile=None, work_dir=FakePath("work"),
            force_fresh=False, keep_open=False,
            no_oauth=False, no_register=False,
            allow_existing=False, dry_run=False,
        )
        for k, v in kwargs.items():
            setattr(base, k, v)
        return base

    def _run_with_mocks(self, args, *, attached=False, channel_id="UCnew1234", keep_open=False,
                        oauth_ok=True, no_oauth=False, no_register=False, drive_error=False,
                        find_side_effect=None):
        mock_sp, pw, browser, ctx, page = make_fake_playwright("https://www.youtube.com/")
        ctx.new_page.return_value = page

        proc = MagicMock()
        proc.pid = 9999
        proc.wait.return_value = 0

        find_result = (9999, 12345) if attached else None
        if find_side_effect is None:
            find_side_effect = [find_result, find_result]

        wizard = {"channel_id": channel_id, "step_reached": "channel_created",
                  "errors": (["drive error"] if drive_error else [])}

        work_dir_slug = FakePath("burnerch")
        args.work_dir._children["burnerch"] = work_dir_slug
        result_json = FakePath("result.json")
        work_dir_slug._children["result.json"] = result_json

        from pipeline.cross_engage.create_burner_channel import run as _run_fn
        with patch.object(_mod, "resolve_profile", return_value="Profile 1"), \
             patch.object(_mod, "assert_no_collision"), \
             patch.object(_mod, "find_running_chrome_debug", side_effect=find_side_effect), \
             patch.object(_mod, "bridge_cookies"), \
             patch.object(_mod, "_clear_singleton"), \
             patch.object(_mod, "launch_chrome_for", return_value=(proc, 12345)), \
             patch.object(_mod, "drive_create", return_value=wizard), \
             patch.object(_mod, "register_burner"), \
             patch.object(_mod, "oauth_in_attached_chrome", return_value=oauth_ok), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("time.sleep"), \
             patch("pathlib.Path.resolve", return_value=work_dir_slug), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_text"):
            args.keep_open = keep_open
            args.no_oauth = no_oauth
            args.no_register = no_register
            return _run_fn(args)

    def test_fresh_chrome_registers(self):
        args = self._args(display_name="BurnerCh", slug="burnerch")
        result = self._run_with_mocks(args, attached=False)
        self.assertEqual(result["channel_id"], "UCnew1234")

    def test_attached_chrome(self):
        args = self._args(display_name="BurnerCh", slug="burnerch")
        result = self._run_with_mocks(args, attached=True)
        self.assertEqual(result["channel_id"], "UCnew1234")

    def test_attach_canonical_first_sibling_second(self):
        """Cover the loop: first find_running_chrome_debug returns None, second returns a result."""
        args = self._args(display_name="BurnerCh", slug="burnerch")
        result = self._run_with_mocks(args, attached=False,
                                      find_side_effect=[None, (9999, 12345)])
        self.assertIsNotNone(result)

    def test_keep_open(self):
        args = self._args(display_name="BurnerCh", slug="burnerch")
        result = self._run_with_mocks(args, attached=False, keep_open=True)
        self.assertIsNotNone(result)

    def test_no_register(self):
        args = self._args(display_name="BurnerCh", slug="burnerch")
        with patch.object(_mod, "register_burner") as mock_reg:
            result = self._run_with_mocks(args, no_register=True)
        # register_burner should not be called
        mock_reg.assert_not_called()

    def test_no_oauth(self):
        args = self._args(display_name="BurnerCh", slug="burnerch")
        result = self._run_with_mocks(args, no_oauth=True)
        self.assertIsNone(result.get("oauth_complete"))

    def test_oauth_with_attached(self):
        args = self._args(display_name="BurnerCh", slug="burnerch")
        result = self._run_with_mocks(args, attached=True, oauth_ok=True)
        # oauth_complete should be set
        self.assertTrue(result.get("oauth_complete"))

    def test_oauth_fails(self):
        args = self._args(display_name="BurnerCh", slug="burnerch")
        result = self._run_with_mocks(args, attached=True, oauth_ok=False)
        self.assertFalse(result.get("oauth_complete"))

    def test_oauth_exception_handled(self):
        args = self._args(display_name="BurnerCh", slug="burnerch")
        mock_sp, pw, browser, ctx, page = make_fake_playwright("https://www.youtube.com/")
        ctx.new_page.return_value = page
        proc = MagicMock()
        proc.pid = 9999
        proc.wait.return_value = 0
        wizard = {"channel_id": "UCnew1234", "step_reached": "channel_created", "errors": []}
        work_dir_slug = FakePath("burnerch")
        args.work_dir._children["burnerch"] = work_dir_slug
        result_json = FakePath("result.json")
        work_dir_slug._children["result.json"] = result_json

        from pipeline.cross_engage.create_burner_channel import run as _run_fn
        with patch.object(_mod, "resolve_profile", return_value="Profile 1"), \
             patch.object(_mod, "assert_no_collision"), \
             patch.object(_mod, "find_running_chrome_debug", return_value=(9999, 12345)), \
             patch.object(_mod, "drive_create", return_value=wizard), \
             patch.object(_mod, "register_burner"), \
             patch.object(_mod, "oauth_in_attached_chrome", side_effect=Exception("oauth crash")), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("time.sleep"), \
             patch("pathlib.Path.resolve", return_value=work_dir_slug), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_text"):
            result = _run_fn(args)
        self.assertFalse(result.get("oauth_complete"))

    def test_drive_create_exception_reraises(self):
        args = self._args(display_name="BurnerCh", slug="burnerch")
        mock_sp, pw, browser, ctx, page = make_fake_playwright("https://www.youtube.com/")
        ctx.new_page.return_value = page
        proc = MagicMock()
        work_dir_slug = FakePath("burnerch")
        args.work_dir._children["burnerch"] = work_dir_slug
        result_json = FakePath("result.json")
        work_dir_slug._children["result.json"] = result_json

        from pipeline.cross_engage.create_burner_channel import run as _run_fn
        with patch.object(_mod, "resolve_profile", return_value="Profile 1"), \
             patch.object(_mod, "assert_no_collision"), \
             patch.object(_mod, "find_running_chrome_debug", return_value=None), \
             patch.object(_mod, "bridge_cookies"), \
             patch.object(_mod, "_clear_singleton"), \
             patch.object(_mod, "launch_chrome_for", return_value=(proc, 12345)), \
             patch.object(_mod, "drive_create", side_effect=Exception("wizard crash")), \
             patch.object(_mod, "_shoot"), \
             patch.object(_mod, "_dump_snapshot"), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("time.sleep"), \
             patch("pathlib.Path.resolve", return_value=work_dir_slug), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_text"):
            with self.assertRaises(Exception, msg="wizard crash"):
                _run_fn(args)

    def test_proc_kill_on_wait_failure(self):
        """Cover proc.kill() when proc.wait() raises."""
        args = self._args(display_name="BurnerCh", slug="burnerch")
        mock_sp, pw, browser, ctx, page = make_fake_playwright("https://www.youtube.com/")
        ctx.new_page.return_value = page
        proc = MagicMock()
        proc.wait.side_effect = Exception("stuck")
        wizard = {"channel_id": None, "step_reached": "modal_filled", "errors": []}
        work_dir_slug = FakePath("burnerch")
        args.work_dir._children["burnerch"] = work_dir_slug
        result_json = FakePath("result.json")
        work_dir_slug._children["result.json"] = result_json

        from pipeline.cross_engage.create_burner_channel import run as _run_fn
        with patch.object(_mod, "resolve_profile", return_value="Profile 1"), \
             patch.object(_mod, "assert_no_collision"), \
             patch.object(_mod, "find_running_chrome_debug", return_value=None), \
             patch.object(_mod, "bridge_cookies"), \
             patch.object(_mod, "_clear_singleton"), \
             patch.object(_mod, "launch_chrome_for", return_value=(proc, 12345)), \
             patch.object(_mod, "drive_create", return_value=wizard), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("time.sleep"), \
             patch("pathlib.Path.resolve", return_value=work_dir_slug), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_text"):
            result = _run_fn(args)
        proc.kill.assert_called_once()

    def test_empty_slug_raises(self):
        from pipeline.cross_engage.create_burner_channel import run as _run_fn
        args = self._args(display_name="!!!", slug=None)  # derive_slug gives ""
        with patch.object(_mod, "random_burner_name", return_value="!!!"):
            with self.assertRaises(SystemExit):
                _run_fn(args)

    def test_no_channel_id_no_register(self):
        args = self._args(display_name="BurnerCh", slug="burnerch")
        result = self._run_with_mocks(args, channel_id=None)
        self.assertIsNone(result.get("channel_id"))


# ── main() ────────────────────────────────────────────────────────────────────

class TestMain(unittest.TestCase):
    from pipeline.cross_engage.create_burner_channel import main

    def test_display_name_too_long(self):
        from pipeline.cross_engage.create_burner_channel import main
        with patch("sys.argv", ["prog", "--display-name", "A" * 51]):
            with self.assertRaises(SystemExit):
                main()

    def test_single_run_success(self):
        from pipeline.cross_engage.create_burner_channel import main
        with patch("sys.argv", ["prog", "--display-name", "burnerch", "--no-oauth"]), \
             patch.object(_mod, "run", return_value={"channel_id": "UCabc", "errors": []}):
            rc = main()
        self.assertEqual(rc, 0)

    def test_single_run_fail(self):
        from pipeline.cross_engage.create_burner_channel import main
        with patch("sys.argv", ["prog", "--display-name", "burnerch", "--no-oauth"]), \
             patch.object(_mod, "run", return_value={"channel_id": None, "errors": ["fail"]}):
            rc = main()
        self.assertEqual(rc, 1)

    def test_dry_run_success(self):
        from pipeline.cross_engage.create_burner_channel import main
        with patch("sys.argv", ["prog", "--dry-run", "--no-oauth"]), \
             patch.object(_mod, "run", return_value={
                 "channel_id": None, "step_reached": "modal_filled", "errors": []
             }):
            rc = main()
        self.assertEqual(rc, 0)

    def test_all_emails_success(self):
        from pipeline.cross_engage.create_burner_channel import main
        pmap = {"Profile 1": "a@e.com", "Profile 2": "b@e.com"}
        work_dir = FakePath("work")
        with patch("sys.argv", ["prog", "--all-emails", "--work-dir", "/tmp/pw-create-burner"]), \
             patch.object(_mod, "profile_email_map", return_value=pmap), \
             patch.object(_mod, "run", return_value={
                 "step_reached": "modal_filled", "errors": [],
             }), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_text"):
            rc = main()
        self.assertEqual(rc, 0)

    def test_all_emails_system_exit(self):
        from pipeline.cross_engage.create_burner_channel import main
        pmap = {"Profile 1": "a@e.com"}
        with patch("sys.argv", ["prog", "--all-emails", "--work-dir", "/tmp/pw-create-burner"]), \
             patch.object(_mod, "profile_email_map", return_value=pmap), \
             patch.object(_mod, "run", side_effect=SystemExit("no cookie")), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_text"):
            rc = main()
        self.assertEqual(rc, 0)  # aggregation doesn't propagate

    def test_all_emails_exception(self):
        from pipeline.cross_engage.create_burner_channel import main
        pmap = {"Profile 1": "a@e.com"}
        with patch("sys.argv", ["prog", "--all-emails", "--work-dir", "/tmp/pw-create-burner"]), \
             patch.object(_mod, "profile_email_map", return_value=pmap), \
             patch.object(_mod, "run", side_effect=Exception("crash")), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_text"):
            rc = main()
        self.assertEqual(rc, 0)  # aggregation always returns 0


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
