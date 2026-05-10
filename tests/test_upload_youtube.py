"""Tests for pipeline/upload/upload.py — full branch coverage.

NO real YouTube API calls. All external dependencies mocked.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import sys
import tempfile
import types
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline.upload.upload as upload_mod
from pipeline.upload.upload import (
    SCOPES,
    UploadError,
    _cmd_auth_refresh,
    _cmd_auth_status,
    _discover_accounts,
    _ensure_critique,
    _find_cast_path_for_sidecar,
    _latest_publish_for_account,
    _mirror_record_to_gcs,
    _parse_iso,
    _part2_pending_path,
    _print_status_table,
    _record_path,
    _token_path,
    authenticate,
    compute_throttled_publish_at,
    derive_metadata,
    existing_upload,
    get_channel_sub_count,
    inspect_token_status,
    main,
    render_description,
    set_thumbnail,
    upload_short,
    write_part2_pending,
    write_upload_record,
    youtube_upload,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeResp:
    """Minimal response object for googleapiclient.errors.HttpError."""
    def __init__(self, status: int):
        self.status = status
        self.reason = "HTTP Error"


def _make_http_error(status: int):
    from googleapiclient.errors import HttpError
    return HttpError(resp=_FakeResp(status), content=b"error body")


def _make_fake_youtube(video_id="vid123"):
    """Build a minimal YouTube API service mock."""
    yt = MagicMock()
    # videos().insert(...).next_chunk() → (None, response_dict)
    insert_req = MagicMock()
    insert_req.next_chunk.return_value = (
        None,
        {"id": video_id, "kind": "youtube#video", "etag": "etag1"},
    )
    yt.videos.return_value.insert.return_value = insert_req
    # thumbnails().set(...).execute()
    yt.thumbnails.return_value.set.return_value.execute.return_value = {}
    # channels().list(...).execute()
    yt.channels.return_value.list.return_value.execute.return_value = {
        "items": [{"statistics": {"subscriberCount": "42000"}}]
    }
    return yt


def _write_token(path: Path, scopes=None, refresh_token="rtoken", token="atoken"):
    """Write a valid token JSON file."""
    path.write_text(json.dumps({
        "token": token,
        "refresh_token": refresh_token,
        "scopes": scopes if scopes is not None else list(SCOPES),
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "csecret",
    }))


# ---------------------------------------------------------------------------
# Base test class: sets up temp dirs + module patches
# ---------------------------------------------------------------------------

class _UploadTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.config_dir = self.tmpdir / "config"
        self.config_dir.mkdir()
        self.project_root = self.tmpdir / "project"
        self.project_root.mkdir()
        self.client_secret_file = self.config_dir / "client_secret.json"

        self._patches = [
            patch("pipeline.upload.upload.CONFIG_DIR", self.config_dir),
            patch("pipeline.upload.upload.CLIENT_SECRET_PATH", self.client_secret_file),
        ]
        for p in self._patches:
            p.start()

        # Disable GCS sync by default — individual tests opt-in
        os.environ["YTFACTORY_DASHBOARD_GCS_SYNC"] = "0"
        # Disable cross-engagement — these tests mock the upload path
        # but cross_engage spins a daemon thread that tries to
        # authenticate against real OAuth credentials and surfaces the
        # failure as PytestUnhandledThreadException. Engagement is
        # tested in its own dedicated test file.
        os.environ["YTFACTORY_CROSS_ENGAGE"] = "0"

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        os.environ.pop("YTFACTORY_DASHBOARD_GCS_SYNC", None)
        os.environ.pop("YTFACTORY_OAUTH_PORT", None)
        os.environ.pop("YTFACTORY_CROSS_ENGAGE", None)

    # -- convenience helpers ------------------------------------------------

    def _mk_channel(self, name="testchan", *, account="default") -> Path:
        d = self.project_root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(f"name: {name}\nupload:\n  account: {account}\n")
        return d

    def _mk_mp4(self, name="test.mp4") -> Path:
        p = self.tmpdir / name
        p.write_bytes(b"\x00" * 1024)
        return p

    def _mk_token(self, account="default", **kw) -> Path:
        p = self.config_dir / f"youtube_token_{account}.json"
        _write_token(p, **kw)
        return p


# ===========================================================================
# 1.  CLIENT_SECRET_PATH / _token_path
# ===========================================================================

class TestTokenAndSecretPaths(_UploadTestBase):
    def test_client_secret_path_constant(self):
        """CLIENT_SECRET_PATH == CONFIG_DIR / 'client_secret.json'."""
        from pipeline.upload.upload import CONFIG_DIR, CLIENT_SECRET_PATH
        self.assertEqual(CLIENT_SECRET_PATH, CONFIG_DIR / "client_secret.json")

    def test_token_path_fallback(self):
        result = _token_path("default")
        self.assertEqual(result, self.config_dir / "youtube_token_default.json")

    def test_token_path_safe_chars(self):
        """Non-alphanum chars (except - _) are stripped from account name."""
        result = _token_path("my@account!name")
        self.assertIn("myaccountname", str(result))

    def test_token_path_empty_becomes_default(self):
        """If all chars are stripped, 'default' is used."""
        result = _token_path("!@#")
        self.assertIn("youtube_token_default", str(result))

    def test_record_path_delegates_to_render_paths(self):
        """_record_path routes through RenderPaths."""
        self._mk_channel("chan1")
        p = _record_path(self.project_root, "chan1", "slug1")
        self.assertTrue(str(p).endswith("uploads/slug1.json"))

    def test_secret_mount_preferred_when_present(self):
        """Cloud Run mounts each secret at /secrets/<name>/value. When
        that file exists, _token_path picks it over CONFIG_DIR. (D3 prep
        for the token-issue permanent fix.)"""
        from pipeline.upload.upload import _secret_mount_path

        sp = _secret_mount_path("default")
        # Write into the secret mount path inside our tmp dir to simulate
        # the Cloud Run mount being present.
        fake_mount = self.tmpdir / "fake_secrets" / "youtube-token-default" / "value"
        fake_mount.parent.mkdir(parents=True)
        fake_mount.write_text('{"token": "x"}')
        with patch("pipeline.upload.upload.SECRETS_ROOT", self.tmpdir / "fake_secrets"):
            from pipeline.upload.upload import _token_path as _tp
            result = _tp("default")
            self.assertEqual(result, fake_mount)


# ===========================================================================
# _persist_token — laptop FS write vs Cloud Run Secret Manager add_version (D1)
# ===========================================================================


class TestPersistToken(_UploadTestBase):
    """Coverage for the FS-vs-SecretManager dispatch in _persist_token."""

    def test_laptop_path_writes_file(self):
        from pipeline.upload.upload import _persist_token, _token_path

        tp = _token_path("acct1")
        _persist_token("acct1", '{"token": "xyz"}', tp)
        self.assertTrue(tp.exists())
        self.assertEqual(tp.read_text(), '{"token": "xyz"}')

    def test_secret_mount_path_calls_add_secret_version(self):
        """When the active path is a Secret Manager mount, write a new
        version of the underlying secret instead of touching the read-
        only mount file."""
        from pipeline.upload.upload import _persist_token

        # Simulate a secret mount path.
        fake_root = self.tmpdir / "secrets"
        secret_dir = fake_root / "youtube-token-acct1"
        secret_dir.mkdir(parents=True)
        mount_path = secret_dir / "value"
        mount_path.write_text("{}")

        # Patch the secret manager client surface.
        fake_client = MagicMock()
        fake_secretmanager = types.ModuleType("google.cloud.secretmanager")
        fake_secretmanager.SecretManagerServiceClient = lambda: fake_client

        with patch.dict(sys.modules, {
                "google.cloud.secretmanager": fake_secretmanager,
        }):
            with patch("pipeline.upload.upload.SECRETS_ROOT", fake_root):
                with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "ytfactory-test"}):
                    _persist_token("acct1", '{"token": "rotated"}', mount_path)

        fake_client.add_secret_version.assert_called_once()
        kwargs = fake_client.add_secret_version.call_args.kwargs or {}
        req = (kwargs.get("request")
               or fake_client.add_secret_version.call_args.args[0])
        self.assertEqual(req["parent"],
                         "projects/ytfactory-test/secrets/youtube-token-acct1")
        self.assertEqual(req["payload"]["data"], b'{"token": "rotated"}')

    def test_secret_manager_failure_raises(self):
        """add_secret_version errors propagate so the caller can route
        them to the token-health endpoint / alert. NEVER swallow — a
        silent failure means the next cold-start re-discovers the same
        bad token."""
        from pipeline.upload.upload import _persist_token

        fake_root = self.tmpdir / "secrets"
        secret_dir = fake_root / "youtube-token-acct1"
        secret_dir.mkdir(parents=True)
        mount_path = secret_dir / "value"
        mount_path.write_text("{}")

        fake_client = MagicMock()
        fake_client.add_secret_version.side_effect = Exception("PERMISSION_DENIED")
        fake_sm = types.ModuleType("google.cloud.secretmanager")
        fake_sm.SecretManagerServiceClient = lambda: fake_client

        with patch.dict(sys.modules, {"google.cloud.secretmanager": fake_sm}):
            with patch("pipeline.upload.upload.SECRETS_ROOT", fake_root):
                with self.assertRaises(RuntimeError) as cm:
                    _persist_token("acct1", "{}", mount_path)
                self.assertIn("grant_token_writeback.sh", str(cm.exception))


# ===========================================================================
# 2.  _parse_iso
# ===========================================================================

class TestParseIso(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_parse_iso(None))

    def test_z_suffix(self):
        dt = _parse_iso("2026-05-01T10:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_no_timezone_adds_utc(self):
        dt = _parse_iso("2026-05-01T10:00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_offset_preserved(self):
        dt = _parse_iso("2026-05-01T10:00:00+05:30")
        self.assertIsNotNone(dt)

    def test_invalid_returns_none(self):
        self.assertIsNone(_parse_iso("not-a-date"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_iso(""))


# ===========================================================================
# 3.  _latest_publish_for_account / compute_throttled_publish_at
# ===========================================================================

class TestPublishThrottle(_UploadTestBase):

    def _write_record(self, chan_dir: Path, slug: str, record: dict):
        uploads = chan_dir / "uploads"
        uploads.mkdir(exist_ok=True)
        (uploads / f"{slug}.json").write_text(json.dumps(record))

    def test_empty_project_root_returns_none(self):
        self.assertIsNone(_latest_publish_for_account(self.project_root, "default"))

    def test_skips_non_channel_dirs(self):
        """Dirs without config.yaml are skipped."""
        (self.project_root / "nonchan").mkdir()
        self.assertIsNone(_latest_publish_for_account(self.project_root, "default"))

    def test_skips_x_json_files(self):
        """*.x.json files (X-platform) are excluded."""
        chan = self._mk_channel()
        (chan / "uploads").mkdir()
        (chan / "uploads" / "slug1.x.json").write_text(
            json.dumps({"account": "default", "uploaded_at": "2026-01-01T00:00:00Z"})
        )
        self.assertIsNone(_latest_publish_for_account(self.project_root, "default"))

    def test_skips_bad_json(self):
        chan = self._mk_channel()
        (chan / "uploads").mkdir()
        (chan / "uploads" / "bad.json").write_text("not json{{{")
        self.assertIsNone(_latest_publish_for_account(self.project_root, "default"))

    def test_skips_wrong_account(self):
        chan = self._mk_channel(account="other")
        self._write_record(chan, "slug1", {"account": "other", "uploaded_at": "2026-01-01T00:00:00Z"})
        self.assertIsNone(_latest_publish_for_account(self.project_root, "default"))

    def test_uses_publish_at_over_uploaded_at(self):
        chan = self._mk_channel()
        self._write_record(chan, "slug1", {
            "account": "default",
            "uploaded_at": "2026-01-01T00:00:00Z",
            "publish_at": "2026-06-01T12:00:00Z",
        })
        result = _latest_publish_for_account(self.project_root, "default")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 6)

    def test_uses_uploaded_at_when_no_publish_at(self):
        chan = self._mk_channel()
        self._write_record(chan, "slug1", {
            "account": "default",
            "uploaded_at": "2026-05-10T08:00:00Z",
        })
        result = _latest_publish_for_account(self.project_root, "default")
        self.assertIsNotNone(result)
        self.assertEqual(result.month, 5)

    def test_record_with_no_timestamps_skipped(self):
        chan = self._mk_channel()
        self._write_record(chan, "slug1", {"account": "default"})
        self.assertIsNone(_latest_publish_for_account(self.project_root, "default"))

    def test_returns_maximum_across_multiple(self):
        chan = self._mk_channel()
        self._write_record(chan, "early", {"account": "default", "uploaded_at": "2026-01-01T00:00:00Z"})
        self._write_record(chan, "late", {"account": "default", "uploaded_at": "2026-12-31T23:59:59Z"})
        result = _latest_publish_for_account(self.project_root, "default")
        self.assertEqual(result.month, 12)

    def test_compute_throttled_no_latest_returns_none(self):
        result = compute_throttled_publish_at(self.project_root, "default")
        self.assertIsNone(result)

    def test_compute_throttled_within_gap(self):
        """Recent upload → returns a future publish slot."""
        now = datetime.now(timezone.utc)
        chan = self._mk_channel()
        self._write_record(chan, "s1", {
            "account": "default",
            "uploaded_at": now.isoformat(),
        })
        result = compute_throttled_publish_at(self.project_root, "default", now=now)
        self.assertIsNotNone(result)
        self.assertIn("Z", result)

    def test_compute_throttled_outside_gap_returns_none(self):
        """Old upload → no throttle needed."""
        old = datetime.now(timezone.utc) - timedelta(hours=5)
        chan = self._mk_channel()
        self._write_record(chan, "s1", {
            "account": "default",
            "uploaded_at": old.isoformat(),
        })
        result = compute_throttled_publish_at(self.project_root, "default")
        self.assertIsNone(result)


# ===========================================================================
# 4.  inspect_token_status
# ===========================================================================

class TestInspectTokenStatus(_UploadTestBase):
    def test_missing_file(self):
        result = inspect_token_status("default")
        self.assertEqual(result["state"], "missing")

    def test_unreadable_file(self):
        p = self.config_dir / "youtube_token_default.json"
        p.write_text("{{invalid json")
        result = inspect_token_status("default")
        self.assertEqual(result["state"], "unreadable")

    def test_missing_scopes(self):
        p = self.config_dir / "youtube_token_default.json"
        p.write_text(json.dumps({
            "token": "tok",
            "refresh_token": "rt",
            "scopes": ["https://www.googleapis.com/auth/youtube.readonly"],
        }))
        result = inspect_token_status("default")
        self.assertEqual(result["state"], "missing_scopes")
        self.assertIn("missing", result)

    def test_no_refresh_token(self):
        p = self._mk_token(refresh_token=None)
        # Write manually without refresh_token
        p.write_text(json.dumps({
            "token": "tok",
            "scopes": list(SCOPES),
            "expiry": "2025-01-01T00:00:00Z",
        }))
        result = inspect_token_status("default")
        self.assertEqual(result["state"], "no_refresh_token")

    def test_ok(self):
        self._mk_token()
        result = inspect_token_status("default")
        self.assertEqual(result["state"], "ok")

    def test_no_scopes_in_file_is_ok(self):
        """Empty scopes list → treated as ok (no scope drift check)."""
        p = self.config_dir / "youtube_token_default.json"
        p.write_text(json.dumps({"token": "tok", "refresh_token": "rt", "scopes": []}))
        result = inspect_token_status("default")
        self.assertEqual(result["state"], "ok")


# ===========================================================================
# 5.  authenticate
# ===========================================================================

class TestAuthenticate(_UploadTestBase):
    """Tests for authenticate() — all branches."""

    def _patch_google(self, mock_creds_cls=None, mock_request_cls=None, mock_flow_cls=None):
        patches = []
        if mock_creds_cls is not None:
            patches.append(patch("google.oauth2.credentials.Credentials", mock_creds_cls))
        if mock_request_cls is not None:
            patches.append(patch("google.auth.transport.requests.Request", mock_request_cls))
        if mock_flow_cls is not None:
            patches.append(patch("google_auth_oauthlib.flow.InstalledAppFlow", mock_flow_cls))
        return patches

    def test_missing_client_secret_raises(self):
        """No client_secret.json → UploadError."""
        with self.assertRaises(UploadError):
            authenticate("default", interactive=False)

    def test_valid_creds_from_file(self):
        """Token file with valid creds → returned immediately."""
        self.client_secret_file.touch()
        self._mk_token()

        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds_cls = MagicMock()
        mock_creds_cls.from_authorized_user_file.return_value = mock_creds

        with patch("google.oauth2.credentials.Credentials", mock_creds_cls):
            result = authenticate("default", interactive=False)
        self.assertIs(result, mock_creds)

    def test_missing_scopes_falls_through_to_non_interactive_error(self):
        """Token has wrong scopes → creds not set → non-interactive raises."""
        self.client_secret_file.touch()
        p = self.config_dir / "youtube_token_default.json"
        p.write_text(json.dumps({
            "token": "tok",
            "refresh_token": "rt",
            "scopes": ["https://www.googleapis.com/auth/youtube.readonly"],
        }))
        with self.assertRaises(UploadError):
            authenticate("default", interactive=False)

    def test_token_file_unreadable_falls_through(self):
        """Bad JSON in token → fallback to re-auth path."""
        self.client_secret_file.touch()
        p = self.config_dir / "youtube_token_default.json"
        p.write_text("{{INVALID")

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = False
        mock_creds.refresh_token = None
        mock_creds_cls = MagicMock()
        mock_creds_cls.from_authorized_user_file.return_value = mock_creds

        with patch("google.oauth2.credentials.Credentials", mock_creds_cls):
            with self.assertRaises(UploadError):
                authenticate("default", interactive=False)

    def test_expired_creds_refresh_success_writes_token(self):
        """Expired token + refresh_token → refresh and write back."""
        self.client_secret_file.touch()
        self._mk_token()

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "rtoken"
        mock_creds.to_json.return_value = json.dumps({"token": "new"})
        mock_creds_cls = MagicMock()
        mock_creds_cls.from_authorized_user_file.return_value = mock_creds

        with patch("google.oauth2.credentials.Credentials", mock_creds_cls):
            with patch("google.auth.transport.requests.Request", MagicMock()):
                result = authenticate("default", interactive=False)
        self.assertIs(result, mock_creds)
        mock_creds.refresh.assert_called_once()

    def test_expired_creds_token_write_fails_returns_creds_anyway(self):
        """Refresh OK but write-back raises → log warning, RETURN creds.

        Behaviour change from D1 (2026-05-10 token-issue plan): the
        in-memory creds are valid for ~1 hour regardless of whether we
        could persist them, so let the upload proceed instead of breaking
        on a transient disk / Secret Manager failure.
        """
        self.client_secret_file.touch()
        self._mk_token()

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "rtoken"
        mock_creds.to_json.return_value = "{}"
        mock_creds_cls = MagicMock()
        mock_creds_cls.from_authorized_user_file.return_value = mock_creds

        def _raise(*a, **kw):
            raise OSError("disk full")

        with patch("google.oauth2.credentials.Credentials", mock_creds_cls):
            with patch("google.auth.transport.requests.Request", MagicMock()):
                with patch.object(Path, "write_text", side_effect=_raise):
                    result = authenticate("default", interactive=False)
        self.assertIs(result, mock_creds)

    def test_refresh_fails_falls_through_to_error(self):
        """Token refresh exception → non-interactive raises UploadError."""
        self.client_secret_file.touch()
        self._mk_token()

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "rtoken"
        mock_creds.refresh.side_effect = Exception("network error")
        mock_creds_cls = MagicMock()
        mock_creds_cls.from_authorized_user_file.return_value = mock_creds

        with patch("google.oauth2.credentials.Credentials", mock_creds_cls):
            with patch("google.auth.transport.requests.Request", MagicMock()):
                with self.assertRaises(UploadError):
                    authenticate("default", interactive=False)

    def test_from_authorized_user_file_fails_manual_creds_no_refresh(self):
        """from_authorized_user_file fails → manual Credentials, no refresh_token → warns."""
        self.client_secret_file.touch()
        self._mk_token(refresh_token=None)

        mock_creds_cls = MagicMock()
        mock_creds_cls.from_authorized_user_file.side_effect = Exception("no refresh_token field")
        manual = MagicMock()
        manual.refresh_token = None
        manual.valid = False
        manual.expired = False
        mock_creds_cls.return_value = manual

        with patch("google.oauth2.credentials.Credentials", mock_creds_cls):
            # Cloud-side (interactive=False) MUST raise RefreshTokenLost
            # so the token-health endpoint / alert fires instead of the
            # silent all-nulls degrade. See plan-D2.
            from pipeline.upload.upload import RefreshTokenLost
            with self.assertRaises(RefreshTokenLost) as cm:
                authenticate("default", interactive=False)
            self.assertEqual(cm.exception.account, "default")
            self.assertIn("default", str(cm.exception))
            self.assertIn("Re-OAuth", str(cm.exception))
            with self.assertRaises(UploadError):
                authenticate("default", interactive=False)

    def test_from_authorized_user_file_fails_and_manual_also_fails(self):
        """Both from_authorized_user_file and manual Credentials fail → creds=None."""
        self.client_secret_file.touch()
        self._mk_token()

        mock_creds_cls = MagicMock()
        mock_creds_cls.from_authorized_user_file.side_effect = Exception("first fail")
        mock_creds_cls.side_effect = Exception("second fail")

        with patch("google.oauth2.credentials.Credentials", mock_creds_cls):
            with self.assertRaises(UploadError):
                authenticate("default", interactive=False)

    def test_interactive_oauth_with_refresh_token(self):
        """Interactive flow returns creds with refresh_token → write + return."""
        self.client_secret_file.write_text(json.dumps({"installed": {}}))

        mock_creds = MagicMock()
        mock_creds.refresh_token = "new_rt"
        mock_creds.to_json.return_value = json.dumps({"token": "t", "refresh_token": "new_rt"})
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds
        mock_flow_cls = MagicMock()
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow

        with patch("google_auth_oauthlib.flow.InstalledAppFlow", mock_flow_cls):
            with patch("google.oauth2.credentials.Credentials", MagicMock()):
                result = authenticate("default", interactive=True)
        self.assertIs(result, mock_creds)
        token_file = self.config_dir / "youtube_token_default.json"
        self.assertTrue(token_file.exists())

    def test_interactive_oauth_splices_prior_refresh_token(self):
        """OAuth returns no refresh_token but prior token exists → splice."""
        self.client_secret_file.write_text(json.dumps({"installed": {}}))
        # Write prior token with a refresh_token
        self._mk_token(refresh_token="prior_rt")

        mock_creds = MagicMock()
        mock_creds.refresh_token = None  # OAuth omitted it
        mock_creds.to_json.return_value = json.dumps({"token": "t"})
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds
        mock_flow_cls = MagicMock()
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow

        # from_authorized_user_file will succeed and return valid-ish mock creds
        # but then expired+no refresh_token path won't refresh → falls to interactive
        mock_creds_cls = MagicMock()
        stale_creds = MagicMock()
        stale_creds.valid = False
        stale_creds.expired = False
        stale_creds.refresh_token = None
        mock_creds_cls.from_authorized_user_file.return_value = stale_creds

        with patch("google.oauth2.credentials.Credentials", mock_creds_cls):
            with patch("google_auth_oauthlib.flow.InstalledAppFlow", mock_flow_cls):
                result = authenticate("default", interactive=True)

        self.assertIs(result, mock_creds)
        # prior_rt was spliced in
        self.assertEqual(mock_creds.refresh_token, "prior_rt")

    def test_interactive_oauth_no_refresh_token_no_prior_raises(self):
        """OAuth returns no refresh_token and there's no prior → UploadError."""
        self.client_secret_file.write_text(json.dumps({"installed": {}}))
        # No prior token file

        mock_creds = MagicMock()
        mock_creds.refresh_token = None
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds
        mock_flow_cls = MagicMock()
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow

        with patch("google.oauth2.credentials.Credentials", MagicMock()):
            with patch("google_auth_oauthlib.flow.InstalledAppFlow", mock_flow_cls):
                with self.assertRaises(UploadError):
                    authenticate("default", interactive=True)

    def test_interactive_oauth_default_opens_browser(self):
        """Default behaviour (YTFACTORY_OAUTH_OPEN_BROWSER unset) calls
        run_local_server with open_browser=True so the OAuth URL lands
        in the user's signed-in default Chrome (the fix for "auth
        flow doesn't use my Chrome profile")."""
        self.client_secret_file.write_text(json.dumps({"installed": {}}))
        mock_creds = MagicMock()
        mock_creds.refresh_token = "rt"
        mock_creds.to_json.return_value = json.dumps({"token": "t", "refresh_token": "rt"})
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds
        mock_flow_cls = MagicMock()
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow

        # Make sure the env-var override is NOT set (autouse fixtures
        # might leak from elsewhere).
        with patch.dict(os.environ, {}, clear=False):
            for k in ("YTFACTORY_OAUTH_OPEN_BROWSER",
                      "YTFACTORY_CHROME_PROFILE_DIR"):
                os.environ.pop(k, None)
            with patch("google.oauth2.credentials.Credentials", MagicMock()):
                with patch("google_auth_oauthlib.flow.InstalledAppFlow", mock_flow_cls):
                    authenticate("default", interactive=True)
        kwargs = mock_flow.run_local_server.call_args.kwargs
        self.assertTrue(kwargs.get("open_browser"),
                        "OAuth flow MUST open a browser by default — "
                        "this is the regression fix for 'doesn't use "
                        "my signed-in Chrome profile'.")

    def test_interactive_oauth_explicit_disable_does_not_open(self):
        """YTFACTORY_OAUTH_OPEN_BROWSER=0 keeps the headless behaviour."""
        self.client_secret_file.write_text(json.dumps({"installed": {}}))
        mock_creds = MagicMock()
        mock_creds.refresh_token = "rt"
        mock_creds.to_json.return_value = json.dumps({"token": "t", "refresh_token": "rt"})
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds
        mock_flow_cls = MagicMock()
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow

        with patch.dict(os.environ, {"YTFACTORY_OAUTH_OPEN_BROWSER": "0"}):
            with patch("google.oauth2.credentials.Credentials", MagicMock()):
                with patch("google_auth_oauthlib.flow.InstalledAppFlow", mock_flow_cls):
                    authenticate("default", interactive=True)
        kwargs = mock_flow.run_local_server.call_args.kwargs
        self.assertFalse(kwargs.get("open_browser"),
                         "OAuth flow MUST NOT open a browser when env=0.")

    def test_interactive_oauth_chrome_profile_uses_chrome_helper(self):
        """YTFACTORY_CHROME_PROFILE_DIR=<path> routes through the
        Chrome-subprocess helper instead of the system default
        browser, so multi-account users can pin a specific profile."""
        self.client_secret_file.write_text(json.dumps({"installed": {}}))
        mock_creds = MagicMock()
        mock_creds.refresh_token = "rt"
        mock_creds.to_json.return_value = json.dumps({"token": "t", "refresh_token": "rt"})

        # Profile dir must EXIST otherwise upload.authenticate falls back
        # to the default-browser path.
        profile_dir = self.tmpdir / "fake-chrome-profile"
        profile_dir.mkdir()

        mock_flow = MagicMock()
        mock_flow_cls = MagicMock()
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow

        with patch.dict(os.environ,
                        {"YTFACTORY_CHROME_PROFILE_DIR": str(profile_dir)}):
            with patch("google.oauth2.credentials.Credentials", MagicMock()):
                with patch("google_auth_oauthlib.flow.InstalledAppFlow", mock_flow_cls):
                    with patch("pipeline.upload.upload._run_local_server_with_chrome_profile",
                               return_value=mock_creds) as helper:
                        authenticate("default", interactive=True)
        helper.assert_called_once()
        self.assertEqual(helper.call_args.kwargs["chrome_profile_dir"],
                         str(profile_dir))

    def test_interactive_oauth_stdout_reconfigure_exception_ignored(self):
        """sys.stdout.reconfigure raising is silently caught (lines 344-345)."""
        self.client_secret_file.write_text(json.dumps({"installed": {}}))
        mock_creds = MagicMock()
        mock_creds.refresh_token = "rt"
        mock_creds.to_json.return_value = json.dumps({"token": "t", "refresh_token": "rt"})
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds
        mock_flow_cls = MagicMock()
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow

        import sys
        original_reconfigure = getattr(sys.stdout, "reconfigure", None)
        try:
            sys.stdout.reconfigure = lambda **kw: (_ for _ in ()).throw(AttributeError("mock"))
            with patch("google.oauth2.credentials.Credentials", MagicMock()):
                with patch("google_auth_oauthlib.flow.InstalledAppFlow", mock_flow_cls):
                    result = authenticate("default", interactive=True)
            self.assertIs(result, mock_creds)
        finally:
            if original_reconfigure is None:
                try:
                    del sys.stdout.reconfigure
                except AttributeError:
                    pass
            else:
                sys.stdout.reconfigure = original_reconfigure

    def test_non_interactive_no_creds_raises(self):
        """No token file, non-interactive → UploadError."""
        self.client_secret_file.touch()
        with self.assertRaises(UploadError):
            authenticate("default", interactive=False)


# ===========================================================================
# 6.  render_description
# ===========================================================================

class TestRenderDescription(unittest.TestCase):
    def test_script_substitution(self):
        result = render_description("{script.hook}", script={"hook": "My Hook"}, raw=None)
        self.assertEqual(result, "My Hook")

    def test_raw_substitution(self):
        result = render_description("{raw.url}", script={}, raw={"url": "https://example.com"})
        self.assertEqual(result, "https://example.com")

    def test_missing_key_is_empty_string(self):
        result = render_description("{script.missing}", script={}, raw=None)
        self.assertEqual(result, "")

    def test_list_access(self):
        result = render_description("{script.opts.0}", script={"opts": ["first", "second"]}, raw=None)
        self.assertEqual(result, "first")

    def test_list_index_error(self):
        result = render_description("{script.opts.99}", script={"opts": ["a"]}, raw=None)
        self.assertEqual(result, "")

    def test_list_non_int_key(self):
        result = render_description("{script.opts.notanint}", script={"opts": ["a"]}, raw=None)
        self.assertEqual(result, "")

    def test_unknown_root_is_empty_string(self):
        result = render_description("{other.key}", script={}, raw=None)
        self.assertEqual(result, "")

    def test_none_value_is_empty_string(self):
        result = render_description("{script.hook}", script={"hook": None}, raw=None)
        self.assertEqual(result, "")

    def test_raw_none_treated_as_empty_dict(self):
        result = render_description("{raw.anything}", script={}, raw=None)
        self.assertEqual(result, "")

    def test_traverse_into_non_dict_non_list_returns_empty(self):
        """If we try to go deeper into a scalar, return empty string."""
        result = render_description("{script.x.y}", script={"x": "scalar"}, raw=None)
        self.assertEqual(result, "")


# ===========================================================================
# 7.  derive_metadata
# ===========================================================================

class TestDeriveMetadata(unittest.TestCase):
    def test_title_from_title_options(self):
        meta = derive_metadata(
            script={"title_options": ["First Title", "Second Title"]},
            raw=None, upload_cfg={},
        )
        self.assertEqual(meta["title"], "First Title")

    def test_title_from_hook_fallback(self):
        meta = derive_metadata(script={"hook": "My Hook"}, raw=None, upload_cfg={})
        self.assertEqual(meta["title"], "My Hook")

    def test_title_from_slug_fallback(self):
        meta = derive_metadata(script={"slug": "my-slug"}, raw=None, upload_cfg={})
        self.assertEqual(meta["title"], "my-slug")

    def test_title_from_override(self):
        meta = derive_metadata(
            script={}, raw=None, upload_cfg={},
            title_override="Override Title",
        )
        self.assertEqual(meta["title"], "Override Title")

    def test_title_capped_at_100_chars(self):
        long_title = "A" * 200
        meta = derive_metadata(script={}, raw=None, upload_cfg={}, title_override=long_title)
        self.assertEqual(len(meta["title"]), 100)

    def test_description_from_template(self):
        meta = derive_metadata(
            script={}, raw={"url": "http://reddit.com/r/aita/123"},
            upload_cfg={"description_template": "Source: {raw.url}"},
        )
        self.assertIn("http://reddit.com", meta["description"])

    def test_description_from_override(self):
        meta = derive_metadata(
            script={}, raw=None, upload_cfg={},
            description_override="Custom description",
        )
        self.assertEqual(meta["description"], "Custom description")

    def test_description_capped_at_5000_chars(self):
        meta = derive_metadata(
            script={}, raw=None, upload_cfg={},
            description_override="X" * 10000,
        )
        self.assertEqual(len(meta["description"]), 5000)

    def test_bad_privacy_raises(self):
        with self.assertRaises(UploadError):
            derive_metadata(script={}, raw=None, upload_cfg={"privacy": "badval"})

    def test_all_fields(self):
        meta = derive_metadata(
            script={}, raw=None,
            upload_cfg={
                "privacy": "public",
                "made_for_kids": True,
                "category_id": "22",
                "tags": ["a", "b"],
            },
        )
        self.assertEqual(meta["privacy"], "public")
        self.assertTrue(meta["made_for_kids"])
        self.assertEqual(meta["category_id"], "22")
        self.assertEqual(meta["tags"], ["a", "b"])

    def test_defaults(self):
        meta = derive_metadata(script={}, raw=None, upload_cfg={})
        self.assertEqual(meta["privacy"], "private")
        self.assertFalse(meta["made_for_kids"])
        self.assertEqual(meta["category_id"], "24")


# ===========================================================================
# 8.  set_thumbnail
# ===========================================================================

class TestSetThumbnail(_UploadTestBase):

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_success(self, mock_build, mock_media, mock_auth):
        thumb = self.tmpdir / "thumb.jpg"
        thumb.write_bytes(b"\xff\xd8" + b"\x00" * 100)
        mock_build.return_value.thumbnails.return_value.set.return_value.execute.return_value = {}
        result = set_thumbnail("vid1", thumb, account="default")
        self.assertEqual(result["video_id"], "vid1")

    def test_not_found_raises(self):
        with self.assertRaises(UploadError):
            set_thumbnail("vid1", self.tmpdir / "missing.jpg")

    def test_too_large_raises(self):
        thumb = self.tmpdir / "big.jpg"
        thumb.write_bytes(b"\x00" * (3 * 1024 * 1024))  # 3 MB
        with self.assertRaises(UploadError):
            set_thumbnail("vid1", thumb)

    def test_bad_extension_raises(self):
        thumb = self.tmpdir / "file.gif"
        thumb.write_bytes(b"\x00" * 100)
        with self.assertRaises(UploadError):
            set_thumbnail("vid1", thumb)

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_http_error_raises_upload_error(self, mock_build, mock_media, mock_auth):
        thumb = self.tmpdir / "thumb.png"
        thumb.write_bytes(b"\x89PNG" + b"\x00" * 100)
        from googleapiclient.errors import HttpError
        mock_build.return_value.thumbnails.return_value.set.return_value.execute.side_effect = (
            _make_http_error(403)
        )
        with self.assertRaises(UploadError):
            set_thumbnail("vid1", thumb)


# ===========================================================================
# 9.  youtube_upload
# ===========================================================================

class TestYoutubeUpload(_UploadTestBase):

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_mp4_not_found(self, mock_build, mock_media, mock_auth):
        with self.assertRaises(UploadError):
            youtube_upload(self.tmpdir / "missing.mp4", title="T", description="D",
                           tags=[], account="default")

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_success_no_thumbnail(self, mock_build, mock_media, mock_auth):
        mp4 = self._mk_mp4()
        mock_build.return_value = _make_fake_youtube("vid1")
        result = youtube_upload(mp4, title="T", description="D", tags=[], account="default")
        self.assertEqual(result["video_id"], "vid1")
        self.assertIn("youtu.be/vid1", result["url"])
        self.assertFalse(result["thumbnail_set"])

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_publish_at_forces_private(self, mock_build, mock_media, mock_auth):
        mp4 = self._mk_mp4()
        mock_build.return_value = _make_fake_youtube()
        result = youtube_upload(mp4, title="T", description="D", tags=[],
                                privacy="public", publish_at="2026-12-01T10:00:00Z")
        self.assertEqual(result["privacy"], "private")
        self.assertEqual(result["publish_at"], "2026-12-01T10:00:00Z")

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_progress_callback(self, mock_build, mock_media, mock_auth):
        mp4 = self._mk_mp4()
        yt = _make_fake_youtube()
        # First call returns chunk_status (progress), second returns final response
        chunk_status = MagicMock()
        chunk_status.progress.return_value = 0.5
        yt.videos.return_value.insert.return_value.next_chunk.side_effect = [
            (chunk_status, None),
            (None, {"id": "vid2", "kind": "youtube#video", "etag": "e"}),
        ]
        mock_build.return_value = yt

        progress_calls = []
        result = youtube_upload(mp4, title="T", description="D", tags=[],
                                progress_cb=lambda pct: progress_calls.append(pct))
        self.assertEqual(result["video_id"], "vid2")
        self.assertTrue(len(progress_calls) > 0)

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("pipeline.upload.upload.time")
    @patch("googleapiclient.discovery.build")
    def test_5xx_retry_then_success(self, mock_build, mock_time, mock_media, mock_auth):
        mp4 = self._mk_mp4()
        yt = _make_fake_youtube()
        from googleapiclient.errors import HttpError
        yt.videos.return_value.insert.return_value.next_chunk.side_effect = [
            _make_http_error(503),
            (None, {"id": "vid3", "kind": "youtube#video", "etag": "e"}),
        ]
        mock_build.return_value = yt
        result = youtube_upload(mp4, title="T", description="D", tags=[])
        self.assertEqual(result["video_id"], "vid3")
        mock_time.sleep.assert_called()

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("pipeline.upload.upload.time")
    @patch("googleapiclient.discovery.build")
    def test_5xx_retry_exceeds_backoff_raises(self, mock_build, mock_time, mock_media, mock_auth):
        mp4 = self._mk_mp4()
        yt = _make_fake_youtube()
        from googleapiclient.errors import HttpError
        # 7 retries: backoff 1,2,4,8,16,32,64 → next would be 128 > 64, so raise
        # backoff: 1→2→4→8→16→32→64 (all ≤64, retry); 64→128 (>64, raise)
        # That's 8 next_chunk calls that all return 503.
        yt.videos.return_value.insert.return_value.next_chunk.side_effect = [
            _make_http_error(503),
            _make_http_error(503),
            _make_http_error(503),
            _make_http_error(503),
            _make_http_error(503),
            _make_http_error(503),
            _make_http_error(503),
            _make_http_error(503),
        ]
        mock_build.return_value = yt
        with self.assertRaises(UploadError):
            youtube_upload(mp4, title="T", description="D", tags=[])

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_non_transient_error_raises(self, mock_build, mock_media, mock_auth):
        mp4 = self._mk_mp4()
        yt = _make_fake_youtube()
        yt.videos.return_value.insert.return_value.next_chunk.side_effect = _make_http_error(400)
        mock_build.return_value = yt
        with self.assertRaises(UploadError):
            youtube_upload(mp4, title="T", description="D", tags=[])

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_no_video_id_raises(self, mock_build, mock_media, mock_auth):
        mp4 = self._mk_mp4()
        yt = _make_fake_youtube()
        yt.videos.return_value.insert.return_value.next_chunk.return_value = (None, {})  # no 'id'
        mock_build.return_value = yt
        with self.assertRaises(UploadError):
            youtube_upload(mp4, title="T", description="D", tags=[])

    @patch("pipeline.upload.upload.authenticate")
    @patch("pipeline.upload.upload.set_thumbnail")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_with_thumbnail_success(self, mock_build, mock_media, mock_thumb, mock_auth):
        mp4 = self._mk_mp4()
        mock_build.return_value = _make_fake_youtube()
        mock_thumb.return_value = {}
        thumb = self.tmpdir / "thumb.jpg"
        thumb.write_bytes(b"\xff" * 100)
        result = youtube_upload(mp4, title="T", description="D", tags=[],
                                thumbnail_path=thumb)
        self.assertTrue(result["thumbnail_set"])

    @patch("pipeline.upload.upload.authenticate")
    @patch("pipeline.upload.upload.set_thumbnail", side_effect=Exception("thumb error"))
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_thumbnail_failure_non_fatal(self, mock_build, mock_media, mock_thumb, mock_auth):
        mp4 = self._mk_mp4()
        mock_build.return_value = _make_fake_youtube()
        thumb = self.tmpdir / "thumb.jpg"
        thumb.write_bytes(b"\xff" * 100)
        result = youtube_upload(mp4, title="T", description="D", tags=[],
                                thumbnail_path=thumb)
        self.assertFalse(result["thumbnail_set"])
        self.assertIsNotNone(result["thumbnail_error"])

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_progress_callback_exception_ignored(self, mock_build, mock_media, mock_auth):
        """progress_cb raising an exception is swallowed."""
        mp4 = self._mk_mp4()
        yt = _make_fake_youtube()
        chunk_status = MagicMock()
        chunk_status.progress.return_value = 0.2
        yt.videos.return_value.insert.return_value.next_chunk.side_effect = [
            (chunk_status, None),
            (None, {"id": "vid99", "kind": "youtube#video", "etag": "e"}),
        ]
        mock_build.return_value = yt

        def bad_cb(_):
            raise RuntimeError("callback error")

        result = youtube_upload(mp4, title="T", description="D", tags=[], progress_cb=bad_cb)
        self.assertEqual(result["video_id"], "vid99")


# ===========================================================================
# 10. _ensure_critique
# ===========================================================================

class TestEnsureCritique(_UploadTestBase):

    def _setup_channel(self, slug="slug1"):
        chan = self._mk_channel()
        cache_dir = chan / "cache" / slug
        cache_dir.mkdir(parents=True)
        out_dir = chan / "critiques" / slug
        out_dir.mkdir(parents=True)
        return chan, cache_dir, out_dir

    def test_cached_critique_fresh_returned(self):
        """When score file is newer than mp4, cached critique is returned."""
        chan, cache_dir, out_dir = self._setup_channel()
        mp4 = self._mk_mp4()
        score_file = out_dir / "slug1.score.json"
        score_file.write_text(json.dumps({"score": 8, "one_line_take": "Great"}))
        # Make score file newer than mp4
        import time as _time
        _time.sleep(0.01)
        score_file.touch()

        result = _ensure_critique(
            slug="slug1", mp4_path=mp4,
            project_root=self.project_root, channel_dir="testchan",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["score"], 8)

    def test_no_beats_returns_none(self):
        """No beats.json → can't critique → returns None."""
        chan, cache_dir, out_dir = self._setup_channel()
        mp4 = self._mk_mp4()
        result = _ensure_critique(
            slug="slug1", mp4_path=mp4,
            project_root=self.project_root, channel_dir="testchan",
        )
        self.assertIsNone(result)

    def test_critic_import_fails_returns_none(self):
        chan, cache_dir, out_dir = self._setup_channel()
        mp4 = self._mk_mp4()
        (cache_dir / "beats.json").write_text("[]")

        with patch.dict("sys.modules", {"pipeline.llm": None, "pipeline.llm.critic": None}):
            result = _ensure_critique(
                slug="slug1", mp4_path=mp4,
                project_root=self.project_root, channel_dir="testchan",
            )
        self.assertIsNone(result)

    def test_critic_succeeds(self):
        chan, cache_dir, out_dir = self._setup_channel()
        mp4 = self._mk_mp4()
        (cache_dir / "beats.json").write_text("[]")

        mock_critic = MagicMock()
        mock_critic.critique_short.return_value = {"score": 7, "one_line_take": "OK"}
        mock_llm = MagicMock()
        mock_llm.critic = mock_critic

        with patch.dict("sys.modules", {"pipeline.llm": mock_llm, "pipeline.llm.critic": mock_critic}):
            result = _ensure_critique(
                slug="slug1", mp4_path=mp4,
                project_root=self.project_root, channel_dir="testchan",
            )
        self.assertEqual(result["score"], 7)

    def test_critic_raises_returns_none(self):
        """Critic exception → fail-open with None."""
        chan, cache_dir, out_dir = self._setup_channel()
        mp4 = self._mk_mp4()
        (cache_dir / "beats.json").write_text("[]")

        mock_critic = MagicMock()
        mock_critic.critique_short.side_effect = RuntimeError("critic flake")
        mock_llm = MagicMock()
        mock_llm.critic = mock_critic

        with patch.dict("sys.modules", {"pipeline.llm": mock_llm, "pipeline.llm.critic": mock_critic}):
            result = _ensure_critique(
                slug="slug1", mp4_path=mp4,
                project_root=self.project_root, channel_dir="testchan",
            )
        self.assertIsNone(result)

    def test_no_channel_dir_uses_data_cache(self):
        """No channel_dir → falls back to data/cache/<slug> and data/critiques/<slug>."""
        mp4 = self._mk_mp4()
        # No beats.json anywhere → returns None
        result = _ensure_critique(
            slug="slug1", mp4_path=mp4,
            project_root=self.project_root, channel_dir=None,
        )
        self.assertIsNone(result)

    def test_force_reruns_even_if_fresh_cache(self):
        """force=True skips cache check → goes to beats check → no beats → None."""
        chan, cache_dir, out_dir = self._setup_channel()
        mp4 = self._mk_mp4()
        score_file = out_dir / "slug1.score.json"
        score_file.write_text(json.dumps({"score": 9}))

        result = _ensure_critique(
            slug="slug1", mp4_path=mp4,
            project_root=self.project_root, channel_dir="testchan",
            force=True,
        )
        self.assertIsNone(result)  # No beats.json, so None

    def test_legacy_data_cache_fallback(self):
        """channel_dir given but per-channel cache not found → data/cache/<slug>."""
        chan = self._mk_channel()
        # Don't create per-channel cache — critiques dir doesn't exist either
        mp4 = self._mk_mp4()
        result = _ensure_critique(
            slug="slug1", mp4_path=mp4,
            project_root=self.project_root, channel_dir="testchan",
        )
        self.assertIsNone(result)

    def test_cached_score_read_error_falls_through(self):
        """OSError reading cached score file → silently ignored, falls through to beats check."""
        chan, cache_dir, out_dir = self._setup_channel()
        mp4 = self._mk_mp4()
        # Create score file so the cache-check path is entered
        score_file = out_dir / "slug1.score.json"
        score_file.write_text("{bad json")  # invalid JSON → JSONDecodeError
        # Make score file appear newer than mp4 so stat path is entered
        import time as _time
        _time.sleep(0.01)
        score_file.touch()
        # No beats.json → should return None after cache falls through
        result = _ensure_critique(
            slug="slug1", mp4_path=mp4,
            project_root=self.project_root, channel_dir="testchan",
        )
        self.assertIsNone(result)


# ===========================================================================
# 11. existing_upload
# ===========================================================================

class TestExistingUpload(_UploadTestBase):
    def test_not_found(self):
        self._mk_channel()
        result = existing_upload(self.project_root, "testchan", "slug1")
        self.assertIsNone(result)

    def test_found_with_video_id(self):
        chan = self._mk_channel()
        uploads = chan / "uploads"
        uploads.mkdir()
        (uploads / "slug1.json").write_text(json.dumps({"video_id": "vid123", "url": "http://youtu.be/vid123"}))
        result = existing_upload(self.project_root, "testchan", "slug1")
        self.assertIsNotNone(result)
        self.assertEqual(result["video_id"], "vid123")

    def test_found_without_video_id_returns_none(self):
        chan = self._mk_channel()
        uploads = chan / "uploads"
        uploads.mkdir()
        (uploads / "slug1.json").write_text(json.dumps({"title": "no video id"}))
        result = existing_upload(self.project_root, "testchan", "slug1")
        self.assertIsNone(result)

    def test_bad_json_returns_none(self):
        chan = self._mk_channel()
        uploads = chan / "uploads"
        uploads.mkdir()
        (uploads / "slug1.json").write_text("{{{invalid")
        result = existing_upload(self.project_root, "testchan", "slug1")
        self.assertIsNone(result)


# ===========================================================================
# 12. get_channel_sub_count
# ===========================================================================

class TestGetChannelSubCount(_UploadTestBase):
    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.discovery.build")
    def test_success(self, mock_build, mock_auth):
        mock_build.return_value = _make_fake_youtube()
        count = get_channel_sub_count("default")
        self.assertEqual(count, 42000)

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.discovery.build")
    def test_no_items_raises(self, mock_build, mock_auth):
        yt = _make_fake_youtube()
        yt.channels.return_value.list.return_value.execute.return_value = {"items": []}
        mock_build.return_value = yt
        with self.assertRaises(UploadError):
            get_channel_sub_count("default")

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.discovery.build")
    def test_hidden_subscriber_count_raises(self, mock_build, mock_auth):
        yt = _make_fake_youtube()
        yt.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"statistics": {"hiddenSubscriberCount": True}}]
        }
        mock_build.return_value = yt
        with self.assertRaises(UploadError):
            get_channel_sub_count("default")


# ===========================================================================
# 13. _find_cast_path_for_sidecar / write_part2_pending
# ===========================================================================

class TestWritePart2Pending(_UploadTestBase):

    def _mk_upload_record(self):
        return {"video_id": "vid123", "url": "https://youtu.be/vid123"}

    def test_no_cliffhanger_returns_none(self):
        result = write_part2_pending(
            project_root=self.project_root,
            channel_yaml={},
            channel_dir="testchan",
            slug="slug1",
            upload_record=self._mk_upload_record(),
            script={},
            raw=None,
            account="default",
        )
        self.assertIsNone(result)

    def test_cliffhanger_no_part2_channel_returns_none(self):
        result = write_part2_pending(
            project_root=self.project_root,
            channel_yaml={"cliffhanger": True},
            channel_dir="testchan",
            slug="slug1",
            upload_record=self._mk_upload_record(),
            script={},
            raw=None,
            account="default",
        )
        self.assertIsNone(result)

    @patch("pipeline.upload.upload.get_channel_sub_count", side_effect=Exception("quota error"))
    def test_subs_fail_returns_none(self, mock_subs):
        self._mk_channel()
        result = write_part2_pending(
            project_root=self.project_root,
            channel_yaml={"cliffhanger": True, "part2_channel": "chan2"},
            channel_dir="testchan",
            slug="slug1",
            upload_record=self._mk_upload_record(),
            script={"narration": "blah"},
            raw=None,
            account="default",
        )
        self.assertIsNone(result)

    @patch("pipeline.upload.upload.get_channel_sub_count", return_value=5000)
    def test_success_writes_sidecar(self, mock_subs):
        self._mk_channel()
        result = write_part2_pending(
            project_root=self.project_root,
            channel_yaml={
                "cliffhanger": True,
                "part2_channel": "chan2",
                "part2_trigger": {"subs_delta": 200, "window_days": 14},
            },
            channel_dir="testchan",
            slug="slug1",
            upload_record=self._mk_upload_record(),
            script={"narration": "Part 1 narration"},
            raw={"url": "http://example.com"},
            account="default",
        )
        self.assertIsNotNone(result)
        self.assertTrue(result.exists())
        data = json.loads(result.read_text())
        self.assertEqual(data["baseline_subs"], 5000)
        self.assertEqual(data["threshold_subs_delta"], 200)

    def test_find_cast_path_found_in_channel(self):
        chan = self._mk_channel()
        cast_dir = chan / "cast"
        cast_dir.mkdir()
        (cast_dir / "slug1.json").write_text(json.dumps([]))
        result = _find_cast_path_for_sidecar(self.project_root, "testchan", "slug1")
        self.assertIsNotNone(result)
        self.assertIn("slug1.json", result)

    def test_find_cast_path_legacy_fallback(self):
        legacy = self.project_root / "data" / "intermediate" / "testchan" / "cast"
        legacy.mkdir(parents=True)
        (legacy / "slug1.json").write_text(json.dumps([]))
        self._mk_channel()
        result = _find_cast_path_for_sidecar(self.project_root, "testchan", "slug1")
        self.assertIsNotNone(result)

    def test_find_cast_path_not_found(self):
        self._mk_channel()
        result = _find_cast_path_for_sidecar(self.project_root, "testchan", "slug1")
        self.assertIsNone(result)

    def test_find_cast_path_relative_to_raises_value_error(self):
        """Cast file exists but cast.relative_to(project_root) raises ValueError → str(cast) returned."""
        # Make the cast file at a path outside project_root by mocking RenderPaths
        # to return a path in tmpdir that is NOT under project_root.
        other_cast = self.tmpdir / "outside_project" / "cast" / "slug1.json"
        other_cast.parent.mkdir(parents=True)
        other_cast.write_text(json.dumps([]))

        mock_paths = MagicMock()
        mock_paths.cast_for.return_value = other_cast
        with patch("pipeline.paths.RenderPaths") as mock_rp_cls:
            mock_rp_cls.from_channel_dir.return_value = mock_paths
            # project_root is self.project_root; other_cast is NOT under it
            result = _find_cast_path_for_sidecar(self.project_root, "testchan", "slug1")
        # Should return str(other_cast) since relative_to raises ValueError
        self.assertIsNotNone(result)
        self.assertIn("slug1.json", result)


# ===========================================================================
# 14. write_upload_record / _mirror_record_to_gcs
# ===========================================================================

class TestWriteUploadRecord(_UploadTestBase):

    def test_basic_write_gcs_disabled(self):
        self._mk_channel()
        record = {"video_id": "vid1", "url": "https://youtu.be/vid1"}
        p = write_upload_record(self.project_root, "testchan", "slug1", record)
        self.assertTrue(p.exists())
        self.assertEqual(json.loads(p.read_text())["video_id"], "vid1")

    def test_mirror_to_gcs_disabled_by_env(self):
        """YTFACTORY_DASHBOARD_GCS_SYNC=0 → returns early, no import."""
        os.environ["YTFACTORY_DASHBOARD_GCS_SYNC"] = "0"
        # Should not raise even without control.core
        _mirror_record_to_gcs(self.tmpdir / "rec.json", self.project_root, {})

    def test_mirror_to_gcs_import_failure(self):
        """ImportError from control.storage → warns, doesn't raise."""
        os.environ["YTFACTORY_DASHBOARD_GCS_SYNC"] = "1"
        with patch.dict("sys.modules", {"control.storage": None}):
            _mirror_record_to_gcs(self.tmpdir / "rec.json", self.project_root, {})

    def test_mirror_to_gcs_success(self):
        os.environ["YTFACTORY_DASHBOARD_GCS_SYNC"] = "1"
        mock_storage = MagicMock()
        mock_storage.upload_record_rel_key.return_value = "rel/key"
        mock_storage.upload_record_uri.return_value = "gs://bucket/rel/key"
        with patch.dict("sys.modules", {"control.storage": mock_storage}):
            _mirror_record_to_gcs(self.tmpdir / "rec.json", self.project_root, {"v": 1})
        mock_storage.upload_bytes.assert_called_once()

    def test_mirror_to_gcs_upload_failure_warns(self):
        os.environ["YTFACTORY_DASHBOARD_GCS_SYNC"] = "1"
        mock_storage = MagicMock()
        mock_storage.upload_bytes.side_effect = Exception("network error")
        with patch.dict("sys.modules", {"control.storage": mock_storage}):
            # Should not raise
            _mirror_record_to_gcs(self.tmpdir / "rec.json", self.project_root, {})


# ===========================================================================
# 15. upload_short
# ===========================================================================

class TestUploadShort(_UploadTestBase):

    def _base_yaml(self, privacy="public", account="default"):
        return {
            "upload": {
                "account": account,
                "privacy": privacy,
                "tags": ["test"],
                "min_score": 6,
            }
        }

    def _base_call(self, **overrides):
        defaults = dict(
            project_root=self.project_root,
            channel_yaml=self._base_yaml(),
            channel_dir="testchan",
            slug="slug1",
            mp4_path=self._mk_mp4(),
            script={"hook": "My Hook"},
            raw=None,
        )
        defaults.update(overrides)
        return defaults

    def test_existing_upload_returns_cached(self):
        """If upload record already exists, return it without re-uploading."""
        chan = self._mk_channel()
        uploads = chan / "uploads"
        uploads.mkdir()
        (uploads / "slug1.json").write_text(json.dumps({"video_id": "old", "url": "http://x"}))

        result = upload_short(**self._base_call())
        self.assertEqual(result["video_id"], "old")

    @patch("pipeline.upload.upload._ensure_critique")
    @patch("pipeline.upload.upload.youtube_upload")
    @patch("pipeline.upload.upload.write_upload_record")
    @patch("pipeline.upload.upload.write_part2_pending", return_value=None)
    def test_skip_critic(self, mock_p2, mock_wr, mock_yu, mock_ec):
        """skip_critic=True bypasses the critic gate."""
        self._mk_channel()
        mock_yu.return_value = {"video_id": "v1", "url": "u", "privacy": "public",
                                "publish_at": None, "thumbnail_path": None,
                                "thumbnail_set": False, "thumbnail_error": None,
                                "raw_response": {}}
        result = upload_short(**self._base_call(skip_critic=True))
        mock_ec.assert_not_called()
        self.assertEqual(result["video_id"], "v1")

    @patch("pipeline.upload.upload._ensure_critique", return_value=None)
    @patch("pipeline.upload.upload.youtube_upload")
    @patch("pipeline.upload.upload.write_upload_record")
    @patch("pipeline.upload.upload.write_part2_pending", return_value=None)
    def test_critic_returns_none_legacy_proceeds(self, mock_p2, mock_wr, mock_yu, mock_ec):
        """Critic returns None (no beats.json) → legacy path, proceed with upload."""
        self._mk_channel()
        mock_yu.return_value = {"video_id": "v2", "url": "u", "privacy": "public",
                                "publish_at": None, "thumbnail_path": None,
                                "thumbnail_set": False, "thumbnail_error": None,
                                "raw_response": {}}
        result = upload_short(**self._base_call())
        self.assertEqual(result["video_id"], "v2")

    @patch("pipeline.upload.upload._ensure_critique", return_value={"score": 3, "one_line_take": "Bad"})
    def test_critic_score_too_low_raises(self, mock_ec):
        self._mk_channel()
        with self.assertRaises(UploadError):
            upload_short(**self._base_call())

    @patch("pipeline.upload.upload._ensure_critique", return_value={"score": 8, "one_line_take": "Good"})
    @patch("pipeline.upload.upload.youtube_upload")
    @patch("pipeline.upload.upload.write_upload_record")
    @patch("pipeline.upload.upload.write_part2_pending", return_value=None)
    def test_critic_passes_upload_proceeds(self, mock_p2, mock_wr, mock_yu, mock_ec):
        self._mk_channel()
        mock_yu.return_value = {"video_id": "v3", "url": "u", "privacy": "public",
                                "publish_at": None, "thumbnail_path": None,
                                "thumbnail_set": False, "thumbnail_error": None,
                                "raw_response": {}}
        result = upload_short(**self._base_call())
        self.assertEqual(result["video_id"], "v3")

    @patch("pipeline.upload.upload._ensure_critique", return_value=None)
    @patch("pipeline.upload.upload.youtube_upload")
    @patch("pipeline.upload.upload.write_upload_record")
    @patch("pipeline.upload.upload.write_part2_pending", return_value=None)
    def test_min_score_override(self, mock_p2, mock_wr, mock_yu, mock_ec):
        self._mk_channel()
        mock_yu.return_value = {"video_id": "v4", "url": "u", "privacy": "public",
                                "publish_at": None, "thumbnail_path": None,
                                "thumbnail_set": False, "thumbnail_error": None,
                                "raw_response": {}}
        result = upload_short(**self._base_call(min_score_override=0))
        self.assertEqual(result["video_id"], "v4")

    @patch("pipeline.upload.upload._ensure_critique", return_value=None)
    @patch("pipeline.upload.upload.compute_throttled_publish_at", return_value="2026-06-01T12:00:00Z")
    @patch("pipeline.upload.upload.youtube_upload")
    @patch("pipeline.upload.upload.write_upload_record")
    @patch("pipeline.upload.upload.write_part2_pending", return_value=None)
    def test_throttle_applies_for_public(self, mock_p2, mock_wr, mock_yu, mock_throttle, mock_ec):
        self._mk_channel()
        mock_yu.return_value = {"video_id": "v5", "url": "u", "privacy": "private",
                                "publish_at": "2026-06-01T12:00:00Z",
                                "thumbnail_path": None, "thumbnail_set": False,
                                "thumbnail_error": None, "raw_response": {}}
        result = upload_short(**self._base_call())
        mock_throttle.assert_called_once()

    @patch("pipeline.upload.upload._ensure_critique", return_value=None)
    @patch("pipeline.upload.upload.compute_throttled_publish_at")
    @patch("pipeline.upload.upload.youtube_upload")
    @patch("pipeline.upload.upload.write_upload_record")
    @patch("pipeline.upload.upload.write_part2_pending", return_value=None)
    def test_no_throttle_when_publish_at_already_set(self, mock_p2, mock_wr, mock_yu, mock_throttle, mock_ec):
        """If publish_at is already provided, compute_throttled_publish_at is not called."""
        self._mk_channel()
        mock_yu.return_value = {"video_id": "v6", "url": "u", "privacy": "private",
                                "publish_at": "2026-06-01T12:00:00Z", "thumbnail_path": None,
                                "thumbnail_set": False, "thumbnail_error": None,
                                "raw_response": {}}
        upload_short(**self._base_call(publish_at="2026-06-01T12:00:00Z"))
        mock_throttle.assert_not_called()

    @patch("pipeline.upload.upload._ensure_critique", return_value=None)
    @patch("pipeline.upload.upload.youtube_upload")
    @patch("pipeline.upload.upload.write_upload_record")
    @patch("pipeline.upload.upload.write_part2_pending", side_effect=Exception("p2 err"))
    def test_part2_write_failure_non_fatal(self, mock_p2, mock_wr, mock_yu, mock_ec):
        self._mk_channel()
        mock_yu.return_value = {"video_id": "v7", "url": "u", "privacy": "public",
                                "publish_at": None, "thumbnail_path": None,
                                "thumbnail_set": False, "thumbnail_error": None,
                                "raw_response": {}}
        result = upload_short(**self._base_call())
        # Should still return successfully
        self.assertEqual(result["video_id"], "v7")

    @patch("pipeline.upload.upload._ensure_critique", return_value=None)
    @patch("pipeline.upload.upload.youtube_upload")
    @patch("pipeline.upload.upload.write_upload_record")
    @patch("pipeline.upload.upload.write_part2_pending", return_value=None)
    def test_cross_engage_failure_non_fatal(self, mock_p2, mock_wr, mock_yu, mock_ec):
        self._mk_channel()
        mock_yu.return_value = {"video_id": "v8", "url": "u", "privacy": "public",
                                "publish_at": None, "thumbnail_path": None,
                                "thumbnail_set": False, "thumbnail_error": None,
                                "raw_response": {}}
        mock_engage = MagicMock(side_effect=Exception("engage failed"))
        with patch.dict("sys.modules", {
            "pipeline.research": MagicMock(cross_engage=MagicMock(engage_after_upload=mock_engage)),
        }):
            result = upload_short(**self._base_call())
        self.assertEqual(result["video_id"], "v8")

    @patch("pipeline.upload.upload._ensure_critique", return_value=None)
    @patch("pipeline.upload.upload.youtube_upload")
    @patch("pipeline.upload.upload.write_upload_record")
    @patch("pipeline.upload.upload.write_part2_pending", return_value=None)
    def test_auto_thumbnail_success(self, mock_p2, mock_wr, mock_yu, mock_ec):
        chan = self._mk_channel()
        cache_dir = chan / "cache" / "slug1"
        cache_dir.mkdir(parents=True)
        mock_yu.return_value = {"video_id": "v9", "url": "u", "privacy": "public",
                                "publish_at": None, "thumbnail_path": None,
                                "thumbnail_set": False, "thumbnail_error": None,
                                "raw_response": {}}
        thumb = cache_dir / "auto_thumb.jpg"
        mock_thumb_mod = MagicMock()
        mock_thumb_mod.auto_thumbnail.return_value = thumb

        with patch.dict("sys.modules", {
            "pipeline.thumbnails": mock_thumb_mod,
        }):
            result = upload_short(**self._base_call())
        # thumbnail_path should have been passed to youtube_upload
        self.assertEqual(result["video_id"], "v9")

    @patch("pipeline.upload.upload._ensure_critique", return_value=None)
    @patch("pipeline.upload.upload.youtube_upload")
    @patch("pipeline.upload.upload.write_upload_record")
    @patch("pipeline.upload.upload.write_part2_pending", return_value=None)
    def test_auto_thumbnail_disabled(self, mock_p2, mock_wr, mock_yu, mock_ec):
        self._mk_channel()
        mock_yu.return_value = {"video_id": "v10", "url": "u", "privacy": "public",
                                "publish_at": None, "thumbnail_path": None,
                                "thumbnail_set": False, "thumbnail_error": None,
                                "raw_response": {}}
        result = upload_short(**self._base_call(auto_thumbnail=False))
        self.assertEqual(result["video_id"], "v10")

    @patch("pipeline.upload.upload._ensure_critique", return_value=None)
    @patch("pipeline.upload.upload.youtube_upload")
    @patch("pipeline.upload.upload.write_upload_record")
    @patch("pipeline.upload.upload.write_part2_pending", return_value=None)
    def test_auto_thumbnail_exception_non_fatal(self, mock_p2, mock_wr, mock_yu, mock_ec):
        chan = self._mk_channel()
        cache_dir = chan / "cache" / "slug1"
        cache_dir.mkdir(parents=True)
        mock_yu.return_value = {"video_id": "v11", "url": "u", "privacy": "public",
                                "publish_at": None, "thumbnail_path": None,
                                "thumbnail_set": False, "thumbnail_error": None,
                                "raw_response": {}}
        mock_thumb_mod = MagicMock()
        mock_thumb_mod.auto_thumbnail.side_effect = Exception("thumb error")

        with patch.dict("sys.modules", {
            "pipeline.thumbnails": mock_thumb_mod,
        }):
            result = upload_short(**self._base_call())
        self.assertEqual(result["video_id"], "v11")

    @patch("pipeline.upload.upload._ensure_critique", return_value=None)
    @patch("pipeline.upload.upload.youtube_upload")
    @patch("pipeline.upload.upload.write_upload_record")
    @patch("pipeline.upload.upload.write_part2_pending", return_value=None)
    def test_privacy_and_tags_override(self, mock_p2, mock_wr, mock_yu, mock_ec):
        self._mk_channel()
        mock_yu.return_value = {"video_id": "v12", "url": "u", "privacy": "unlisted",
                                "publish_at": None, "thumbnail_path": None,
                                "thumbnail_set": False, "thumbnail_error": None,
                                "raw_response": {}}
        upload_short(**self._base_call(
            privacy_override="unlisted",
            tags_override=["override", "tags"],
        ))
        called_kw = mock_yu.call_args.kwargs
        self.assertEqual(called_kw["privacy"], "unlisted")
        self.assertEqual(called_kw["tags"], ["override", "tags"])

    def test_force_bypasses_idempotency(self):
        """force=True skips existing-upload check and proceeds to upload."""
        chan = self._mk_channel()
        uploads = chan / "uploads"
        uploads.mkdir()
        (uploads / "slug1.json").write_text(json.dumps({"video_id": "old"}))

        with patch("pipeline.upload.upload._ensure_critique", return_value=None):
            with patch("pipeline.upload.upload.youtube_upload") as mock_yu:
                with patch("pipeline.upload.upload.write_upload_record"):
                    with patch("pipeline.upload.upload.write_part2_pending", return_value=None):
                        mock_yu.return_value = {"video_id": "new", "url": "u",
                                                "privacy": "public", "publish_at": None,
                                                "thumbnail_path": None, "thumbnail_set": False,
                                                "thumbnail_error": None, "raw_response": {}}
                        result = upload_short(**self._base_call(force=True))
        self.assertEqual(result["video_id"], "new")


# ===========================================================================
# 16. CLI helpers: _discover_accounts, _print_status_table,
#                  _cmd_auth_status, _cmd_auth_refresh, main
# ===========================================================================

class TestDiscoverAccounts(_UploadTestBase):
    def test_from_youtube_stats(self):
        mock_ys = MagicMock()
        mock_ys.iter_channel_configs.return_value = [("acc1", {}), ("acc2", {})]
        with patch.dict("sys.modules", {
            "pipeline.research": MagicMock(youtube=mock_ys),
            "pipeline.research.youtube": mock_ys,
        }):
            result = _discover_accounts()
        self.assertEqual(sorted(result), ["acc1", "acc2"])

    def test_fallback_to_token_files(self):
        """youtube_stats fails → fall back to token files in CONFIG_DIR."""
        (self.config_dir / "youtube_token_alpha.json").touch()
        (self.config_dir / "youtube_token_beta.json").touch()

        mock_ys = MagicMock()
        mock_ys.iter_channel_configs.side_effect = Exception("no configs")
        with patch.dict("sys.modules", {
            "pipeline.research": MagicMock(youtube=mock_ys),
            "pipeline.research.youtube": mock_ys,
        }):
            result = _discover_accounts()
        self.assertIn("alpha", result)
        self.assertIn("beta", result)

    def test_empty_fallback(self):
        """No configs and no token files → empty list."""
        mock_ys = MagicMock()
        mock_ys.iter_channel_configs.side_effect = Exception("fail")
        with patch.dict("sys.modules", {
            "pipeline.research": MagicMock(youtube=mock_ys),
            "pipeline.research.youtube": mock_ys,
        }):
            result = _discover_accounts()
        self.assertEqual(result, [])

    def test_config_dir_missing_returns_empty(self):
        """youtube_stats fails and CONFIG_DIR doesn't exist → return [] (line 1179)."""
        nonexistent = self.tmpdir / "no_such_dir"
        mock_ys = MagicMock()
        mock_ys.iter_channel_configs.side_effect = Exception("fail")
        with patch.dict("sys.modules", {
            "pipeline.research": MagicMock(youtube=mock_ys),
            "pipeline.research.youtube": mock_ys,
        }):
            with patch("pipeline.upload.upload.CONFIG_DIR", nonexistent):
                result = _discover_accounts()
        self.assertEqual(result, [])


class TestPrintStatusTable(unittest.TestCase):
    def test_all_states_print_without_error(self):
        import io
        rows = [
            {"account": "a", "state": "ok", "expiry": "2026-06-01"},
            {"account": "b", "state": "missing"},
            {"account": "c", "state": "no_refresh_token", "expiry": "2026-01-01"},
            {"account": "d", "state": "missing_scopes", "missing": ["youtube", "upload"]},
            {"account": "e", "state": "unreadable", "error": "bad JSON in file"},
        ]
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            _print_status_table(rows)
        # Just verify it ran without error


class TestCmdAuthStatus(_UploadTestBase):
    @patch("pipeline.upload.upload._discover_accounts", return_value=[])
    def test_no_accounts_returns_1(self, mock_disc):
        args = Namespace()
        result = _cmd_auth_status(args)
        self.assertEqual(result, 1)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["acct1"])
    @patch("pipeline.upload.upload.inspect_token_status")
    def test_with_bad_accounts_prints_message(self, mock_inspect, mock_disc):
        mock_inspect.return_value = {"account": "acct1", "state": "missing"}
        args = Namespace()
        result = _cmd_auth_status(args)
        self.assertEqual(result, 0)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["acct1"])
    @patch("pipeline.upload.upload.inspect_token_status")
    def test_all_ok_returns_0(self, mock_inspect, mock_disc):
        mock_inspect.return_value = {"account": "acct1", "state": "ok", "expiry": "2026"}
        args = Namespace()
        result = _cmd_auth_status(args)
        self.assertEqual(result, 0)


class TestCmdAuthRefresh(_UploadTestBase):
    """Branch-by-branch coverage of _cmd_auth_refresh."""

    def _args(self, **kw):
        defaults = dict(account=None, exclude=[], yes=True)
        defaults.update(kw)
        return Namespace(**defaults)

    @patch("pipeline.upload.upload.authenticate")
    @patch("pipeline.upload.upload.inspect_token_status")
    def test_single_account_explicit(self, mock_inspect, mock_auth):
        """--account forces single-account auth."""
        mock_inspect.return_value = {"account": "acct1", "state": "ok", "expiry": "x"}
        args = self._args(account="acct1")
        result = _cmd_auth_refresh(args)
        self.assertEqual(result, 0)
        mock_auth.assert_called_once_with(account="acct1", interactive=True)

    @patch("pipeline.upload.upload._discover_accounts", return_value=[])
    def test_no_accounts_returns_1(self, mock_disc):
        args = self._args()
        result = _cmd_auth_refresh(args)
        self.assertEqual(result, 1)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1"])
    @patch("pipeline.upload.upload.inspect_token_status")
    def test_all_accounts_ok_returns_0(self, mock_inspect, mock_disc):
        mock_inspect.return_value = {"account": "a1", "state": "ok", "expiry": "x"}
        args = self._args()
        result = _cmd_auth_refresh(args)
        self.assertEqual(result, 0)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1", "a2"])
    @patch("pipeline.upload.upload.inspect_token_status")
    def test_user_says_no_returns_1(self, mock_inspect, mock_disc):
        mock_inspect.side_effect = [
            {"account": "a1", "state": "missing"},
            {"account": "a2", "state": "missing"},
        ]
        args = self._args(yes=False)
        with patch("builtins.input", return_value="n"):
            result = _cmd_auth_refresh(args)
        self.assertEqual(result, 1)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1"])
    @patch("pipeline.upload.upload.inspect_token_status")
    def test_user_eof_returns_1(self, mock_inspect, mock_disc):
        mock_inspect.return_value = {"account": "a1", "state": "missing"}
        args = self._args(yes=False)
        with patch("builtins.input", side_effect=EOFError):
            result = _cmd_auth_refresh(args)
        self.assertEqual(result, 1)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1", "a2"])
    @patch("pipeline.upload.upload.inspect_token_status")
    @patch("pipeline.upload.upload.authenticate")
    def test_exclude_skips_account(self, mock_auth, mock_inspect, mock_disc):
        # call order: (1,2) initial batch, (3) pre-status a1, (4) summary a1
        mock_inspect.side_effect = [
            {"account": "a1", "state": "missing"},
            {"account": "a2", "state": "missing"},
            {"account": "a1", "state": "missing"},
            {"account": "a1", "state": "ok", "expiry": "x"},
        ]
        args = self._args(exclude=["a2"])
        result = _cmd_auth_refresh(args)
        self.assertEqual(result, 0)
        # Only a1 should be authed
        mock_auth.assert_called_once_with(account="a1", interactive=True)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1"])
    @patch("pipeline.upload.upload.inspect_token_status")
    @patch("pipeline.upload.upload.authenticate", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt_aborts(self, mock_auth, mock_inspect, mock_disc):
        # call order: (1) initial batch, (2) pre-status before auth, (3) summary
        mock_inspect.side_effect = [
            {"account": "a1", "state": "missing"},
            {"account": "a1", "state": "missing"},
            {"account": "a1", "state": "missing"},
        ]
        args = self._args()
        result = _cmd_auth_refresh(args)
        self.assertEqual(result, 1)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1"])
    @patch("pipeline.upload.upload.inspect_token_status")
    @patch("pipeline.upload.upload.authenticate")
    def test_os_error_eaddrinuse(self, mock_auth, mock_inspect, mock_disc):
        e = OSError("port in use")
        e.errno = errno.EADDRINUSE
        mock_auth.side_effect = e
        # (1) initial batch, (2) pre-status before auth, (3) summary
        mock_inspect.side_effect = [
            {"account": "a1", "state": "missing"},
            {"account": "a1", "state": "missing"},
            {"account": "a1", "state": "missing"},
        ]
        args = self._args()
        result = _cmd_auth_refresh(args)
        self.assertEqual(result, 1)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1"])
    @patch("pipeline.upload.upload.inspect_token_status")
    @patch("pipeline.upload.upload.authenticate")
    def test_os_error_other(self, mock_auth, mock_inspect, mock_disc):
        e = OSError("disk full")
        e.errno = errno.ENOENT
        mock_auth.side_effect = e
        # (1) initial batch, (2) pre-status before auth, (3) summary
        mock_inspect.side_effect = [
            {"account": "a1", "state": "missing"},
            {"account": "a1", "state": "missing"},
            {"account": "a1", "state": "missing"},
        ]
        args = self._args()
        result = _cmd_auth_refresh(args)
        self.assertEqual(result, 1)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1"])
    @patch("pipeline.upload.upload.inspect_token_status")
    @patch("pipeline.upload.upload.authenticate", side_effect=RuntimeError("unexpected"))
    def test_generic_exception(self, mock_auth, mock_inspect, mock_disc):
        # (1) initial batch, (2) pre-status before auth, (3) summary
        mock_inspect.side_effect = [
            {"account": "a1", "state": "missing"},
            {"account": "a1", "state": "missing"},
            {"account": "a1", "state": "missing"},
        ]
        args = self._args()
        result = _cmd_auth_refresh(args)
        self.assertEqual(result, 1)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1"])
    @patch("pipeline.upload.upload.inspect_token_status")
    @patch("pipeline.upload.upload.authenticate")
    def test_moves_stale_token_aside_no_refresh(self, mock_auth, mock_inspect, mock_disc):
        """Pre-status no_refresh_token → token file renamed before auth."""
        tp = self.config_dir / "youtube_token_a1.json"
        tp.write_text("{}")
        # (1) initial batch → not-ok, (2) pre-status → triggers rename, (3) summary → ok
        mock_inspect.side_effect = [
            {"account": "a1", "state": "no_refresh_token"},
            {"account": "a1", "state": "no_refresh_token", "path": str(tp)},
            {"account": "a1", "state": "ok", "expiry": "x"},
        ]
        # Patch _token_path to return our temp file
        with patch("pipeline.upload.upload._token_path", return_value=tp):
            args = self._args()
            result = _cmd_auth_refresh(args)
        self.assertEqual(result, 0)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1"])
    @patch("pipeline.upload.upload.inspect_token_status")
    @patch("pipeline.upload.upload.authenticate")
    def test_rename_failure_continues(self, mock_auth, mock_inspect, mock_disc):
        """If rename fails (OSError), auth still proceeds."""
        tp = self.config_dir / "youtube_token_a1.json"
        tp.write_text("{}")
        # (1) initial batch → not-ok, (2) pre-status → triggers rename (then OSError), (3) summary → ok
        mock_inspect.side_effect = [
            {"account": "a1", "state": "missing_scopes"},
            {"account": "a1", "state": "missing_scopes", "path": str(tp)},
            {"account": "a1", "state": "ok", "expiry": "x"},
        ]
        with patch("pipeline.upload.upload._token_path", return_value=tp):
            with patch.object(Path, "rename", side_effect=OSError("permission denied")):
                args = self._args()
                result = _cmd_auth_refresh(args)
        self.assertEqual(result, 0)
        mock_auth.assert_called_once()

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1", "a2"])
    @patch("pipeline.upload.upload.inspect_token_status")
    @patch("pipeline.upload.upload.authenticate")
    def test_multiple_accounts_port_free(self, mock_auth, mock_inspect, mock_disc):
        """Second account waits for port → port becomes free → continues."""
        # call order: (1,2) initial batch, (3,4) pre-status for each account, (5,6) summary
        mock_inspect.side_effect = [
            {"account": "a1", "state": "missing"},
            {"account": "a2", "state": "missing"},
            {"account": "a1", "state": "missing"},
            {"account": "a2", "state": "missing"},
            {"account": "a1", "state": "ok", "expiry": "x"},
            {"account": "a2", "state": "ok", "expiry": "x"},
        ]
        os.environ["YTFACTORY_OAUTH_PORT"] = "19876"

        mock_sock = MagicMock()
        with patch("socket.socket", return_value=mock_sock):
            with patch("time.sleep"):
                args = self._args()
                result = _cmd_auth_refresh(args)
        self.assertEqual(result, 0)
        self.assertEqual(mock_auth.call_count, 2)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1", "a2"])
    @patch("pipeline.upload.upload.inspect_token_status")
    @patch("pipeline.upload.upload.authenticate")
    def test_multiple_accounts_port_busy_breaks(self, mock_auth, mock_inspect, mock_disc):
        """Second account's port wait times out → break the loop."""
        # call order: (1,2) initial batch, (3) pre-status a1, (4,5) summary a1+a2
        # a2 port wait times out → break; both in targets → 2 summary calls
        mock_inspect.side_effect = [
            {"account": "a1", "state": "missing"},
            {"account": "a2", "state": "missing"},
            {"account": "a1", "state": "missing"},
            {"account": "a1", "state": "ok", "expiry": "x"},
            {"account": "a2", "state": "missing"},
        ]
        os.environ["YTFACTORY_OAUTH_PORT"] = "19877"

        mock_sock = MagicMock()
        mock_sock.bind.side_effect = OSError("port busy")

        # Make time.time advance past the deadline immediately
        time_calls = [0.0, 100.0]  # deadline=90 → 100 > 90 → loop exits → False

        with patch("socket.socket", return_value=mock_sock):
            with patch("time.sleep"):
                with patch("time.time", side_effect=time_calls):
                    args = self._args()
                    result = _cmd_auth_refresh(args)
        self.assertEqual(result, 1)
        # Only first account was authed
        self.assertEqual(mock_auth.call_count, 1)

    @patch("pipeline.upload.upload._discover_accounts", return_value=["a1", "a2"])
    @patch("pipeline.upload.upload.inspect_token_status")
    @patch("pipeline.upload.upload.authenticate")
    def test_port_retry_then_success(self, mock_auth, mock_inspect, mock_disc):
        """Port busy on first try → succeeds on second try inside _wait_for_port_free."""
        # call order: (1,2) initial batch, (3,4) pre-status for each, (5,6) summary
        mock_inspect.side_effect = [
            {"account": "a1", "state": "missing"},
            {"account": "a2", "state": "missing"},
            {"account": "a1", "state": "missing"},
            {"account": "a2", "state": "missing"},
            {"account": "a1", "state": "ok", "expiry": "x"},
            {"account": "a2", "state": "ok", "expiry": "x"},
        ]
        os.environ["YTFACTORY_OAUTH_PORT"] = "19878"

        mock_sock = MagicMock()
        mock_sock.bind.side_effect = [OSError("busy"), None]  # fail then succeed

        # time.time: deadline=0+90=90, first loop check: 1 < 90 → enter, bind fails,
        # second loop check: 2 < 90 → enter, bind succeeds → return True
        with patch("socket.socket", return_value=mock_sock):
            with patch("time.sleep"):
                with patch("time.time", side_effect=[0.0, 1.0, 2.0, 3.0, 4.0]):
                    args = self._args()
                    result = _cmd_auth_refresh(args)
        self.assertEqual(result, 0)


class TestMain(_UploadTestBase):
    def test_main_auth_status(self):
        with patch("sys.argv", ["prog", "auth", "status"]):
            with patch("pipeline.upload.upload._cmd_auth_status", return_value=0):
                with self.assertRaises(SystemExit) as ctx:
                    main()
                self.assertEqual(ctx.exception.code, 0)

    def test_main_auth_refresh(self):
        with patch("sys.argv", ["prog", "auth", "refresh", "--yes"]):
            with patch("pipeline.upload.upload._cmd_auth_refresh", return_value=0):
                with self.assertRaises(SystemExit) as ctx:
                    main()
                self.assertEqual(ctx.exception.code, 0)

    def test_main_auth_refresh_with_account(self):
        with patch("sys.argv", ["prog", "auth", "refresh", "--account", "myaccount"]):
            with patch("pipeline.upload.upload._cmd_auth_refresh", return_value=1):
                with self.assertRaises(SystemExit) as ctx:
                    main()
                self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
