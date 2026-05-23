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
    _latest_publish_for_account,
    _mirror_record_to_gcs,
    _parse_iso,
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


def _make_http_error(status: int, content: bytes = b"error body"):
    from googleapiclient.errors import HttpError
    return HttpError(resp=_FakeResp(status), content=content)


def _make_quota_http_error(reason: str = "quotaExceeded"):
    """403 with the documented YouTube Data API quota error body shape."""
    body = json.dumps({
        "error": {
            "code": 403,
            "message": "The request cannot be completed because you have "
                       "exceeded your quota.",
            "errors": [{"reason": reason, "domain": "youtube.quota",
                        "message": "quota over"}],
        },
    }).encode()
    return _make_http_error(403, content=body)


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
        # Post-R10 (2026-05-23): channel artifact roots live under
        # ``<project_root>/data/<channel>/`` per pipeline.paths.RenderPaths.
        d = self.project_root / "data" / name
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


class TestPublishThrottleGcsBackend(_UploadTestBase):
    """Audit T1.6 — when YTFACTORY_STATE_BUCKET is set the throttle
    must list GCS upload records (not the laptop FS) so the throttle
    works on the Cloud Run upload path. Without this fix the cloud
    job's project_root.iterdir() returned nothing → throttle returned
    None → publish-storms when YouTube quota frees."""

    def setUp(self) -> None:
        super().setUp()
        self._saved_bucket = os.environ.pop("YTFACTORY_STATE_BUCKET", None)

    def tearDown(self) -> None:
        os.environ.pop("YTFACTORY_STATE_BUCKET", None)
        if self._saved_bucket is not None:
            os.environ["YTFACTORY_STATE_BUCKET"] = self._saved_bucket
        super().tearDown()

    def _make_blob(self, name: str, payload: dict):
        b = MagicMock()
        b.name = name
        b.download_as_text.return_value = json.dumps(payload)
        return b

    def test_dispatches_to_gcs_when_bucket_env_set(self):
        from unittest.mock import patch
        os.environ["YTFACTORY_STATE_BUCKET"] = "test-bucket"
        ts = "2026-05-12T12:00:00Z"
        blobs = [
            self._make_blob(
                "historyrecapped/uploads/aita-001.json",
                {"account": "default", "uploaded_at": ts},
            ),
            # Niche-nested layout matches the laptop semantics.
            self._make_blob(
                "mystoriesanimated/reddit_amitheasshole/uploads/aita-002.json",
                {"account": "default", "publish_at": "2026-06-01T00:00:00Z"},
            ),
            # Wrong account should be skipped.
            self._make_blob(
                "rhymetimejunction/uploads/x.json",
                {"account": "other", "uploaded_at": ts},
            ),
            # X-platform sidecars are excluded.
            self._make_blob(
                "historyrecapped/uploads/aita-001.x.json",
                {"account": "default", "uploaded_at": "2030-01-01T00:00:00Z"},
            ),
        ]
        fake_client = MagicMock()
        fake_client.list_blobs.return_value = iter(blobs)
        with patch(
            "pipeline.upload.upload._gcs_storage_client",
            return_value=fake_client,
        ):
            result = _latest_publish_for_account(self.project_root, "default")
        # Latest of [2026-05-12, 2026-06-01] under account=default; the
        # x.json's 2030 must NOT win because it's a sidecar; "other" account
        # must NOT win either.
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 6)

    def test_returns_none_when_gcs_client_unavailable(self):
        from unittest.mock import patch
        os.environ["YTFACTORY_STATE_BUCKET"] = "test-bucket"
        with patch(
            "pipeline.upload.upload._gcs_storage_client",
            return_value=None,
        ):
            result = _latest_publish_for_account(self.project_root, "default")
        self.assertIsNone(result)

    def test_gcs_listing_failure_returns_none(self):
        from unittest.mock import patch
        os.environ["YTFACTORY_STATE_BUCKET"] = "test-bucket"
        fake_client = MagicMock()
        fake_client.list_blobs.side_effect = RuntimeError("permission denied")
        with patch(
            "pipeline.upload.upload._gcs_storage_client",
            return_value=fake_client,
        ):
            # Throttle defensive: bucket missing / IAM blocked ⇒ no
            # throttle (better than crashing the upload job).
            result = _latest_publish_for_account(self.project_root, "default")
        self.assertIsNone(result)

    def test_gcs_skips_malformed_json_records(self):
        from unittest.mock import patch
        os.environ["YTFACTORY_STATE_BUCKET"] = "test-bucket"
        bad = MagicMock()
        bad.name = "historyrecapped/uploads/bad.json"
        bad.download_as_text.return_value = "not json{{{"
        good = self._make_blob(
            "historyrecapped/uploads/good.json",
            {"account": "default", "uploaded_at": "2026-05-12T12:00:00Z"},
        )
        fake_client = MagicMock()
        fake_client.list_blobs.return_value = iter([bad, good])
        with patch(
            "pipeline.upload.upload._gcs_storage_client",
            return_value=fake_client,
        ):
            result = _latest_publish_for_account(self.project_root, "default")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)

    def test_gcs_skips_records_with_no_timestamps(self):
        from unittest.mock import patch
        os.environ["YTFACTORY_STATE_BUCKET"] = "test-bucket"
        no_ts = self._make_blob(
            "historyrecapped/uploads/no_ts.json",
            {"account": "default"},  # no publish_at + no uploaded_at
        )
        fake_client = MagicMock()
        fake_client.list_blobs.return_value = iter([no_ts])
        with patch(
            "pipeline.upload.upload._gcs_storage_client",
            return_value=fake_client,
        ):
            result = _latest_publish_for_account(self.project_root, "default")
        self.assertIsNone(result)

    def test_compute_throttled_uses_gcs_path_when_bucket_set(self):
        """End-to-end: compute_throttled_publish_at routes through the
        GCS variant and produces a future publish slot."""
        from unittest.mock import patch
        os.environ["YTFACTORY_STATE_BUCKET"] = "test-bucket"
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        fake_client = MagicMock()
        fake_client.list_blobs.return_value = iter([
            self._make_blob(
                "historyrecapped/uploads/recent.json",
                {"account": "default", "uploaded_at": recent},
            ),
        ])
        with patch(
            "pipeline.upload.upload._gcs_storage_client",
            return_value=fake_client,
        ):
            result = compute_throttled_publish_at(
                self.project_root, "default", now=now,
            )
        self.assertIsNotNone(result)
        self.assertIn("Z", result)


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

    def test_audit_q227_expired_access_token_surfaced(self):
        """Audit Q2.27 — pre-fix any token with refresh_token + scopes
        was reported as ``ok`` regardless of expiry. Now an expired
        access_token surfaces as ``state="expired"`` so the dashboard's
        signal is honest about needing a refresh on the next call.
        """
        p = self.config_dir / "youtube_token_default.json"
        p.write_text(json.dumps({
            "token": "tok",
            "refresh_token": "rt",
            "scopes": list(SCOPES),
            "expiry": "2020-01-01T00:00:00Z",  # long past
        }))
        result = inspect_token_status("default")
        self.assertEqual(result["state"], "expired")
        self.assertEqual(result["expiry"], "2020-01-01T00:00:00Z")

    def test_audit_q227_expiring_soon_access_token_surfaced(self):
        """Audit Q2.27 — token expiring within 60 s surfaces as
        ``expiring_soon`` so the operator knows to expect a Secret
        Manager version bump on the next call."""
        from datetime import datetime, timezone, timedelta
        soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        p = self.config_dir / "youtube_token_default.json"
        p.write_text(json.dumps({
            "token": "tok",
            "refresh_token": "rt",
            "scopes": list(SCOPES),
            "expiry": soon,
        }))
        result = inspect_token_status("default")
        self.assertEqual(result["state"], "expiring_soon")

    def test_audit_q227_far_future_expiry_is_ok(self):
        """Token with expiry > 60 s away → ``ok`` (no operator action)."""
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        p = self.config_dir / "youtube_token_default.json"
        p.write_text(json.dumps({
            "token": "tok",
            "refresh_token": "rt",
            "scopes": list(SCOPES),
            "expiry": future,
        }))
        result = inspect_token_status("default")
        self.assertEqual(result["state"], "ok")

    def test_audit_q227_unparseable_expiry_falls_back_to_ok(self):
        """Audit Q2.27 — tokens authored before the field was canonical
        may have a malformed expiry. Treat as ``ok`` rather than
        crashing the dashboard."""
        p = self.config_dir / "youtube_token_default.json"
        p.write_text(json.dumps({
            "token": "tok",
            "refresh_token": "rt",
            "scopes": list(SCOPES),
            "expiry": "not-a-real-date",
        }))
        result = inspect_token_status("default")
        self.assertEqual(result["state"], "ok")

    def test_audit_q227_missing_expiry_treated_as_ok(self):
        """No expiry field → no expiry check; falls through to ``ok``."""
        p = self.config_dir / "youtube_token_default.json"
        p.write_text(json.dumps({
            "token": "tok",
            "refresh_token": "rt",
            "scopes": list(SCOPES),
        }))
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


class TestFictionDisclosure(unittest.TestCase):
    """Phase 4c — DRAMATIZED FICTION transparency footer for LLM-only
    sources. Off by default; opt-in per channel via
    upload.fiction_disclosure: true."""

    def test_disabled_by_default(self):
        meta = derive_metadata(
            script={"hook": "h"}, raw=None,
            upload_cfg={"description_template": "base text"},
        )
        # raw is None → would be LLM-only, but disclosure off by default.
        self.assertNotIn("DRAMATIZED FICTION", meta["description"])

    def test_enabled_appends_when_raw_is_none(self):
        meta = derive_metadata(
            script={"hook": "h"}, raw=None,
            upload_cfg={
                "description_template": "base text",
                "fiction_disclosure": True,
            },
        )
        self.assertTrue(meta["description"].startswith("base text"))
        self.assertIn("DRAMATIZED FICTION", meta["description"])

    def test_enabled_appends_when_source_kind_is_llm(self):
        meta = derive_metadata(
            script={"hook": "h"},
            raw={"source_kind": "llm", "url": "https://something",
                 "body": "LLM-fabricated body"},
            upload_cfg={
                "description_template": "base text",
                "fiction_disclosure": True,
            },
        )
        # source_kind=llm wins regardless of url/body presence.
        self.assertIn("DRAMATIZED FICTION", meta["description"])

    def test_enabled_skips_when_real_reddit_url_present(self):
        # Real reddit_url source → no disclosure (it's a real story).
        meta = derive_metadata(
            script={"hook": "h"},
            raw={
                "source_kind": "reddit_url",
                "url": "https://reddit.com/r/aita/comments/abc",
                "body": "real reddit post body",
            },
            upload_cfg={
                "description_template": "base text",
                "fiction_disclosure": True,
            },
        )
        self.assertNotIn("DRAMATIZED FICTION", meta["description"])

    def test_enabled_appends_when_url_and_body_both_empty(self):
        # Defensive — legacy raw dict without source_kind set, but
        # missing both url + body. Treat as LLM-only.
        meta = derive_metadata(
            script={"hook": "h"},
            raw={"some_other_field": "value"},
            upload_cfg={
                "description_template": "base text",
                "fiction_disclosure": True,
            },
        )
        self.assertIn("DRAMATIZED FICTION", meta["description"])

    def test_custom_disclosure_text_used(self):
        custom = "\n\n--- Custom transparency text ---"
        meta = derive_metadata(
            script={"hook": "h"}, raw=None,
            upload_cfg={
                "description_template": "base text",
                "fiction_disclosure": True,
                "fiction_disclosure_text": custom,
            },
        )
        self.assertIn("Custom transparency text", meta["description"])
        self.assertNotIn("DRAMATIZED FICTION", meta["description"])

    def test_empty_disclosure_text_disables(self):
        # Empty string → no append even when fiction_disclosure=True.
        meta = derive_metadata(
            script={"hook": "h"}, raw=None,
            upload_cfg={
                "description_template": "base text",
                "fiction_disclosure": True,
                "fiction_disclosure_text": "",
            },
        )
        self.assertEqual(meta["description"], "base text")

    def test_disclosure_does_not_exceed_5000_char_cap(self):
        # If base description + disclosure would exceed 5000 chars,
        # the truncation still happens (description[:5000]).
        meta = derive_metadata(
            script={"hook": "h"}, raw=None,
            upload_cfg={"fiction_disclosure": True},
            description_override="X" * 4990,
        )
        # Cap holds; disclosure truncated as needed.
        self.assertLessEqual(len(meta["description"]), 5000)


class TestIsLlmOnlySource(unittest.TestCase):

    def test_none_raw_is_llm_only(self):
        from pipeline.upload.upload import _is_llm_only_source
        self.assertTrue(_is_llm_only_source(None))

    def test_empty_raw_is_llm_only(self):
        from pipeline.upload.upload import _is_llm_only_source
        self.assertTrue(_is_llm_only_source({}))

    def test_explicit_llm_source_kind(self):
        from pipeline.upload.upload import _is_llm_only_source
        self.assertTrue(_is_llm_only_source({"source_kind": "llm"}))
        self.assertTrue(_is_llm_only_source({"source_kind": "LLM"}))

    def test_real_reddit_url_is_not_llm_only(self):
        from pipeline.upload.upload import _is_llm_only_source
        self.assertFalse(_is_llm_only_source({
            "source_kind": "reddit_url",
            "url": "https://reddit.com/r/aita/comments/abc",
            "body": "post body",
        }))

    def test_url_present_with_no_source_kind_is_not_llm_only(self):
        from pipeline.upload.upload import _is_llm_only_source
        self.assertFalse(_is_llm_only_source({
            "url": "https://example.com",
        }))

    def test_body_present_with_no_url_is_not_llm_only(self):
        from pipeline.upload.upload import _is_llm_only_source
        self.assertFalse(_is_llm_only_source({"body": "real story body"}))

    def test_neither_url_nor_body_is_llm_only(self):
        from pipeline.upload.upload import _is_llm_only_source
        self.assertTrue(_is_llm_only_source({"some_field": "value"}))


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
    def test_403_quota_exceeded_raises_typed_QuotaExceededError(
        self, mock_build, mock_media, mock_auth,
    ):
        # Audit T1.10 — 403 quotaExceeded must surface as the typed
        # QuotaExceededError so the caller can defer the slug until
        # the YouTube quota window resets, instead of treating it as
        # a generic auth failure.
        from pipeline.upload.upload import QuotaExceededError
        mp4 = self._mk_mp4()
        yt = _make_fake_youtube()
        yt.videos.return_value.insert.return_value.next_chunk.side_effect = (
            _make_quota_http_error("quotaExceeded")
        )
        mock_build.return_value = yt
        with self.assertRaises(QuotaExceededError) as ctx:
            youtube_upload(mp4, title="T", description="D", tags=[])
        self.assertEqual(ctx.exception.reason, "quotaExceeded")

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_403_other_quota_reasons_also_raise_QuotaExceededError(
        self, mock_build, mock_media, mock_auth,
    ):
        from pipeline.upload.upload import QuotaExceededError
        for reason in ("rateLimitExceeded", "userRateLimitExceeded",
                       "uploadLimitExceeded", "dailyLimitExceeded"):
            with self.subTest(reason=reason):
                mp4 = self._mk_mp4()
                yt = _make_fake_youtube()
                yt.videos.return_value.insert.return_value.next_chunk.side_effect = (
                    _make_quota_http_error(reason)
                )
                mock_build.return_value = yt
                with self.assertRaises(QuotaExceededError) as ctx:
                    youtube_upload(mp4, title="T", description="D", tags=[])
                self.assertEqual(ctx.exception.reason, reason)

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("googleapiclient.discovery.build")
    def test_403_non_quota_reason_stays_generic_UploadError(
        self, mock_build, mock_media, mock_auth,
    ):
        # 403 with a non-quota reason (e.g. forbidden / disabled)
        # should NOT be misclassified as quota-exhaustion.
        from pipeline.upload.upload import QuotaExceededError
        mp4 = self._mk_mp4()
        yt = _make_fake_youtube()
        body = json.dumps({
            "error": {"errors": [{"reason": "forbidden"}]},
        }).encode()
        yt.videos.return_value.insert.return_value.next_chunk.side_effect = (
            _make_http_error(403, content=body)
        )
        mock_build.return_value = yt
        with self.assertRaises(UploadError) as ctx:
            youtube_upload(mp4, title="T", description="D", tags=[])
        self.assertNotIsInstance(ctx.exception, QuotaExceededError)


class TestHttpErrorQuotaReason(_UploadTestBase):
    """Audit T1.10 — direct unit tests for _http_error_quota_reason
    helper that distinguishes quota-class 403s from other 403s."""

    def test_non_403_returns_none(self):
        from pipeline.upload.upload import _http_error_quota_reason
        self.assertIsNone(_http_error_quota_reason(_make_http_error(500)))

    def test_403_with_quota_reason_returns_reason(self):
        from pipeline.upload.upload import _http_error_quota_reason
        err = _make_quota_http_error("rateLimitExceeded")
        self.assertEqual(
            _http_error_quota_reason(err), "rateLimitExceeded",
        )

    def test_403_non_json_body_returns_none(self):
        # Defensive: body that doesn't parse as JSON ≠ quota.
        from pipeline.upload.upload import _http_error_quota_reason
        err = _make_http_error(403, content=b"not json {{{")
        self.assertIsNone(_http_error_quota_reason(err))

    def test_403_json_with_no_errors_array_returns_none(self):
        from pipeline.upload.upload import _http_error_quota_reason
        body = json.dumps({"error": {"code": 403}}).encode()
        err = _make_http_error(403, content=body)
        self.assertIsNone(_http_error_quota_reason(err))

    def test_403_json_unknown_reason_returns_none(self):
        from pipeline.upload.upload import _http_error_quota_reason
        body = json.dumps({
            "error": {"errors": [{"reason": "youtubeSignupRequired"}]},
        }).encode()
        err = _make_http_error(403, content=body)
        self.assertIsNone(_http_error_quota_reason(err))


    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("pipeline.upload.upload.time")
    @patch("googleapiclient.discovery.build")
    def test_socket_timeout_retries_then_succeeds(
        self, mock_build, mock_time, mock_media, mock_auth,
    ):
        # Audit Q2.28 — socket.timeout from next_chunk() must retry
        # via the resumable infrastructure, not crash the upload.
        import socket as _socket
        mp4 = self._mk_mp4()
        yt = _make_fake_youtube()
        yt.videos.return_value.insert.return_value.next_chunk.side_effect = [
            _socket.timeout("read timed out"),
            (None, {"id": "vid_socket", "kind": "youtube#video", "etag": "e"}),
        ]
        mock_build.return_value = yt
        result = youtube_upload(mp4, title="T", description="D", tags=[])
        self.assertEqual(result["video_id"], "vid_socket")
        mock_time.sleep.assert_called()

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("pipeline.upload.upload.time")
    @patch("googleapiclient.discovery.build")
    def test_connection_error_retries_then_succeeds(
        self, mock_build, mock_time, mock_media, mock_auth,
    ):
        mp4 = self._mk_mp4()
        yt = _make_fake_youtube()
        yt.videos.return_value.insert.return_value.next_chunk.side_effect = [
            ConnectionResetError("connection reset by peer"),
            (None, {"id": "vid_conn", "kind": "youtube#video", "etag": "e"}),
        ]
        mock_build.return_value = yt
        result = youtube_upload(mp4, title="T", description="D", tags=[])
        self.assertEqual(result["video_id"], "vid_conn")
        mock_time.sleep.assert_called()

    @patch("pipeline.upload.upload.authenticate")
    @patch("googleapiclient.http.MediaFileUpload")
    @patch("pipeline.upload.upload.time")
    @patch("googleapiclient.discovery.build")
    def test_socket_timeout_exceeds_backoff_raises(
        self, mock_build, mock_time, mock_media, mock_auth,
    ):
        # 7 retries with backoff 1,2,4,8,16,32,64 → next would be
        # 128 > 64, so eventually raise.
        import socket as _socket
        mp4 = self._mk_mp4()
        yt = _make_fake_youtube()
        yt.videos.return_value.insert.return_value.next_chunk.side_effect = [
            _socket.timeout("timeout"),
        ] * 8
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
