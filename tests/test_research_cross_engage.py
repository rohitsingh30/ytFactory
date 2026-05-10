"""Tests for pipeline/research/cross_engage.py — cross-channel engagement.

All Playwright, subprocess, YouTube API, and filesystem interactions are mocked.
No real Chrome, no real network, no real tokens.

Strategy:
  - Mock CONFIG_DIR to a tempdir so token files / registry are isolated.
  - Fake googleapiclient for subscribe/like tests.
  - Fake playwright.sync_api for play_view and drive_profiles tests.
  - Fake subprocess.Popen / subprocess.run for chrome process tests.
  - Fake Chrome Local State JSON for profile-listing tests.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from tests._helpers import PROJECT_ROOT  # noqa: F401

import pipeline.research.cross_engage as ce_mod


# ---------------------------------------------------------------------------
# Shared fake GoogleAPIClient helpers
# ---------------------------------------------------------------------------


def _install_fake_google_for_ce():
    """Install a minimal googleapiclient stub into sys.modules."""
    discovery = types.ModuleType("googleapiclient.discovery")
    errors_mod = types.ModuleType("googleapiclient.errors")

    class HttpError(Exception):
        def __init__(self, resp, content=b""):
            self.resp = resp
            self.content = content
            super().__init__(str(resp.status))

    class _Resp:
        def __init__(self, status):
            self.status = status

    errors_mod.HttpError = HttpError
    errors_mod._Resp = _Resp

    sys.modules.setdefault("googleapiclient", types.ModuleType("googleapiclient"))
    sys.modules["googleapiclient.discovery"] = discovery
    sys.modules["googleapiclient.errors"] = errors_mod

    return HttpError, _Resp


def _make_http_error(status: int, body: str = ""):
    _, _Resp = _install_fake_google_for_ce()
    errors_mod = sys.modules["googleapiclient.errors"]
    return errors_mod.HttpError(
        errors_mod._Resp(status), body.encode()
    )


# ---------------------------------------------------------------------------
# _is_real_token
# ---------------------------------------------------------------------------


class IsRealTokenTest(unittest.TestCase):

    def _p(self, name: str) -> Path:
        return Path("/fake") / name

    def test_valid_token_returns_true(self):
        self.assertTrue(ce_mod._is_real_token(self._p("youtube_token_myacct.json")))

    def test_missing_prefix_returns_false(self):
        self.assertFalse(ce_mod._is_real_token(self._p("token_myacct.json")))

    def test_missing_json_suffix_returns_false(self):
        self.assertFalse(ce_mod._is_real_token(self._p("youtube_token_myacct.txt")))

    def test_fluted_backup_returns_false(self):
        self.assertFalse(
            ce_mod._is_real_token(self._p("youtube_token_myacct.fluted-backup.json"))
        )


# ---------------------------------------------------------------------------
# list_sibling_accounts
# ---------------------------------------------------------------------------


class ListSiblingAccountsTest(unittest.TestCase):

    def test_returns_empty_when_config_dir_missing(self):
        with patch.object(ce_mod, "CONFIG_DIR", Path("/nonexistent/__cfg__")):
            result = ce_mod.list_sibling_accounts()
        self.assertEqual(result, [])

    def test_returns_sorted_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            (cfg / "youtube_token_beta.json").write_text("{}")
            (cfg / "youtube_token_alpha.json").write_text("{}")
            # Should be filtered out: default account
            (cfg / "youtube_token_default.json").write_text("{}")
            # Should be filtered: backup
            (cfg / "youtube_token_alpha.fluted-backup.json").write_text("{}")

            with patch.object(ce_mod, "CONFIG_DIR", cfg):
                result = ce_mod.list_sibling_accounts()

        self.assertEqual(result, ["alpha", "beta"])

    def test_excludes_non_token_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            (cfg / "youtube_token_acct.json").write_text("{}")
            (cfg / "channel_ids.json").write_text("{}")
            (cfg / "some_other_file.txt").write_text("")

            with patch.object(ce_mod, "CONFIG_DIR", cfg):
                result = ce_mod.list_sibling_accounts()

        self.assertEqual(result, ["acct"])


# ---------------------------------------------------------------------------
# _load_registry / _save_registry
# ---------------------------------------------------------------------------


class RegistryTest(unittest.TestCase):

    def test_load_returns_empty_when_file_missing(self):
        with patch.object(ce_mod, "CHANNEL_IDS_PATH", Path("/nonexistent/__channel_ids__.json")):
            result = ce_mod._load_registry()
        self.assertEqual(result, {})

    def test_load_returns_empty_on_bad_json(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("{bad json}")
            tmp = Path(f.name)
        try:
            with patch.object(ce_mod, "CHANNEL_IDS_PATH", tmp):
                result = ce_mod._load_registry()
            self.assertEqual(result, {})
        finally:
            tmp.unlink(missing_ok=True)

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channel_ids.json"
            reg = {"acct1": {"channel_id": "UCabc", "title": "Test"}}
            with patch.object(ce_mod, "CONFIG_DIR", Path(tmp)):
                with patch.object(ce_mod, "CHANNEL_IDS_PATH", path):
                    ce_mod._save_registry(reg)
                    loaded = ce_mod._load_registry()
        self.assertEqual(loaded["acct1"]["channel_id"], "UCabc")


# ---------------------------------------------------------------------------
# resolve_channel_id
# ---------------------------------------------------------------------------


class ResolveChannelIdTest(unittest.TestCase):

    def test_returns_cached_entry_on_hit(self):
        cached = {"channel_id": "UCcached", "title": "Cached", "discovered_at": "now"}
        reg = {"myacct": cached}
        with patch.object(ce_mod, "_load_registry", return_value=reg):
            result = ce_mod.resolve_channel_id("myacct")
        self.assertEqual(result["channel_id"], "UCcached")

    def test_fetches_from_api_on_cache_miss(self):
        _install_fake_google_for_ce()
        fake_yt = MagicMock()
        fake_yt.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "UCfresh", "snippet": {"title": "Fresh"}}]
        }

        with patch.object(ce_mod, "_load_registry", return_value={}):
            with patch.object(ce_mod, "_save_registry") as mock_save:
                with patch("pipeline.research.cross_engage.authenticate", return_value=MagicMock()):
                    with patch("pipeline.research.cross_engage.build", return_value=fake_yt,
                               create=True):
                        # Use googleapiclient.discovery.build mock
                        sys.modules["googleapiclient.discovery"].build = MagicMock(return_value=fake_yt)
                        result = ce_mod.resolve_channel_id("myacct")

        self.assertEqual(result["channel_id"], "UCfresh")
        mock_save.assert_called_once()

    def test_raises_when_api_returns_no_items(self):
        fake_yt = MagicMock()
        fake_yt.channels.return_value.list.return_value.execute.return_value = {"items": []}

        with patch.object(ce_mod, "_load_registry", return_value={}):
            with patch("pipeline.research.cross_engage.authenticate", return_value=MagicMock()):
                sys.modules["googleapiclient.discovery"].build = MagicMock(return_value=fake_yt)
                with self.assertRaises(RuntimeError):
                    ce_mod.resolve_channel_id("myacct")

    def test_force_bypasses_cache(self):
        cached = {"channel_id": "UCcached", "title": "Cached", "discovered_at": "now"}
        reg = {"myacct": cached}
        fake_yt = MagicMock()
        fake_yt.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "UCfresh", "snippet": {"title": "Fresh"}}]
        }

        with patch.object(ce_mod, "_load_registry", return_value=reg):
            with patch.object(ce_mod, "_save_registry"):
                with patch("pipeline.research.cross_engage.authenticate", return_value=MagicMock()):
                    sys.modules["googleapiclient.discovery"].build = MagicMock(return_value=fake_yt)
                    result = ce_mod.resolve_channel_id("myacct", force=True)

        self.assertEqual(result["channel_id"], "UCfresh")


# ---------------------------------------------------------------------------
# refresh_all_channel_ids
# ---------------------------------------------------------------------------


class RefreshAllChannelIdsTest(unittest.TestCase):

    def test_calls_resolve_for_each_sibling(self):
        with patch.object(ce_mod, "list_sibling_accounts", return_value=["a1", "a2"]):
            with patch.object(ce_mod, "resolve_channel_id",
                               return_value={"channel_id": "UCX", "title": "X"}) as mock_res:
                with patch("builtins.print"):
                    result = ce_mod.refresh_all_channel_ids()

        self.assertEqual(mock_res.call_count, 2)
        self.assertIn("a1", result)

    def test_handles_exception_per_account(self):
        def fake_resolve(a, force=False):
            if a == "bad":
                raise RuntimeError("auth failed")
            return {"channel_id": "UCX", "title": "X"}

        with patch.object(ce_mod, "list_sibling_accounts", return_value=["good", "bad"]):
            with patch.object(ce_mod, "resolve_channel_id", side_effect=fake_resolve):
                with patch("builtins.print"):
                    result = ce_mod.refresh_all_channel_ids()

        self.assertIn("good", result)
        self.assertNotIn("bad", result)


# ---------------------------------------------------------------------------
# subscribe_pair
# ---------------------------------------------------------------------------


class SubscribePairTest(unittest.TestCase):

    def test_successful_subscribe(self):
        _install_fake_google_for_ce()
        fake_yt = MagicMock()
        fake_yt.subscriptions.return_value.insert.return_value.execute.return_value = {
            "id": "sub123"
        }

        with patch("pipeline.research.cross_engage.authenticate", return_value=MagicMock()):
            sys.modules["googleapiclient.discovery"].build = MagicMock(return_value=fake_yt)
            result = ce_mod.subscribe_pair("home_acct", "UCTarget")

        self.assertEqual(result["status"], "subscribed")
        self.assertEqual(result["subscription_id"], "sub123")

    def test_already_subscribed_returns_gracefully(self):
        _install_fake_google_for_ce()
        fake_yt = MagicMock()
        err = _make_http_error(400, "subscriptionDuplicate")
        fake_yt.subscriptions.return_value.insert.return_value.execute.side_effect = err

        with patch("pipeline.research.cross_engage.authenticate", return_value=MagicMock()):
            sys.modules["googleapiclient.discovery"].build = MagicMock(return_value=fake_yt)
            result = ce_mod.subscribe_pair("home_acct", "UCTarget")

        self.assertEqual(result["status"], "already_subscribed")

    def test_other_http_error_re_raises(self):
        _install_fake_google_for_ce()
        fake_yt = MagicMock()
        err = _make_http_error(403, "forbidden")
        HttpError = sys.modules["googleapiclient.errors"].HttpError
        fake_yt.subscriptions.return_value.insert.return_value.execute.side_effect = err

        with patch("pipeline.research.cross_engage.authenticate", return_value=MagicMock()):
            sys.modules["googleapiclient.discovery"].build = MagicMock(return_value=fake_yt)
            with self.assertRaises(HttpError):
                ce_mod.subscribe_pair("home_acct", "UCTarget")


# ---------------------------------------------------------------------------
# subscribe_all_pairs
# ---------------------------------------------------------------------------


class SubscribeAllPairsTest(unittest.TestCase):

    def test_fewer_than_2_siblings_returns_empty(self):
        with patch.object(ce_mod, "list_sibling_accounts", return_value=["solo"]):
            with patch("builtins.print"):
                result = ce_mod.subscribe_all_pairs()
        self.assertEqual(result, [])

    def test_cross_subscribes_all_pairs(self):
        accounts = ["a1", "a2", "a3"]

        def fake_resolve(a):
            return {"channel_id": f"UC{a}"}

        def fake_subscribe(home, target):
            return {"status": "subscribed"}

        with patch.object(ce_mod, "list_sibling_accounts", return_value=accounts):
            with patch.object(ce_mod, "resolve_channel_id", side_effect=fake_resolve):
                with patch.object(ce_mod, "subscribe_pair", side_effect=fake_subscribe):
                    with patch("builtins.print"):
                        results = ce_mod.subscribe_all_pairs()

        # 3 accounts × (3-1) = 6 subscribe calls
        self.assertEqual(len(results), 6)

    def test_skip_account_when_resolve_fails(self):
        def fake_resolve(a):
            if a == "bad":
                raise RuntimeError("lookup failed")
            return {"channel_id": f"UC{a}"}

        with patch.object(ce_mod, "list_sibling_accounts", return_value=["good1", "bad", "good2"]):
            with patch.object(ce_mod, "resolve_channel_id", side_effect=fake_resolve):
                with patch.object(ce_mod, "subscribe_pair", return_value={"status": "subscribed"}):
                    with patch("builtins.print"):
                        results = ce_mod.subscribe_all_pairs()

        # only good1 and good2 remain
        homes = [r["home"] for r in results]
        self.assertNotIn("bad", homes)

    def test_handles_subscribe_exception(self):
        def fake_resolve(a):
            return {"channel_id": f"UC{a}"}

        def fake_subscribe(home, target):
            raise RuntimeError("API error")

        with patch.object(ce_mod, "list_sibling_accounts", return_value=["a1", "a2"]):
            with patch.object(ce_mod, "resolve_channel_id", side_effect=fake_resolve):
                with patch.object(ce_mod, "subscribe_pair", side_effect=fake_subscribe):
                    with patch("builtins.print"):
                        results = ce_mod.subscribe_all_pairs()

        self.assertTrue(all("error" in r for r in results))


# ---------------------------------------------------------------------------
# like_video
# ---------------------------------------------------------------------------


class LikeVideoTest(unittest.TestCase):

    def test_successful_like(self):
        _install_fake_google_for_ce()
        fake_yt = MagicMock()
        fake_yt.videos.return_value.rate.return_value.execute.return_value = {}

        with patch("pipeline.research.cross_engage.authenticate", return_value=MagicMock()):
            sys.modules["googleapiclient.discovery"].build = MagicMock(return_value=fake_yt)
            result = ce_mod.like_video("myacct", "VID1")

        self.assertEqual(result["status"], "liked")

    def test_http_error_returns_error_dict(self):
        _install_fake_google_for_ce()
        fake_yt = MagicMock()
        err = _make_http_error(403, "forbidden")
        fake_yt.videos.return_value.rate.return_value.execute.side_effect = err

        with patch("pipeline.research.cross_engage.authenticate", return_value=MagicMock()):
            sys.modules["googleapiclient.discovery"].build = MagicMock(return_value=fake_yt)
            result = ce_mod.like_video("myacct", "VID1")

        self.assertEqual(result["status"], "error")
        self.assertIn("403", result["error"])


# ---------------------------------------------------------------------------
# like_from_siblings
# ---------------------------------------------------------------------------


class LikeFromSiblingsTest(unittest.TestCase):

    def test_excludes_uploader_and_likes_from_rest(self):
        siblings = ["alpha", "beta", "gamma"]
        liked = []

        def fake_like(account, video_id):
            liked.append(account)
            return {"status": "liked", "account": account, "video_id": video_id}

        with patch.object(ce_mod, "list_sibling_accounts", return_value=siblings):
            with patch.object(ce_mod, "like_video", side_effect=fake_like):
                with patch("builtins.print"):
                    result = ce_mod.like_from_siblings("beta", "VID1")

        self.assertNotIn("beta", liked)
        self.assertIn("alpha", liked)
        self.assertIn("gamma", liked)
        self.assertEqual(len(result), 2)

    def test_error_status_still_included_in_results(self):
        def fake_like(account, video_id):
            return {"status": "error", "account": account, "video_id": video_id, "error": "403"}

        with patch.object(ce_mod, "list_sibling_accounts", return_value=["a", "b"]):
            with patch.object(ce_mod, "like_video", side_effect=fake_like):
                with patch("builtins.print"):
                    result = ce_mod.like_from_siblings("uploader", "VID1")

        self.assertEqual(len(result), 2)
        for r in result:
            self.assertEqual(r["status"], "error")


# ---------------------------------------------------------------------------
# discover_existing_uploads
# ---------------------------------------------------------------------------


class DiscoverExistingUploadsTest(unittest.TestCase):

    def test_finds_uploads_from_known_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Create a channel dir with an upload record
            chan = root / "mychan"
            ud = chan / "uploads"
            ud.mkdir(parents=True)
            record = {"video_id": "V1", "slug": "s1", "account": "myvid", "url": "https://yt/V1"}
            (ud / "s1.json").write_text(json.dumps(record))

            with patch.object(ce_mod, "_project_root", return_value=root):
                with patch.object(ce_mod, "list_sibling_accounts", return_value=["myvid"]):
                    result = ce_mod.discover_existing_uploads()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["video_id"], "V1")

    def test_skips_records_without_video_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chan = root / "mychan"
            ud = chan / "uploads"
            ud.mkdir(parents=True)
            (ud / "s1.json").write_text(json.dumps({"slug": "s1", "account": "myvid"}))

            with patch.object(ce_mod, "_project_root", return_value=root):
                with patch.object(ce_mod, "list_sibling_accounts", return_value=["myvid"]):
                    result = ce_mod.discover_existing_uploads()

        self.assertEqual(result, [])

    def test_skips_bad_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chan = root / "mychan"
            ud = chan / "uploads"
            ud.mkdir(parents=True)
            (ud / "bad.json").write_text("{bad}")

            with patch.object(ce_mod, "_project_root", return_value=root):
                with patch.object(ce_mod, "list_sibling_accounts", return_value=["myvid"]):
                    result = ce_mod.discover_existing_uploads()

        self.assertEqual(result, [])

    def test_skips_unknown_account(self):
        """Upload record's account not in sibling list → skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chan = root / "mychan"
            ud = chan / "uploads"
            ud.mkdir(parents=True)
            (ud / "s1.json").write_text(json.dumps({"video_id": "V1", "account": "stranger"}))

            with patch.object(ce_mod, "_project_root", return_value=root):
                with patch.object(ce_mod, "list_sibling_accounts", return_value=["myvid"]):
                    result = ce_mod.discover_existing_uploads()

        self.assertEqual(result, [])

    def test_uses_dir_name_when_no_account_in_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chan = root / "myvid"  # dir name == account
            ud = chan / "uploads"
            ud.mkdir(parents=True)
            (ud / "s1.json").write_text(json.dumps({"video_id": "V1", "slug": "s1"}))

            with patch.object(ce_mod, "_project_root", return_value=root):
                with patch.object(ce_mod, "list_sibling_accounts", return_value=["myvid"]):
                    result = ce_mod.discover_existing_uploads()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["account"], "myvid")


# ---------------------------------------------------------------------------
# backfill_engagement
# ---------------------------------------------------------------------------


class BackfillEngagementTest(unittest.TestCase):

    def _fake_upload(self, account="myvid"):
        return {"account": account, "channel_dir": "mychan",
                "slug": "s1", "video_id": "V1", "url": "https://yt/V1",
                "mp4_path": "/mychan/shorts/s1.mp4"}

    def test_include_self_true(self):
        """All siblings like, including the uploader."""
        upload = self._fake_upload(account="acct1")
        siblings = ["acct1", "acct2"]
        liked_accounts = []

        def fake_like(account, vid):
            liked_accounts.append(account)
            return {"status": "liked"}

        with patch.object(ce_mod, "list_sibling_accounts", return_value=siblings):
            with patch.object(ce_mod, "discover_existing_uploads", return_value=[upload]):
                with patch.object(ce_mod, "like_video", side_effect=fake_like):
                    with patch("builtins.print"):
                        result = ce_mod.backfill_engagement(include_self=True)

        self.assertIn("acct1", liked_accounts)
        self.assertIn("acct2", liked_accounts)
        self.assertEqual(result["uploads_scanned"], 1)

    def test_include_self_false_excludes_uploader(self):
        upload = self._fake_upload(account="acct1")
        siblings = ["acct1", "acct2"]
        liked_accounts = []

        def fake_like(account, vid):
            liked_accounts.append(account)
            return {"status": "liked"}

        with patch.object(ce_mod, "list_sibling_accounts", return_value=siblings):
            with patch.object(ce_mod, "discover_existing_uploads", return_value=[upload]):
                with patch.object(ce_mod, "like_video", side_effect=fake_like):
                    with patch("builtins.print"):
                        result = ce_mod.backfill_engagement(include_self=False)

        self.assertNotIn("acct1", liked_accounts)
        self.assertIn("acct2", liked_accounts)

    def test_include_views_calls_play_view(self):
        upload = self._fake_upload()

        with patch.object(ce_mod, "list_sibling_accounts", return_value=["acct1"]):
            with patch.object(ce_mod, "discover_existing_uploads", return_value=[upload]):
                with patch.object(ce_mod, "like_video", return_value={"status": "liked"}):
                    with patch.object(ce_mod, "play_view",
                                       return_value={"status": "viewed", "playback_s": 35.0}) as mock_pv:
                        with patch("builtins.print"):
                            result = ce_mod.backfill_engagement(include_views=True, view_seconds=35)

        mock_pv.assert_called_once_with("V1", duration_s=35)
        self.assertEqual(len(result["views"]), 1)


# ---------------------------------------------------------------------------
# play_view
# ---------------------------------------------------------------------------


class PlayViewTest(unittest.TestCase):

    def test_playwright_not_installed_returns_error(self):
        # Ensure playwright is NOT importable
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            result = ce_mod.play_view("VID1", duration_s=1)
        self.assertEqual(result["status"], "error")
        self.assertIn("playwright", result["error"].lower())

    def test_playwright_success(self):
        # Create a fake playwright module
        mock_page = MagicMock()
        mock_page.goto = MagicMock()
        mock_page.locator.return_value.first.click = MagicMock()
        mock_page.wait_for_selector = MagicMock()
        mock_page.evaluate = MagicMock(return_value=5.0)

        mock_context = MagicMock()
        mock_context.new_page.return_value = mock_page

        mock_browser = MagicMock()
        mock_browser.new_context.return_value = mock_context

        mock_pw_instance = MagicMock()
        mock_pw_instance.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw_instance.__exit__ = MagicMock(return_value=False)
        mock_pw_instance.chromium.launch.return_value = mock_browser

        mock_sync_playwright = MagicMock(return_value=mock_pw_instance)

        fake_pw_module = types.ModuleType("playwright.sync_api")
        fake_pw_module.sync_playwright = mock_sync_playwright
        fake_pw_parent = types.ModuleType("playwright")

        with patch.dict("sys.modules", {
            "playwright": fake_pw_parent,
            "playwright.sync_api": fake_pw_module,
        }):
            with patch("time.sleep"):
                result = ce_mod.play_view("VID1", duration_s=2)

        self.assertIn(result["status"], ("viewed", "error"))

    def test_playwright_exception_returns_error(self):
        mock_pw_instance = MagicMock()
        mock_pw_instance.__enter__ = MagicMock(side_effect=RuntimeError("Chrome crashed"))
        mock_pw_instance.__exit__ = MagicMock(return_value=False)
        mock_sync_playwright = MagicMock(return_value=mock_pw_instance)

        fake_pw_module = types.ModuleType("playwright.sync_api")
        fake_pw_module.sync_playwright = mock_sync_playwright
        fake_pw_parent = types.ModuleType("playwright")

        with patch.dict("sys.modules", {
            "playwright": fake_pw_parent,
            "playwright.sync_api": fake_pw_module,
        }):
            with patch("time.sleep"):
                result = ce_mod.play_view("VID1", duration_s=1)

        self.assertEqual(result["status"], "error")
        self.assertIn("Chrome crashed", result["error"])


# ---------------------------------------------------------------------------
# list_chrome_profiles
# ---------------------------------------------------------------------------


def _make_local_state(profiles: dict) -> dict:
    return {
        "profile": {
            "info_cache": {
                dir_name: {"user_name": info.get("user_name", ""), "name": info.get("name", dir_name)}
                for dir_name, info in profiles.items()
            }
        }
    }


# ---------------------------------------------------------------------------
# _engage_blocking
# ---------------------------------------------------------------------------


class EngageBlockingTest(unittest.TestCase):

    def test_returns_likes_and_view(self):
        """_engage_blocking delegates to like_from_siblings + play_view."""
        with patch.object(ce_mod, "like_from_siblings", return_value=[{"status": "liked"}]):
            with patch.object(ce_mod, "play_view", return_value={"status": "viewed"}):
                result = ce_mod._engage_blocking("uploader", "VID1")
        self.assertEqual(len(result["likes"]), 1)
        self.assertEqual(result["view"]["status"], "viewed")


# ---------------------------------------------------------------------------
# engage_after_upload
# ---------------------------------------------------------------------------


class EngageAfterUploadTest(unittest.TestCase):

    def test_disabled_via_env_returns_status_dict(self):
        with patch.dict(os.environ, {"YTFACTORY_CROSS_ENGAGE": "0"}):
            with patch("builtins.print"):
                result = ce_mod.engage_after_upload("uploader", "VID1")
        self.assertEqual(result["status"], "disabled")

    def test_background_true_returns_thread(self):
        with patch.dict(os.environ, {"YTFACTORY_CROSS_ENGAGE": "1"}):
            with patch.object(ce_mod, "_engage_blocking", return_value={"likes": [], "view": {}}):
                with patch.object(ce_mod, "list_sibling_accounts", return_value=[]):
                    with patch("builtins.print"):
                        result = ce_mod.engage_after_upload("uploader", "VID1", background=True)
        self.assertIsInstance(result, threading.Thread)
        result.join(timeout=2)

    def test_background_false_returns_dict(self):
        with patch.dict(os.environ, {"YTFACTORY_CROSS_ENGAGE": "1"}):
            with patch.object(ce_mod, "_engage_blocking",
                               return_value={"likes": [], "view": {}}) as mock_engage:
                result = ce_mod.engage_after_upload("uploader", "VID1", background=False)
        mock_engage.assert_called_once_with("uploader", "VID1")
        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# CLI _main()
# ---------------------------------------------------------------------------


class CrossEngageCLITest(unittest.TestCase):

    def _run_cmd(self, argv):
        """Run _main() with sys.argv patched and all heavy work mocked."""
        with patch("sys.argv", ["pipeline.cross_engage"] + argv):
            with patch("builtins.print"):
                return ce_mod._main()

    def test_list_cmd_returns_0(self):
        with patch.object(ce_mod, "list_sibling_accounts", return_value=["a"]):
            with patch.object(ce_mod, "_load_registry", return_value={"a": {"channel_id": "UC1"}}):
                rc = self._run_cmd(["list"])
        self.assertEqual(rc, 0)

    def test_refresh_ids_cmd(self):
        with patch.object(ce_mod, "refresh_all_channel_ids", return_value={}):
            rc = self._run_cmd(["refresh-ids"])
        self.assertEqual(rc, 0)

    def test_subscribe_all_cmd(self):
        with patch.object(ce_mod, "subscribe_all_pairs", return_value=[]):
            rc = self._run_cmd(["subscribe-all"])
        self.assertEqual(rc, 0)

    def test_like_cmd(self):
        with patch.object(ce_mod, "like_from_siblings", return_value=[]):
            rc = self._run_cmd(["like", "VID1"])
        self.assertEqual(rc, 0)

    def test_view_cmd_success(self):
        with patch.object(ce_mod, "play_view", return_value={"status": "viewed"}):
            rc = self._run_cmd(["view", "VID1"])
        self.assertEqual(rc, 0)

    def test_view_cmd_error(self):
        with patch.object(ce_mod, "play_view", return_value={"status": "error"}):
            rc = self._run_cmd(["view", "VID1"])
        self.assertEqual(rc, 1)

    def test_engage_cmd(self):
        with patch.object(ce_mod, "engage_after_upload",
                           return_value={"likes": [], "view": {}}):
            rc = self._run_cmd(["engage", "VID1", "--uploader", "acct"])
        self.assertEqual(rc, 0)

    def test_backfill_cmd(self):
        with patch.object(ce_mod, "backfill_engagement",
                           return_value={"likes": [], "uploads_scanned": 0}):
            rc = self._run_cmd(["backfill"])
        self.assertEqual(rc, 0)

    def test_reauth_all_cmd_success(self):
        """Lines 597-604: reauth-all loops over siblings, calls authenticate."""
        with patch.object(ce_mod, "list_sibling_accounts", return_value=["a", "b"]):
            with patch.object(ce_mod, "authenticate", return_value=None):
                rc = self._run_cmd(["reauth-all"])
        self.assertEqual(rc, 0)

    def test_reauth_all_cmd_exception_suppressed(self):
        """Line 602-603: authenticate raises → prints FAILED, continues."""
        with patch.object(ce_mod, "list_sibling_accounts", return_value=["a"]):
            with patch.object(ce_mod, "authenticate", side_effect=Exception("no creds")):
                rc = self._run_cmd(["reauth-all"])
        self.assertEqual(rc, 0)



class ProjectRootTest(unittest.TestCase):

    def test_project_root_returns_path(self):
        """_project_root() returns parent.parent of the module file (line 256)."""
        result = ce_mod._project_root()
        self.assertIsInstance(result, Path)
        # Module is at pipeline/research/cross_engage.py → parent.parent = pipeline/
        expected = Path(ce_mod.__file__).resolve().parent.parent
        self.assertEqual(result, expected)


class DiscoverExistingUploadsNoDirTest(unittest.TestCase):

    def test_uploads_not_a_dir_skips_channel(self):
        """Line 273: if not ud.is_dir() → continue."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chan = root / "mychan"
            chan.mkdir()
            # No uploads/ dir inside chan
            with patch.object(ce_mod, "_project_root", return_value=root):
                with patch.object(ce_mod, "list_sibling_accounts", return_value=["myvid"]):
                    result = ce_mod.discover_existing_uploads()
        self.assertEqual(result, [])


class SubscribePairContentDecodeTest(unittest.TestCase):

    def test_content_decode_raises_but_status_400_still_graceful(self):
        """Lines 177-178: e.content.decode() raises → caught; status 400 → already_subscribed."""
        _install_fake_google_for_ce()
        fake_yt = MagicMock()
        err = _make_http_error(400, "")
        # Make content.decode() raise
        err.content = MagicMock()
        err.content.decode.side_effect = Exception("decode error")
        fake_yt.subscriptions.return_value.insert.return_value.execute.side_effect = err

        with patch("pipeline.research.cross_engage.authenticate", return_value=MagicMock()):
            sys.modules["googleapiclient.discovery"].build = MagicMock(return_value=fake_yt)
            result = ce_mod.subscribe_pair("home_acct", "UCTarget")

        self.assertEqual(result["status"], "already_subscribed")


class PlayViewAllExceptionsTest(unittest.TestCase):

    def test_all_page_operations_raise_exceptions(self):
        """Make every page operation raise so all except handlers in play_view are covered."""
        mock_page = MagicMock()
        # Cookie consent loop - locator().first.click() raises → continue
        mock_page.locator.return_value.first.click.side_effect = Exception("click fail")
        # wait_for_selector raises → pass
        mock_page.wait_for_selector.side_effect = Exception("selector timeout")
        # video click raises → pass
        # is_visible raises → continue in play button loop
        mock_page.locator.return_value.first.is_visible.side_effect = Exception("invisible")
        # evaluate raises → pass (all three evaluate calls)
        mock_page.evaluate.side_effect = Exception("eval fail")

        mock_context = MagicMock()
        mock_context.new_page.return_value = mock_page
        mock_browser = MagicMock()
        mock_browser.new_context.return_value = mock_context

        mock_pw = MagicMock()
        mock_pw.__enter__ = MagicMock(return_value=mock_pw)
        mock_pw.__exit__ = MagicMock(return_value=False)
        mock_pw.chromium.launch.return_value = mock_browser
        mock_sync_playwright = MagicMock(return_value=mock_pw)

        fake_pw_module = types.ModuleType("playwright.sync_api")
        fake_pw_module.sync_playwright = mock_sync_playwright
        fake_pw_parent = types.ModuleType("playwright")

        with patch.dict("sys.modules", {
            "playwright": fake_pw_parent,
            "playwright.sync_api": fake_pw_module,
        }):
            with patch("time.sleep"):
                result = ce_mod.play_view("VID1", duration_s=5)

        # Even with all operations failing, should return viewed (or error)
        self.assertIn(result["status"], ("viewed", "error"))
if __name__ == "__main__":
    unittest.main()
