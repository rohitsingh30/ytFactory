"""Tests for pipeline/research/channel_assets.py — avatar/banner mirror.

All HTTP calls and file writes against external paths are mocked.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.research import channel_assets as ca_mod


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_yt_payload(account: str, avatar_url: str | None, banner_url: str | None) -> dict:
    return {
        "account": account,
        "fetched_at": "2026-01-01T00:00:00Z",
        "channel": {
            "id": "UCtest",
            "title": "Test",
            "avatar_url": avatar_url,
            "banner_url": banner_url,
        },
    }


# ---------------------------------------------------------------------------
# _account_dir / _source_state_path
# ---------------------------------------------------------------------------


class AccountDirTest(unittest.TestCase):

    def test_account_dir_is_under_assets(self):
        with patch.object(ca_mod, "ASSETS_DIR", Path("/tmp/assets")):
            result = ca_mod._account_dir("myaccount")
        self.assertEqual(result, Path("/tmp/assets/myaccount"))

    def test_source_state_path(self):
        with patch.object(ca_mod, "ASSETS_DIR", Path("/tmp/assets")):
            result = ca_mod._source_state_path("myaccount")
        self.assertEqual(result.name, ".source.json")


# ---------------------------------------------------------------------------
# _load_source_state / _save_source_state
# ---------------------------------------------------------------------------


class SourceStateTest(unittest.TestCase):

    def test_load_returns_empty_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ca_mod, "ASSETS_DIR", Path(tmp)):
                result = ca_mod._load_source_state("acct")
        self.assertEqual(result, {})

    def test_load_returns_empty_on_bad_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "acct" / ".source.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{bad json}")
            with patch.object(ca_mod, "ASSETS_DIR", Path(tmp)):
                result = ca_mod._load_source_state("acct")
        self.assertEqual(result, {})

    def test_save_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ca_mod, "ASSETS_DIR", Path(tmp)):
                ca_mod._save_source_state("acct", {"avatar_url": "https://img.jpg"})
                result = ca_mod._load_source_state("acct")
        self.assertEqual(result["avatar_url"], "https://img.jpg")


# ---------------------------------------------------------------------------
# _download
# ---------------------------------------------------------------------------


class DownloadTest(unittest.TestCase):

    def test_successful_download_writes_file(self):
        chunk = b"FAKEIMAGE" * 100

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_content = MagicMock(return_value=[chunk])

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "avatar.jpg"
            with patch("requests.get", return_value=mock_resp):
                ok = ca_mod._download("https://example.com/img.jpg", dest)
            self.assertTrue(ok)
            self.assertTrue(dest.exists())

    def test_failed_download_returns_false(self):
        import requests as req
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "avatar.jpg"
            with patch("requests.get", side_effect=Exception("network error")):
                ok = ca_mod._download("https://example.com/img.jpg", dest)
        self.assertFalse(ok)
        self.assertFalse(dest.exists())

    def test_tmp_file_cleaned_up_on_failure(self):
        """A partial .tmp file should be removed after a download failure."""
        import requests as req
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "avatar.jpg"
            tmp_file = Path(str(dest) + ".tmp")
            with patch("requests.get", side_effect=OSError("disk full")):
                ca_mod._download("https://example.com/img.jpg", dest)
            # The .tmp should not exist after failure
            self.assertFalse(tmp_file.exists())
    def test_unlink_oserror_suppressed_on_failure(self):
        """If the .tmp cleanup itself raises OSError, it's silently suppressed (lines 95-96)."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "avatar.jpg"
            with patch("requests.get", side_effect=IOError("network")):
                with patch("pathlib.Path.unlink", side_effect=OSError("busy")):
                    ok = ca_mod._download("https://example.com/img.jpg", dest)
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# download_for_account
# ---------------------------------------------------------------------------


class DownloadForAccountTest(unittest.TestCase):

    def test_missing_cache_returns_false_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ca_mod, "YOUTUBE_DIR", Path(tmp) / "youtube"):
                with patch.object(ca_mod, "ASSETS_DIR", Path(tmp) / "assets"):
                    with patch("builtins.print"):
                        result = ca_mod.download_for_account("no_such_acct")
        self.assertEqual(result, {"avatar": False, "banner": False})

    def test_missing_cache_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ca_mod, "YOUTUBE_DIR", Path(tmp) / "youtube"):
                with patch.object(ca_mod, "ASSETS_DIR", Path(tmp) / "assets"):
                    result = ca_mod.download_for_account("no_such_acct", quiet=True)
        self.assertEqual(result, {"avatar": False, "banner": False})

    def test_unreadable_cache_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            (yt_dir / "acct.json").write_text("{bad json}")
            with patch.object(ca_mod, "YOUTUBE_DIR", yt_dir):
                with patch.object(ca_mod, "ASSETS_DIR", Path(tmp) / "assets"):
                    with patch("builtins.print"):
                        result = ca_mod.download_for_account("acct")
        self.assertEqual(result, {"avatar": False, "banner": False})

    def test_skips_download_when_cached_url_unchanged(self):
        """Same URL + existing file + non-zero size → skip download."""
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            assets_dir = Path(tmp) / "assets"
            avatar_url = "https://example.com/avatar.jpg"
            banner_url = "https://example.com/banner.jpg"

            payload = _make_yt_payload("acct", avatar_url, banner_url)
            (yt_dir / "acct.json").write_text(json.dumps(payload))

            # Pre-create the avatar + banner files
            acct_dir = assets_dir / "acct"
            acct_dir.mkdir(parents=True)
            avatar_dest = acct_dir / "avatar.jpg"
            banner_dest = acct_dir / "banner.jpg"
            avatar_dest.write_bytes(b"FAKE_AVATAR_DATA")
            banner_dest.write_bytes(b"FAKE_BANNER_DATA")

            # Pre-write state showing these URLs were already fetched
            state = {"avatar_url": avatar_url, "banner_url": banner_url}
            (acct_dir / ".source.json").write_text(json.dumps(state))

            with patch.object(ca_mod, "YOUTUBE_DIR", yt_dir):
                with patch.object(ca_mod, "ASSETS_DIR", assets_dir):
                    with patch("requests.get") as mock_get:
                        result = ca_mod.download_for_account("acct")

        # No HTTP requests should have been made (cached + same URL)
        mock_get.assert_not_called()
        self.assertEqual(result, {"avatar": True, "banner": True})

    def test_downloads_when_url_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            assets_dir = Path(tmp) / "assets"
            old_url = "https://example.com/old_avatar.jpg"
            new_url = "https://example.com/new_avatar.jpg"

            payload = _make_yt_payload("acct", new_url, None)
            (yt_dir / "acct.json").write_text(json.dumps(payload))

            acct_dir = assets_dir / "acct"
            acct_dir.mkdir(parents=True)
            avatar_dest = acct_dir / "avatar.jpg"
            avatar_dest.write_bytes(b"OLD_DATA")

            # State shows OLD url was fetched
            state = {"avatar_url": old_url}
            (acct_dir / ".source.json").write_text(json.dumps(state))

            # Mock download success
            with patch.object(ca_mod, "YOUTUBE_DIR", yt_dir):
                with patch.object(ca_mod, "ASSETS_DIR", assets_dir):
                    with patch.object(ca_mod, "_download", return_value=True) as mock_dl:
                        with patch("builtins.print"):
                            result = ca_mod.download_for_account("acct")

        mock_dl.assert_called_once()

    def test_no_url_leaves_stale_file_in_place(self):
        """When no URL is in the payload, keep any existing stale file."""
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            assets_dir = Path(tmp) / "assets"

            # No URLs in payload
            payload = _make_yt_payload("acct", None, None)
            (yt_dir / "acct.json").write_text(json.dumps(payload))

            # Pre-create a stale avatar
            acct_dir = assets_dir / "acct"
            acct_dir.mkdir(parents=True)
            (acct_dir / "avatar.jpg").write_bytes(b"STALE")

            with patch.object(ca_mod, "YOUTUBE_DIR", yt_dir):
                with patch.object(ca_mod, "ASSETS_DIR", assets_dir):
                    result = ca_mod.download_for_account("acct")

        # Stale file stays → avatar reported as True
        self.assertTrue(result["avatar"])
        self.assertFalse(result["banner"])

    def test_download_failure_reports_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            assets_dir = Path(tmp) / "assets"

            payload = _make_yt_payload("acct", "https://img.jpg", None)
            (yt_dir / "acct.json").write_text(json.dumps(payload))

            with patch.object(ca_mod, "YOUTUBE_DIR", yt_dir):
                with patch.object(ca_mod, "ASSETS_DIR", assets_dir):
                    with patch.object(ca_mod, "_download", return_value=False):
                        result = ca_mod.download_for_account("acct")

        self.assertFalse(result["avatar"])

    def test_saves_state_after_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            assets_dir = Path(tmp) / "assets"
            avatar_url = "https://example.com/avatar.jpg"

            payload = _make_yt_payload("acct", avatar_url, None)
            (yt_dir / "acct.json").write_text(json.dumps(payload))

            def fake_download(url, dest, **kw):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"DOWNLOADED_IMAGE")
                return True

            with patch.object(ca_mod, "YOUTUBE_DIR", yt_dir):
                with patch.object(ca_mod, "ASSETS_DIR", assets_dir):
                    with patch.object(ca_mod, "_download", side_effect=fake_download):
                        with patch("builtins.print"):
                            ca_mod.download_for_account("acct")

            state_path = assets_dir / "acct" / ".source.json"
            self.assertTrue(state_path.exists())
            state = json.loads(state_path.read_text())
            self.assertEqual(state.get("avatar_url"), avatar_url)


# ---------------------------------------------------------------------------
# download_for_accounts
# ---------------------------------------------------------------------------


class DownloadForAccountsTest(unittest.TestCase):

    def test_enumerates_from_youtube_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            assets_dir = Path(tmp) / "assets"
            (yt_dir / "acct1.json").write_text(json.dumps({"channel": {}}))
            (yt_dir / "acct2.json").write_text(json.dumps({"channel": {}}))

            with patch.object(ca_mod, "YOUTUBE_DIR", yt_dir):
                with patch.object(ca_mod, "ASSETS_DIR", assets_dir):
                    result = ca_mod.download_for_accounts(quiet=True)

        self.assertIn("acct1", result)
        self.assertIn("acct2", result)

    def test_explicit_account_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            yt_dir.mkdir()
            assets_dir = Path(tmp) / "assets"

            with patch.object(ca_mod, "YOUTUBE_DIR", yt_dir):
                with patch.object(ca_mod, "ASSETS_DIR", assets_dir):
                    result = ca_mod.download_for_accounts(["only_this"], quiet=True)

        self.assertIn("only_this", result)
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# asset_path
# ---------------------------------------------------------------------------


class AssetPathTest(unittest.TestCase):

    def test_invalid_kind_returns_none(self):
        result = ca_mod.asset_path("acct", "screenshot")
        self.assertIsNone(result)

    def test_returns_none_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ca_mod, "ASSETS_DIR", Path(tmp)):
                result = ca_mod.asset_path("acct", "avatar")
        self.assertIsNone(result)

    def test_returns_path_when_file_exists_and_non_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            acct_dir = Path(tmp) / "acct"
            acct_dir.mkdir()
            avatar = acct_dir / "avatar.jpg"
            avatar.write_bytes(b"IMAGEDATA")
            with patch.object(ca_mod, "ASSETS_DIR", Path(tmp)):
                result = ca_mod.asset_path("acct", "avatar")
        self.assertEqual(result, avatar)

    def test_returns_none_when_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            acct_dir = Path(tmp) / "acct"
            acct_dir.mkdir()
            avatar = acct_dir / "avatar.jpg"
            avatar.write_bytes(b"")
            with patch.object(ca_mod, "ASSETS_DIR", Path(tmp)):
                result = ca_mod.asset_path("acct", "avatar")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------


class ChannelAssetsCLITest(unittest.TestCase):

    def _run(self, argv, quiet=False):
        with patch("sys.argv", ["pipeline.research.channel_assets"] + argv):
            with patch.object(ca_mod, "YOUTUBE_DIR", Path("/nonexistent/__yt__")):
                with patch.object(ca_mod, "ASSETS_DIR", Path("/nonexistent/__assets__")):
                    with patch.object(ca_mod, "download_for_account",
                                       return_value={"avatar": False, "banner": False}):
                        with patch.object(ca_mod, "download_for_accounts",
                                           return_value={}):
                            ca_mod.main()

    def test_cli_all_accounts(self):
        self._run([])

    def test_cli_specific_account(self):
        self._run(["--account", "myacct"])

    def test_cli_quiet(self):
        import io
        with patch("sys.stdout", io.StringIO()) as mock_out:
            self._run(["--quiet"])
            output = mock_out.getvalue()
        # quiet mode prints JSON
        self.assertIn("{", output)

    def test_cli_quiet_with_account(self):
        import io
        with patch("sys.stdout", io.StringIO()) as mock_out:
            self._run(["--account", "acct", "--quiet"])
            output = mock_out.getvalue()
        self.assertIn("{", output)

    def test_cli_non_quiet_prints_per_account_status(self):
        with patch("sys.argv", ["pipeline.research.channel_assets"]):
            with patch.object(ca_mod, "YOUTUBE_DIR", Path("/nonexistent/__yt__")):
                with patch.object(ca_mod, "ASSETS_DIR", Path("/nonexistent/__assets__")):
                    # download_for_accounts returns one result
                    with patch.object(ca_mod, "download_for_accounts",
                                       return_value={"acct": {"avatar": True, "banner": False}}):
                        import io
                        with patch("sys.stdout", io.StringIO()) as mock_out:
                            ca_mod.main()
                            output = mock_out.getvalue()
        self.assertIn("avatar=True", output)


if __name__ == "__main__":
    unittest.main()
