"""Tests for pipeline/upload/x_upload.py — full branch coverage.

NO real X/Twitter API calls. All external dependencies mocked.
tweepy is mocked entirely via patch.dict on sys.modules.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch, patch as mock_patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline.upload.x_upload as x_mod
from pipeline.upload.x_upload import (
    TWEET_DEFAULT_MAX_CHARS,
    XUploadError,
    _credentials_path,
    _record_path,
    derive_tweet,
    existing_post,
    load_credentials,
    post_short,
    render_tweet_text,
    write_post_record,
    x_post,
    _cli,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_CREDS = {
    "consumer_key": "ck",
    "consumer_secret": "cs",
    "access_token": "at",
    "access_token_secret": "ats",
    "handle": "myhandle",
}


def _make_tweepy_mock(
    *,
    media_id=12345,
    processing_info=None,
    tweet_id="tweet99",
):
    """Build a minimal tweepy mock for x_post tests."""
    tweepy = MagicMock()

    # Media object from chunked_upload
    media = MagicMock()
    media.media_id = media_id
    media.media_id_string = str(media_id)
    media.processing_info = processing_info
    tweepy.API.return_value.chunked_upload.return_value = media
    tweepy.API.return_value.get_media_upload_status.return_value = MagicMock(
        processing_info={"state": "succeeded", "progress_percent": 100}
    )

    # Tweet response
    resp = MagicMock()
    resp.data = {"id": tweet_id}
    tweepy.Client.return_value.create_tweet.return_value = resp

    return tweepy


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class _XUploadTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.config_dir = self.tmpdir / "config"
        self.config_dir.mkdir()
        self.project_root = self.tmpdir / "project"
        self.project_root.mkdir()

        self._patches = [
            patch("pipeline.upload.x_upload.CONFIG_DIR", self.config_dir),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _mk_channel(self, name="testchan") -> Path:
        d = self.project_root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(f"name: {name}\n")
        return d

    def _mk_mp4(self, name="test.mp4") -> Path:
        p = self.tmpdir / name
        p.write_bytes(b"\x00" * 1024)
        return p

    def _write_creds(self, account="default", **overrides) -> Path:
        creds = dict(_VALID_CREDS)
        creds.update(overrides)
        safe = "".join(c for c in account if c.isalnum() or c in "-_") or "default"
        p = self.config_dir / f"x_credentials_{safe}.json"
        p.write_text(json.dumps(creds))
        return p


# ===========================================================================
# 1.  _credentials_path / load_credentials
# ===========================================================================

class TestCredentials(_XUploadTestBase):

    def test_credentials_path_safe_chars(self):
        p = _credentials_path("my-account_1")
        self.assertIn("x_credentials_my-account_1", str(p))

    def test_credentials_path_strips_unsafe(self):
        p = _credentials_path("my@account!")
        self.assertIn("x_credentials_myaccount", str(p))

    def test_credentials_path_empty_becomes_default(self):
        p = _credentials_path("!@#")
        self.assertIn("x_credentials_default", str(p))

    def test_load_credentials_missing_file(self):
        with self.assertRaises(XUploadError) as ctx:
            load_credentials("missing")
        self.assertIn("Missing", str(ctx.exception))

    def test_load_credentials_bad_json(self):
        p = self.config_dir / "x_credentials_bad.json"
        p.write_text("{{{not json")
        with self.assertRaises(XUploadError) as ctx:
            load_credentials("bad")
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_load_credentials_missing_keys(self):
        p = self.config_dir / "x_credentials_incomplete.json"
        p.write_text(json.dumps({"consumer_key": "ck"}))
        with self.assertRaises(XUploadError) as ctx:
            load_credentials("incomplete")
        self.assertIn("missing keys", str(ctx.exception))

    def test_load_credentials_success(self):
        self._write_creds()
        result = load_credentials("default")
        self.assertEqual(result["consumer_key"], "ck")
        self.assertEqual(result["handle"], "myhandle")


# ===========================================================================
# 2.  _record_path / existing_post / write_post_record
# ===========================================================================

class TestRecordKeeping(_XUploadTestBase):

    def test_existing_post_not_found(self):
        self._mk_channel()
        result = existing_post(self.project_root, "testchan", "slug1")
        self.assertIsNone(result)

    def test_existing_post_found_with_tweet_id(self):
        chan = self._mk_channel()
        uploads = chan / "uploads"
        uploads.mkdir()
        (uploads / "slug1.x.json").write_text(json.dumps({
            "tweet_id": "t123",
            "url": "https://x.com/h/status/t123",
        }))
        result = existing_post(self.project_root, "testchan", "slug1")
        self.assertIsNotNone(result)
        self.assertEqual(result["tweet_id"], "t123")

    def test_existing_post_found_but_no_tweet_id(self):
        chan = self._mk_channel()
        uploads = chan / "uploads"
        uploads.mkdir()
        (uploads / "slug1.x.json").write_text(json.dumps({"url": "https://x.com/..."}))
        result = existing_post(self.project_root, "testchan", "slug1")
        self.assertIsNone(result)

    def test_existing_post_bad_json(self):
        chan = self._mk_channel()
        uploads = chan / "uploads"
        uploads.mkdir()
        (uploads / "slug1.x.json").write_text("{{{invalid")
        result = existing_post(self.project_root, "testchan", "slug1")
        self.assertIsNone(result)

    def test_write_post_record(self):
        self._mk_channel()
        record = {"tweet_id": "t456", "url": "https://x.com/h/status/t456"}
        p = write_post_record(self.project_root, "testchan", "slug1", record)
        self.assertTrue(p.exists())
        self.assertEqual(json.loads(p.read_text())["tweet_id"], "t456")


# ===========================================================================
# 3.  render_tweet_text / derive_tweet
# ===========================================================================

class TestTweetText(unittest.TestCase):

    def test_script_token(self):
        result = render_tweet_text("{script.hook}", script={"hook": "Big Hook"}, raw=None)
        self.assertEqual(result, "Big Hook")

    def test_raw_token(self):
        result = render_tweet_text("{raw.url}", script={}, raw={"url": "https://reddit.com/r/aita"})
        self.assertEqual(result, "https://reddit.com/r/aita")

    def test_missing_key_is_empty(self):
        result = render_tweet_text("{script.missing}", script={}, raw=None)
        self.assertEqual(result, "")

    def test_unknown_root_is_empty(self):
        result = render_tweet_text("{other.key}", script={}, raw=None)
        self.assertEqual(result, "")

    def test_list_access(self):
        result = render_tweet_text("{script.tags.1}", script={"tags": ["a", "b", "c"]}, raw=None)
        self.assertEqual(result, "b")

    def test_list_out_of_range(self):
        result = render_tweet_text("{script.tags.99}", script={"tags": ["a"]}, raw=None)
        self.assertEqual(result, "")

    def test_list_non_int_key(self):
        result = render_tweet_text("{script.tags.notanint}", script={"tags": ["a"]}, raw=None)
        self.assertEqual(result, "")

    def test_none_value_is_empty(self):
        result = render_tweet_text("{script.hook}", script={"hook": None}, raw=None)
        self.assertEqual(result, "")

    def test_raw_none_is_empty(self):
        result = render_tweet_text("{raw.url}", script={}, raw=None)
        self.assertEqual(result, "")

    def test_traverse_into_scalar_is_empty(self):
        result = render_tweet_text("{script.x.y}", script={"x": "scalar"}, raw=None)
        self.assertEqual(result, "")

    def test_derive_tweet_text_override(self):
        result = derive_tweet(
            script={}, raw=None, x_cfg={},
            text_override="Override text!",
        )
        self.assertEqual(result["text"], "Override text!")

    def test_derive_tweet_template(self):
        result = derive_tweet(
            script={"hook": "Hook"}, raw=None,
            x_cfg={"tweet_template": "{script.hook} #shorts"},
        )
        self.assertIn("Hook", result["text"])
        self.assertIn("#shorts", result["text"])

    def test_derive_tweet_fallback_to_title_options(self):
        result = derive_tweet(
            script={"title_options": ["Best Title", "Alt Title"]},
            raw=None, x_cfg={},
        )
        self.assertIn("Best Title", result["text"])

    def test_derive_tweet_fallback_to_hook(self):
        result = derive_tweet(
            script={"hook": "My Hook"}, raw=None, x_cfg={},
        )
        self.assertIn("My Hook", result["text"])

    def test_derive_tweet_fallback_to_slug(self):
        result = derive_tweet(
            script={"slug": "my-slug"}, raw=None, x_cfg={},
        )
        self.assertIn("my-slug", result["text"])

    def test_derive_tweet_truncation(self):
        long_text = "A" * 400
        result = derive_tweet(script={}, raw=None, x_cfg={}, text_override=long_text)
        self.assertLessEqual(len(result["text"]), TWEET_DEFAULT_MAX_CHARS)
        self.assertTrue(result["text"].endswith("…"))

    def test_derive_tweet_collapses_triple_newlines(self):
        text = "Line 1\n\n\n\nLine 2"
        result = derive_tweet(script={}, raw=None, x_cfg={}, text_override=text)
        self.assertNotIn("\n\n\n", result["text"])

    def test_derive_tweet_max_chars_override(self):
        result = derive_tweet(
            script={}, raw=None,
            x_cfg={"max_chars": 50},
            text_override="A" * 100,
        )
        self.assertLessEqual(len(result["text"]), 50)
        self.assertEqual(result["max_chars"], 50)

    def test_derive_tweet_strips_whitespace(self):
        result = derive_tweet(script={}, raw=None, x_cfg={}, text_override="  hello  ")
        self.assertEqual(result["text"], "hello")


# ===========================================================================
# 4.  x_post
# ===========================================================================

class TestXPost(_XUploadTestBase):

    def test_tweepy_not_installed_raises(self):
        """ImportError for tweepy → XUploadError."""
        mp4 = self._mk_mp4()
        self._write_creds()

        with patch.dict("sys.modules", {"tweepy": None}):
            with self.assertRaises(XUploadError) as ctx:
                x_post(mp4, text="Hello", account="default")
        self.assertIn("tweepy", str(ctx.exception))

    def test_mp4_not_found_raises(self):
        tweepy_mock = _make_tweepy_mock()
        with patch.dict("sys.modules", {"tweepy": tweepy_mock}):
            with self.assertRaises(XUploadError):
                x_post(self.tmpdir / "missing.mp4", text="Hello", account="default")

    def test_success_no_processing_info(self):
        """Successful upload where media has no processing_info (no wait loop)."""
        mp4 = self._mk_mp4()
        self._write_creds()
        tweepy_mock = _make_tweepy_mock(media_id=111, processing_info=None, tweet_id="tid1")

        with patch.dict("sys.modules", {"tweepy": tweepy_mock}):
            result = x_post(mp4, text="Test tweet", account="default")

        self.assertEqual(result["tweet_id"], "tid1")
        self.assertIn("myhandle", result["url"])
        self.assertEqual(result["account"], "default")

    def test_success_processing_info_succeeded(self):
        """Media has processing_info already succeeded → no polling loop."""
        mp4 = self._mk_mp4()
        self._write_creds()
        proc = {"state": "succeeded", "progress_percent": 100}
        tweepy_mock = _make_tweepy_mock(media_id=222, processing_info=proc, tweet_id="tid2")

        with patch.dict("sys.modules", {"tweepy": tweepy_mock}):
            result = x_post(mp4, text="Test tweet", account="default")
        self.assertEqual(result["tweet_id"], "tid2")

    def test_processing_info_pending_then_succeeded(self):
        """processing_info.state='pending' → poll until 'succeeded'."""
        mp4 = self._mk_mp4()
        self._write_creds()

        proc_initial = {"state": "pending", "check_after_secs": 1}
        tweepy_mock = _make_tweepy_mock(media_id=333, processing_info=proc_initial, tweet_id="tid3")

        # get_media_upload_status: first 'in_progress', then 'succeeded'
        status1 = MagicMock()
        status1.processing_info = {"state": "in_progress", "progress_percent": 50}
        status2 = MagicMock()
        status2.processing_info = {"state": "succeeded", "progress_percent": 100}
        tweepy_mock.API.return_value.get_media_upload_status.side_effect = [status1, status2]

        progress_vals = []
        with patch.dict("sys.modules", {"tweepy": tweepy_mock}):
            with patch("time.sleep"):
                with patch("time.time", side_effect=[0, 0, 60, 60]):
                    result = x_post(
                        mp4, text="Test", account="default",
                        progress_cb=lambda pct: progress_vals.append(pct),
                    )
        self.assertEqual(result["tweet_id"], "tid3")

    def test_processing_info_failed_raises(self):
        """processing_info.state='failed' → XUploadError."""
        mp4 = self._mk_mp4()
        self._write_creds()

        proc_initial = {"state": "pending", "check_after_secs": 1}
        tweepy_mock = _make_tweepy_mock(media_id=444, processing_info=proc_initial)

        status_failed = MagicMock()
        status_failed.processing_info = {"state": "failed", "error": {"message": "transcode error"}}
        tweepy_mock.API.return_value.get_media_upload_status.return_value = status_failed

        with patch.dict("sys.modules", {"tweepy": tweepy_mock}):
            with patch("time.sleep"):
                with patch("time.time", side_effect=[0, 0, 60]):
                    with self.assertRaises(XUploadError) as ctx:
                        x_post(mp4, text="Test", account="default")
        self.assertIn("transcode failed", str(ctx.exception))

    def test_processing_info_timeout_raises(self):
        """Processing loop times out after 3 minutes → XUploadError."""
        mp4 = self._mk_mp4()
        self._write_creds()

        proc_initial = {"state": "pending", "check_after_secs": 5}
        tweepy_mock = _make_tweepy_mock(media_id=555, processing_info=proc_initial)

        status_pending = MagicMock()
        status_pending.processing_info = {"state": "in_progress", "progress_percent": 10}
        tweepy_mock.API.return_value.get_media_upload_status.return_value = status_pending

        # Simulate time past deadline: deadline=0+180=180, check: 200 > 180 → loop exits
        with patch.dict("sys.modules", {"tweepy": tweepy_mock}):
            with patch("time.sleep"):
                with patch("time.time", side_effect=[0, 200]):
                    with self.assertRaises(XUploadError) as ctx:
                        x_post(mp4, text="Test", account="default")
        self.assertIn("timed out", str(ctx.exception))

    def test_no_media_id_raises(self):
        """chunked_upload returns media with no media_id → XUploadError."""
        mp4 = self._mk_mp4()
        self._write_creds()
        tweepy_mock = _make_tweepy_mock()
        # Override media to have no media_id
        media = MagicMock()
        media.media_id = None
        media.media_id_string = None
        media.processing_info = None
        tweepy_mock.API.return_value.chunked_upload.return_value = media

        with patch.dict("sys.modules", {"tweepy": tweepy_mock}):
            with self.assertRaises(XUploadError) as ctx:
                x_post(mp4, text="Test", account="default")
        self.assertIn("no media_id", str(ctx.exception))

    def test_no_tweet_id_raises(self):
        """create_tweet returns no id → XUploadError."""
        mp4 = self._mk_mp4()
        self._write_creds()
        tweepy_mock = _make_tweepy_mock()
        # Override tweet response to have no id
        resp = MagicMock()
        resp.data = {}
        tweepy_mock.Client.return_value.create_tweet.return_value = resp

        with patch.dict("sys.modules", {"tweepy": tweepy_mock}):
            with self.assertRaises(XUploadError) as ctx:
                x_post(mp4, text="Test", account="default")
        self.assertIn("no id", str(ctx.exception))

    def test_no_handle_uses_i(self):
        """No 'handle' key in creds → uses 'i' in URL."""
        mp4 = self._mk_mp4()
        creds = dict(_VALID_CREDS)
        del creds["handle"]
        p = self.config_dir / "x_credentials_nohndl.json"
        p.write_text(json.dumps(creds))

        tweepy_mock = _make_tweepy_mock(tweet_id="tid_nh")
        with patch.dict("sys.modules", {"tweepy": tweepy_mock}):
            result = x_post(mp4, text="Test", account="nohndl")
        self.assertIn("/i/status/", result["url"])

    def test_progress_callback_exception_ignored(self):
        """progress_cb raising is swallowed."""
        mp4 = self._mk_mp4()
        self._write_creds()

        proc_initial = {"state": "pending", "check_after_secs": 1}
        tweepy_mock = _make_tweepy_mock(media_id=666, processing_info=proc_initial, tweet_id="tid_cb")

        status_ok = MagicMock()
        status_ok.processing_info = {"state": "succeeded"}
        tweepy_mock.API.return_value.get_media_upload_status.return_value = status_ok

        def _bad_cb(_):
            raise RuntimeError("callback error")

        with patch.dict("sys.modules", {"tweepy": tweepy_mock}):
            with patch("time.sleep"):
                with patch("time.time", side_effect=[0, 1, 10]):
                    result = x_post(mp4, text="Test", account="default", progress_cb=_bad_cb)
        self.assertEqual(result["tweet_id"], "tid_cb")


# ===========================================================================
# 5.  post_short
# ===========================================================================

class TestPostShort(_XUploadTestBase):

    def _base_call(self, **overrides):
        defaults = dict(
            project_root=self.project_root,
            channel_yaml={"x": {"account": "default", "enabled": True}},
            channel_dir="testchan",
            slug="slug1",
            mp4_path=self._mk_mp4(),
            script={"hook": "Great hook"},
            raw=None,
        )
        defaults.update(overrides)
        return defaults

    def test_no_x_config_raises(self):
        args = self._base_call()
        args["channel_yaml"] = {}  # no 'x' block
        with self.assertRaises(XUploadError) as ctx:
            post_short(**args)
        self.assertIn("no `x:` block", str(ctx.exception))

    def test_disabled_raises(self):
        args = self._base_call()
        args["channel_yaml"] = {"x": {"enabled": False}}
        with self.assertRaises(XUploadError) as ctx:
            post_short(**args)
        self.assertIn("x.enabled is false", str(ctx.exception))

    def test_existing_post_returns_cached(self):
        """Idempotency: if slug already posted, return cached record."""
        chan = self._mk_channel()
        uploads = chan / "uploads"
        uploads.mkdir()
        (uploads / "slug1.x.json").write_text(json.dumps({
            "tweet_id": "t_old",
            "url": "https://x.com/h/status/t_old",
        }))
        result = post_short(**self._base_call())
        self.assertEqual(result["tweet_id"], "t_old")

    def test_dry_run_returns_preview(self):
        self._write_creds()
        result = post_short(**self._base_call(dry_run=True))
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["slug"], "slug1")
        self.assertEqual(result["channel_dir"], "testchan")

    def test_force_skips_idempotency(self):
        """force=True skips the existing-post check."""
        chan = self._mk_channel()
        uploads = chan / "uploads"
        uploads.mkdir()
        (uploads / "slug1.x.json").write_text(json.dumps({
            "tweet_id": "t_old",
            "url": "https://x.com/h/status/t_old",
        }))
        self._write_creds()

        tweepy_mock = _make_tweepy_mock(tweet_id="t_new")
        with patch.dict("sys.modules", {"tweepy": tweepy_mock}):
            result = post_short(**self._base_call(force=True))
        self.assertEqual(result["tweet_id"], "t_new")

    @patch("pipeline.upload.x_upload.x_post")
    @patch("pipeline.upload.x_upload.write_post_record")
    def test_full_success_writes_record(self, mock_wr, mock_xpost):
        mock_xpost.return_value = {
            "tweet_id": "t_full",
            "url": "https://x.com/h/status/t_full",
        }
        self._mk_channel()
        result = post_short(**self._base_call())
        self.assertEqual(result["tweet_id"], "t_full")
        mock_wr.assert_called_once()
        # record should have extra fields added
        rec_arg = mock_wr.call_args[0][3]
        self.assertEqual(rec_arg["slug"], "slug1")
        self.assertEqual(rec_arg["channel_dir"], "testchan")

    @patch("pipeline.upload.x_upload.x_post")
    @patch("pipeline.upload.x_upload.write_post_record")
    def test_account_from_x_config(self, mock_wr, mock_xpost):
        mock_xpost.return_value = {"tweet_id": "t_acct", "url": "u"}
        self._mk_channel()
        channel_yaml = {"x": {"account": "myaccount", "enabled": True}}
        args = self._base_call(channel_yaml=channel_yaml)
        result = post_short(**args)
        # x_post should have been called with account="myaccount"
        mock_xpost.assert_called_once()
        self.assertEqual(mock_xpost.call_args.kwargs["account"], "myaccount")

    @patch("pipeline.upload.x_upload.x_post")
    @patch("pipeline.upload.x_upload.write_post_record")
    def test_text_override_used(self, mock_wr, mock_xpost):
        mock_xpost.return_value = {"tweet_id": "t_ov", "url": "u"}
        self._mk_channel()
        post_short(**self._base_call(text_override="Custom tweet text"))
        # derive_tweet was called with text_override; x_post gets that text
        mock_xpost.assert_called_once()
        called_text = mock_xpost.call_args.kwargs["text"]
        self.assertEqual(called_text, "Custom tweet text")


# ===========================================================================
# 6.  _cli
# ===========================================================================

class TestCli(_XUploadTestBase):

    def _mock_render_paths(self, *, config_yaml_exists=True, script_exists=True):
        mock_paths = MagicMock()
        mock_paths.config_yaml.exists.return_value = config_yaml_exists
        mock_paths.config_yaml.read_text.return_value = "x:\n  enabled: true\n"
        mock_paths.short_for.return_value = self._mk_mp4()
        if script_exists:
            script_path = self.tmpdir / "script.json"
            script_path.write_text(json.dumps({"hook": "CLI hook"}))
            mock_paths.narration_for.return_value = script_path
        else:
            mock_paths.narration_for.return_value = self.tmpdir / "missing_script.json"
        raw_path = self.tmpdir / "raw.json"
        if raw_path.exists():
            mock_paths.raw_for.return_value = raw_path
        else:
            mock_paths.raw_for.return_value = self.tmpdir / "no_raw.json"
        return mock_paths

    def test_cli_missing_config_yaml_returns_2(self):
        with patch("sys.argv", ["x_upload", "--channel", "testchan", "--slug", "slug1"]):
            with patch("pipeline.paths.RenderPaths") as mock_rp_cls:
                mock_rp_cls.from_channel_dir.return_value = self._mock_render_paths(
                    config_yaml_exists=False
                )
                result = _cli()
        self.assertEqual(result, 2)

    def test_cli_missing_script_returns_2(self):
        with patch("sys.argv", ["x_upload", "--channel", "testchan", "--slug", "slug1"]):
            with patch("pipeline.paths.RenderPaths") as mock_rp_cls:
                with patch("yaml.safe_load", return_value={"x": {"enabled": True}}):
                    mock_rp_cls.from_channel_dir.return_value = self._mock_render_paths(
                        script_exists=False
                    )
                    result = _cli()
        self.assertEqual(result, 2)

    def test_cli_upload_error_returns_1(self):
        with patch("sys.argv", ["x_upload", "--channel", "testchan", "--slug", "slug1"]):
            with patch("pipeline.paths.RenderPaths") as mock_rp_cls:
                paths = self._mock_render_paths()
                mock_rp_cls.from_channel_dir.return_value = paths
                with patch("yaml.safe_load", return_value={"x": {"enabled": True}}):
                    with patch("pipeline.upload.x_upload.post_short",
                               side_effect=XUploadError("upload failed")):
                        result = _cli()
        self.assertEqual(result, 1)

    def test_cli_success_returns_0(self):
        with patch("sys.argv", ["x_upload", "--channel", "testchan", "--slug", "slug1",
                                "--dry-run"]):
            with patch("pipeline.paths.RenderPaths") as mock_rp_cls:
                paths = self._mock_render_paths()
                mock_rp_cls.from_channel_dir.return_value = paths
                with patch("yaml.safe_load", return_value={"x": {"enabled": True}}):
                    with patch("pipeline.upload.x_upload.post_short",
                               return_value={"dry_run": True, "slug": "slug1"}):
                        result = _cli()
        self.assertEqual(result, 0)

    def test_cli_with_mp4_override(self):
        mp4 = self._mk_mp4("override.mp4")
        with patch("sys.argv", ["x_upload", "--channel", "testchan", "--slug", "slug1",
                                "--mp4", str(mp4), "--force"]):
            with patch("pipeline.paths.RenderPaths") as mock_rp_cls:
                paths = self._mock_render_paths()
                mock_rp_cls.from_channel_dir.return_value = paths
                with patch("yaml.safe_load", return_value={"x": {"enabled": True}}):
                    with patch("pipeline.upload.x_upload.post_short",
                               return_value={"tweet_id": "t1", "url": "u"}) as mock_ps:
                        result = _cli()
        self.assertEqual(result, 0)
        # --force should be passed
        self.assertTrue(mock_ps.call_args.kwargs.get("force"))

    def test_cli_with_raw_file(self):
        """When raw.json exists, it should be loaded and passed."""
        raw_path = self.tmpdir / "raw.json"
        raw_path.write_text(json.dumps({"url": "http://reddit.com/r/test"}))

        with patch("sys.argv", ["x_upload", "--channel", "testchan", "--slug", "slug1"]):
            with patch("pipeline.paths.RenderPaths") as mock_rp_cls:
                paths = self._mock_render_paths()
                paths.raw_for.return_value = raw_path
                mock_rp_cls.from_channel_dir.return_value = paths
                with patch("yaml.safe_load", return_value={"x": {"enabled": True}}):
                    with patch("pipeline.upload.x_upload.post_short",
                               return_value={"tweet_id": "t2", "url": "u"}) as mock_ps:
                        result = _cli()
        self.assertEqual(result, 0)
        self.assertIsNotNone(mock_ps.call_args.kwargs.get("raw"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
