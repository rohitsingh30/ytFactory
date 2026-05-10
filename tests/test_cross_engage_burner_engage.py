"""Tests for pipeline.cross_engage.burner_engage — 100% line coverage."""
from __future__ import annotations

import json
import pathlib
import sys
import time
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests._helpers import FakePath, make_fake_playwright

import pipeline.cross_engage.burner_engage as _mod
from pipeline.cross_engage.burner_engage import (
    DATA_DIR,
    EngageState,
    PROFILE_MAP_PATH,
    SECRETS_DIR,
    TOKEN_DIR,
    VideoState,
    _account_email_for_token,
    _bump_action,
    _channel_ids_registry,
    _click_like,
    _click_subscribe,
    _email_to_chrome_profile,
    _human_pause,
    _maybe_stop,
    _now,
    _resolve_channel_ids,
    _resolve_profile_map,
    _resolve_token_path,
    _save_state,
    _secret_value,
    _stage_profile_copy,
    _state_path,
    _stop_path,
    is_running,
    list_burner_channels,
    read_state,
    request_stop,
)


# ── small path helpers ──────────────────────────────────────────────────────

class TestSecretValue(unittest.TestCase):
    def test_returns_expected_path(self):
        p = _secret_value("my-secret")
        self.assertTrue(str(p).endswith("my-secret/value"))


class TestResolveTokenPath(unittest.TestCase):
    def test_secret_exists(self):
        fake_sec = FakePath("value", exists=True)
        with patch.object(_mod, "SECRETS_DIR", FakePath("secrets")):
            # build the chain: SECRETS_DIR / "youtube-token-myslug" / "value"
            sec_dir = FakePath("secrets")
            slug_dir = FakePath("youtube-token-myslug")
            slug_dir._children["value"] = FakePath("value", exists=True)
            sec_dir._children["youtube-token-myslug"] = slug_dir
            with patch.object(_mod, "SECRETS_DIR", sec_dir):
                result = _resolve_token_path("myslug")
        self.assertIsNotNone(result)

    def test_local_exists(self):
        local_dir = FakePath(".config/ytfactory")
        local_dir._children["youtube_token_myslug.json"] = FakePath(
            "youtube_token_myslug.json", exists=True
        )
        with patch.object(_mod, "SECRETS_DIR", FakePath("secrets", exists=False)):
            with patch.object(_mod, "TOKEN_DIR", local_dir):
                result = _resolve_token_path("myslug")
        self.assertIsNotNone(result)

    def test_neither_exists(self):
        with patch.object(_mod, "SECRETS_DIR", FakePath("secrets", exists=False)), \
             patch.object(_mod, "TOKEN_DIR", FakePath("token", exists=False)):
            result = _resolve_token_path("nope")
        self.assertIsNone(result)


class TestResolveProfileMap(unittest.TestCase):
    def test_secret_exists(self):
        sec_dir = FakePath("secrets")
        pm_dir = FakePath("profile-map")
        pm_dir._children["value"] = FakePath("value", exists=True)
        sec_dir._children["profile-map"] = pm_dir
        with patch.object(_mod, "SECRETS_DIR", sec_dir):
            result = _resolve_profile_map()
        self.assertIsNotNone(result)

    def test_local_exists(self):
        local = FakePath("local_profile_map.json", exists=True)
        with patch.object(_mod, "SECRETS_DIR", FakePath("s", exists=False)), \
             patch.object(_mod, "PROFILE_MAP_PATH", local):
            result = _resolve_profile_map()
        self.assertIsNotNone(result)

    def test_neither(self):
        with patch.object(_mod, "SECRETS_DIR", FakePath("s", exists=False)), \
             patch.object(_mod, "PROFILE_MAP_PATH", FakePath("p", exists=False)):
            result = _resolve_profile_map()
        self.assertIsNone(result)


class TestResolveChannelIds(unittest.TestCase):
    def test_secret_exists(self):
        sec_dir = FakePath("secrets")
        cid_dir = FakePath("youtube-channel-ids")
        cid_dir._children["value"] = FakePath("value", exists=True)
        sec_dir._children["youtube-channel-ids"] = cid_dir
        with patch.object(_mod, "SECRETS_DIR", sec_dir):
            result = _resolve_channel_ids()
        self.assertIsNotNone(result)

    def test_local(self):
        local_dir = FakePath("token-dir")
        local_dir._children["channel_ids.json"] = FakePath("channel_ids.json", exists=True)
        with patch.object(_mod, "SECRETS_DIR", FakePath("s", exists=False)), \
             patch.object(_mod, "TOKEN_DIR", local_dir):
            result = _resolve_channel_ids()
        self.assertIsNotNone(result)

    def test_none(self):
        with patch.object(_mod, "SECRETS_DIR", FakePath("s", exists=False)), \
             patch.object(_mod, "TOKEN_DIR", FakePath("t", exists=False)):
            result = _resolve_channel_ids()
        self.assertIsNone(result)


# ── _production_slugs ───────────────────────────────────────────────────────

class TestProductionSlugs(unittest.TestCase):
    def test_returns_keys(self):
        registry = [{"key": "chan1"}, {"key": "chan2"}]
        with patch("pipeline.schemas.customization.CHANNEL_REGISTRY", registry):
            from pipeline.cross_engage.burner_engage import _production_slugs
            result = _production_slugs()
        self.assertEqual(result, {"chan1", "chan2"})


# ── _channel_ids_registry ───────────────────────────────────────────────────

class TestChannelIdsRegistry(unittest.TestCase):
    def test_returns_dict(self):
        data = {"myslug": {"channel_id": "UC123"}}
        fake_p = FakePath("channel_ids.json", exists=True)
        fake_p._text_data = json.dumps(data)
        with patch.object(_mod, "_resolve_channel_ids", return_value=fake_p):
            result = _channel_ids_registry()
        self.assertEqual(result, data)

    def test_none_path_returns_empty(self):
        with patch.object(_mod, "_resolve_channel_ids", return_value=None):
            result = _channel_ids_registry()
        self.assertEqual(result, {})

    def test_bad_json_returns_empty(self):
        fake_p = FakePath("channel_ids.json", exists=True)
        fake_p._text_data = "not-json"
        with patch.object(_mod, "_resolve_channel_ids", return_value=fake_p):
            result = _channel_ids_registry()
        self.assertEqual(result, {})


# ── _account_email_for_token ─────────────────────────────────────────────────

class TestAccountEmailForToken(unittest.TestCase):
    def test_no_profile_map(self):
        with patch.object(_mod, "_resolve_profile_map", return_value=None):
            result = _account_email_for_token("myslug")
        self.assertIsNone(result)

    def test_bad_json(self):
        fake_p = FakePath("profile_map.json", exists=True)
        fake_p._text_data = "not-json"
        with patch.object(_mod, "_resolve_profile_map", return_value=fake_p):
            result = _account_email_for_token("myslug")
        self.assertIsNone(result)

    def test_slug_with_email(self):
        data = {"myslug": {"email": "user@example.com"}}
        fake_p = FakePath("profile_map.json", exists=True)
        fake_p._text_data = json.dumps(data)
        with patch.object(_mod, "_resolve_profile_map", return_value=fake_p):
            result = _account_email_for_token("myslug")
        self.assertEqual(result, "user@example.com")

    def test_slug_missing(self):
        data = {}
        fake_p = FakePath("profile_map.json", exists=True)
        fake_p._text_data = json.dumps(data)
        with patch.object(_mod, "_resolve_profile_map", return_value=fake_p):
            result = _account_email_for_token("nope")
        self.assertIsNone(result)

    def test_slug_not_dict(self):
        data = {"myslug": "somestring"}
        fake_p = FakePath("profile_map.json", exists=True)
        fake_p._text_data = json.dumps(data)
        with patch.object(_mod, "_resolve_profile_map", return_value=fake_p):
            result = _account_email_for_token("myslug")
        self.assertIsNone(result)


# ── list_burner_channels ─────────────────────────────────────────────────────

class TestListBurnerChannels(unittest.TestCase):
    def test_loads_from_yaml(self):
        """Burners come from pipeline.channels.BURNERS (the YAML manifest)."""
        from pipeline.channels import BurnerAccount

        fake_burners = (
            BurnerAccount(
                slug="zeta",
                youtube_title="Zeta Title",
                youtube_channel_id="UC_zeta",
                google_email="zeta@example.com",
            ),
            BurnerAccount(
                slug="alpha",
                youtube_title="Alpha Title",
                youtube_channel_id="UC_alpha",
                google_email="alpha@example.com",
            ),
        )
        with patch("pipeline.channels.BURNERS", fake_burners), \
             patch.object(_mod, "_resolve_token_path",
                          side_effect=lambda s: FakePath(s) if s == "alpha" else None):
            result = list_burner_channels()
        # Sorted by slug
        self.assertEqual([r["slug"] for r in result], ["alpha", "zeta"])
        self.assertEqual(result[0]["title"], "Alpha Title")
        self.assertEqual(result[0]["channel_id"], "UC_alpha")
        self.assertEqual(result[0]["email"], "alpha@example.com")
        self.assertTrue(result[0]["has_token"])
        self.assertTrue(result[0]["profile_known"])
        # zeta has no token file but still shows up — YAML is authoritative
        self.assertFalse(result[1]["has_token"])
        self.assertTrue(result[1]["profile_known"])

    def test_empty_yaml(self):
        with patch("pipeline.channels.BURNERS", ()):
            result = list_burner_channels()
        self.assertEqual(result, [])


# ── _email_to_chrome_profile ─────────────────────────────────────────────────

class TestEmailToChromeProfile(unittest.TestCase):
    def test_found(self):
        local_state = {
            "profile": {
                "info_cache": {
                    "Profile 1": {"user_name": "Target@Example.com"},
                }
            }
        }
        ls_path = FakePath("Local State", exists=True)
        ls_path._text_data = json.dumps(local_state)
        chrome_dir = FakePath("Chrome")
        chrome_dir._children["Local State"] = ls_path
        with patch.object(_mod, "CHROME_USER_DIR", chrome_dir):
            result = _email_to_chrome_profile("target@example.com")
        self.assertEqual(result, "Profile 1")

    def test_not_found(self):
        local_state = {"profile": {"info_cache": {"Profile 1": {"user_name": "other@e.com"}}}}
        ls_path = FakePath("Local State", exists=True)
        ls_path._text_data = json.dumps(local_state)
        chrome_dir = FakePath("Chrome")
        chrome_dir._children["Local State"] = ls_path
        with patch.object(_mod, "CHROME_USER_DIR", chrome_dir):
            result = _email_to_chrome_profile("nope@e.com")
        self.assertIsNone(result)

    def test_no_local_state(self):
        chrome_dir = FakePath("Chrome")
        chrome_dir._children["Local State"] = FakePath("Local State", exists=False)
        with patch.object(_mod, "CHROME_USER_DIR", chrome_dir):
            result = _email_to_chrome_profile("any@e.com")
        self.assertIsNone(result)

    def test_bad_json(self):
        ls_path = FakePath("Local State", exists=True)
        ls_path._text_data = "BAD"
        chrome_dir = FakePath("Chrome")
        chrome_dir._children["Local State"] = ls_path
        with patch.object(_mod, "CHROME_USER_DIR", chrome_dir):
            result = _email_to_chrome_profile("any@e.com")
        self.assertIsNone(result)


# ── _stage_profile_copy ──────────────────────────────────────────────────────

class TestStageProfileCopy(unittest.TestCase):
    def test_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            _stage_profile_copy("myslug", "Profile 1")


# ── dataclasses ──────────────────────────────────────────────────────────────

class TestDataclasses(unittest.TestCase):
    def test_video_state_defaults(self):
        vs = VideoState(
            video_id="abc", channel="ch", channel_label="Ch", title="Title", url="http://x"
        )
        self.assertFalse(vs.liked)
        self.assertFalse(vs.subscribed)

    def test_engage_state_defaults(self):
        es = EngageState(slug="s", channel_id="UC", started_at="now")
        self.assertEqual(es.phase, "initializing")
        self.assertEqual(es.videos, [])


# ── sidecar state helpers ────────────────────────────────────────────────────

class TestStatePath(unittest.TestCase):
    def test_returns_path_under_data_dir(self):
        fake_dd = FakePath("data/burner_engage")
        fake_dd._exists = True
        with patch.object(_mod, "DATA_DIR", fake_dd):
            p = _state_path("myslug")
        self.assertIn("myslug", str(p))


class TestStopPath(unittest.TestCase):
    def test_name(self):
        p = _stop_path("myslug")
        self.assertIn("myslug", str(p))


class TestNow(unittest.TestCase):
    def test_iso(self):
        s = _now()
        datetime.fromisoformat(s)  # should not raise


class TestSaveState(unittest.TestCase):
    def test_writes_json(self):
        state = EngageState(slug="s", channel_id="UC1", started_at="2026-01-01T00:00:00+00:00")
        state.videos = [VideoState("v", "ch", "Ch", "T", "http://u")]

        written_data = {}

        def fake_write_text(data, **kwargs):
            written_data["payload"] = data

        fake_tmp = FakePath("s.json.tmp")
        fake_tmp.write_text = fake_write_text
        fake_tmp.replace = MagicMock()

        fake_p = FakePath("s.json")
        fake_p.with_suffix = MagicMock(return_value=fake_tmp)

        with patch.object(_mod, "_state_path", return_value=fake_p):
            _save_state(state)

        self.assertIn("slug", written_data["payload"])
        fake_tmp.replace.assert_called_once_with(fake_p)


class TestReadState(unittest.TestCase):
    def test_returns_dict(self):
        data = {"slug": "s", "phase": "watching"}
        fake_p = FakePath("s.json", exists=True)
        fake_p._text_data = json.dumps(data)
        with patch.object(_mod, "_state_path", return_value=fake_p):
            result = read_state("s")
        self.assertEqual(result["phase"], "watching")

    def test_not_exists_returns_none(self):
        fake_p = FakePath("s.json", exists=False)
        with patch.object(_mod, "_state_path", return_value=fake_p):
            result = read_state("s")
        self.assertIsNone(result)

    def test_bad_json_returns_none(self):
        fake_p = FakePath("s.json", exists=True)
        fake_p._text_data = "BAD"
        with patch.object(_mod, "_state_path", return_value=fake_p):
            result = read_state("s")
        self.assertIsNone(result)


class TestRequestStop(unittest.TestCase):
    def test_writes_file(self):
        fake_p = FakePath("stop", exists=False)
        with patch.object(_mod, "_stop_path", return_value=fake_p):
            request_stop("myslug")
        self.assertNotEqual(fake_p._text_data, "")  # something was written


class TestIsRunning(unittest.TestCase):
    def test_no_state(self):
        with patch.object(_mod, "read_state", return_value=None):
            self.assertFalse(is_running("s"))

    def test_stopped_phase(self):
        with patch.object(_mod, "read_state", return_value={"phase": "stopped"}):
            self.assertFalse(is_running("s"))

    def test_failed_phase(self):
        with patch.object(_mod, "read_state", return_value={"phase": "failed"}):
            self.assertFalse(is_running("s"))

    def test_no_last_action(self):
        with patch.object(_mod, "read_state", return_value={"phase": "watching"}):
            self.assertFalse(is_running("s"))

    def test_bad_timestamp(self):
        with patch.object(_mod, "read_state", return_value={
            "phase": "watching", "last_action_at": "not-a-date"
        }):
            self.assertFalse(is_running("s"))

    def test_recent_action_true(self):
        recent = datetime.now(timezone.utc).isoformat()
        with patch.object(_mod, "read_state", return_value={
            "phase": "watching", "last_action_at": recent
        }):
            self.assertTrue(is_running("s"))

    def test_old_action_false(self):
        old = "2020-01-01T00:00:00+00:00"
        with patch.object(_mod, "read_state", return_value={
            "phase": "watching", "last_action_at": old
        }):
            self.assertFalse(is_running("s"))

    def test_accepts_pre_fetched_state_skips_read(self):
        """When ``state`` is passed, ``read_state`` is NOT called —
        the dashboard's bulk path relies on this to avoid a second
        round trip per burner."""
        recent = datetime.now(timezone.utc).isoformat()
        with patch.object(_mod, "read_state") as mock_read:
            self.assertTrue(is_running(
                "s", state={"phase": "watching", "last_action_at": recent},
            ))
        mock_read.assert_not_called()

    def test_pre_fetched_none_state_means_not_running(self):
        # ``state=None`` falls back to read_state; ``state={}`` is treated
        # as "no state file" and short-circuits to False.
        with patch.object(_mod, "read_state") as mock_read:
            self.assertFalse(is_running("s", state={}))
        mock_read.assert_not_called()


# ── prewarm ──────────────────────────────────────────────────────────────────


class TestPrewarmStates(unittest.TestCase):
    def setUp(self):
        _mod._GCS_STATE_CACHE.clear()
        _mod._GCS_CLIENT = None

    def tearDown(self):
        _mod._GCS_STATE_CACHE.clear()
        _mod._GCS_CLIENT = None

    def test_noop_without_bucket(self):
        with patch.object(_mod, "_state_bucket", return_value=None):
            _mod.prewarm_states(["a", "b"])  # must not raise
        self.assertEqual(_mod._GCS_STATE_CACHE, {})

    def test_noop_without_client(self):
        with patch.object(_mod, "_state_bucket", return_value="bk"), \
             patch.object(_mod, "_gcs_client", return_value=None):
            _mod.prewarm_states(["a", "b"])
        self.assertEqual(_mod._GCS_STATE_CACHE, {})

    def test_noop_empty_slugs(self):
        fake_client = MagicMock()
        with patch.object(_mod, "_state_bucket", return_value="bk"), \
             patch.object(_mod, "_gcs_client", return_value=fake_client):
            _mod.prewarm_states([])
        fake_client.list_blobs.assert_not_called()

    def test_one_list_blobs_then_per_present_download(self):
        """The whole point of this function: ONE list_blobs and only
        downloading the slugs that actually have state in GCS."""
        present_blob = MagicMock()
        present_blob.name = "burner_engage/alpha.json"
        unrelated_blob = MagicMock()
        unrelated_blob.name = "burner_engage/zeta.json"

        download_blob = MagicMock()
        download_blob.download_as_text = MagicMock(
            return_value='{"phase": "watching"}',
        )
        fake_bucket = MagicMock()
        fake_bucket.blob = MagicMock(return_value=download_blob)
        fake_client = MagicMock()
        fake_client.bucket = MagicMock(return_value=fake_bucket)
        fake_client.list_blobs = MagicMock(return_value=iter([present_blob, unrelated_blob]))

        with patch.object(_mod, "_state_bucket", return_value="bk"), \
             patch.object(_mod, "_gcs_client", return_value=fake_client):
            _mod.prewarm_states(["alpha", "beta", "gamma"])

        # ONE list_blobs op for the whole batch.
        fake_client.list_blobs.assert_called_once_with("bk", prefix="burner_engage/")
        # Only `alpha` is present in the listing AND requested → one download.
        # `zeta` is in the listing but not requested; `beta`/`gamma` are
        # requested but not present — no download for them.
        fake_bucket.blob.assert_called_once_with("burner_engage/alpha.json")

        # Cache populated for ALL requested slugs (None for the absent ones)
        # so a subsequent read_state() doesn't fan back out to GCS.
        self.assertEqual(_mod._GCS_STATE_CACHE["alpha"][1], {"phase": "watching"})
        self.assertIsNone(_mod._GCS_STATE_CACHE["beta"][1])
        self.assertIsNone(_mod._GCS_STATE_CACHE["gamma"][1])

    def test_list_blobs_failure_is_swallowed(self):
        fake_client = MagicMock()
        fake_client.list_blobs = MagicMock(side_effect=RuntimeError("network"))
        with patch.object(_mod, "_state_bucket", return_value="bk"), \
             patch.object(_mod, "_gcs_client", return_value=fake_client):
            _mod.prewarm_states(["a"])  # must not raise
        # On failure, cache is NOT populated — read_state will fall
        # through to its own per-slug path.
        self.assertEqual(_mod._GCS_STATE_CACHE, {})

    def test_download_failure_caches_none(self):
        present_blob = MagicMock()
        present_blob.name = "burner_engage/alpha.json"
        download_blob = MagicMock()
        download_blob.download_as_text = MagicMock(side_effect=RuntimeError("boom"))
        fake_bucket = MagicMock()
        fake_bucket.blob = MagicMock(return_value=download_blob)
        fake_client = MagicMock()
        fake_client.bucket = MagicMock(return_value=fake_bucket)
        fake_client.list_blobs = MagicMock(return_value=iter([present_blob]))

        with patch.object(_mod, "_state_bucket", return_value="bk"), \
             patch.object(_mod, "_gcs_client", return_value=fake_client):
            _mod.prewarm_states(["alpha"])

        self.assertIsNone(_mod._GCS_STATE_CACHE["alpha"][1])


# ── worker helpers ───────────────────────────────────────────────────────────

class TestBumpAction(unittest.TestCase):
    def test_updates_state(self):
        state = EngageState(slug="s", channel_id="UC", started_at="now")
        with patch.object(_mod, "_save_state"):
            _bump_action(state, "test msg")
        self.assertEqual(state.last_action_msg, "test msg")
        self.assertNotEqual(state.last_action_at, "")


class TestMaybeStop(unittest.TestCase):
    def test_stop_file_exists(self):
        state = EngageState(slug="s", channel_id="UC", started_at="now")
        fake_stop = FakePath("stop", exists=True)
        with patch.object(_mod, "_stop_path", return_value=fake_stop):
            result = _maybe_stop(state)
        self.assertTrue(result)
        self.assertTrue(state.stop_requested)

    def test_no_stop_file(self):
        state = EngageState(slug="s", channel_id="UC", started_at="now")
        fake_stop = FakePath("stop", exists=False)
        with patch.object(_mod, "_stop_path", return_value=fake_stop), \
             patch.object(_mod, "_read_stop_sentinel_gcs", return_value=False):
            result = _maybe_stop(state)
        self.assertFalse(result)

    def test_gcs_sentinel_triggers_stop(self):
        """Cloud-initiated stop: no local /tmp file but GCS sentinel
        present should still stop the worker."""
        state = EngageState(slug="s", channel_id="UC", started_at="now")
        fake_stop = FakePath("stop", exists=False)
        with patch.object(_mod, "_stop_path", return_value=fake_stop), \
             patch.object(_mod, "_read_stop_sentinel_gcs", return_value=True):
            result = _maybe_stop(state)
        self.assertTrue(result)
        self.assertTrue(state.stop_requested)


# ── GCS state helpers (cloud cutover 2026-05-09) ────────────────────────────


class TestStateBucketHelpers(unittest.TestCase):
    def setUp(self):
        # Always start with a clean cache so tests don't see each other's data.
        _mod._GCS_STATE_CACHE.clear()
        _mod._GCS_STOP_CACHE.clear()
        _mod._GCS_CLIENT = None

    def tearDown(self):
        _mod._GCS_STATE_CACHE.clear()
        _mod._GCS_STOP_CACHE.clear()
        _mod._GCS_CLIENT = None

    def test_bucket_unset_means_noop(self):
        """Without YTFACTORY_STATE_BUCKET, all GCS helpers no-op."""
        with patch.dict("os.environ", {k: v for k, v in __import__("os").environ.items()
                                       if k != "YTFACTORY_STATE_BUCKET"}, clear=True):
            self.assertIsNone(_mod._state_bucket())
            _mod._push_state_gcs("s", {"phase": "x"})  # must not raise
            self.assertIsNone(_mod._read_state_gcs("s"))
            self.assertFalse(_mod._read_stop_sentinel_gcs("s"))
            self.assertFalse(_mod._write_stop_sentinel_gcs("s"))
            _mod.clear_stop_sentinel("s")  # must not raise

    def test_push_state_uploads_to_correct_blob(self):
        fake_blob = MagicMock()
        fake_bucket = MagicMock()
        fake_bucket.blob = MagicMock(return_value=fake_blob)
        fake_client = MagicMock()
        fake_client.bucket = MagicMock(return_value=fake_bucket)
        with patch.dict("os.environ", {"YTFACTORY_STATE_BUCKET": "bk"}), \
             patch.object(_mod, "_gcs_client", return_value=fake_client):
            _mod._push_state_gcs("myslug", {"phase": "watching"})
        fake_bucket.blob.assert_called_once_with("burner_engage/myslug.json")
        fake_blob.upload_from_string.assert_called_once()
        # Fresh state should invalidate any prior cache for that slug.
        self.assertNotIn("myslug", _mod._GCS_STATE_CACHE)

    def test_push_state_swallows_errors(self):
        with patch.dict("os.environ", {"YTFACTORY_STATE_BUCKET": "bk"}), \
             patch.object(_mod, "_gcs_client", side_effect=RuntimeError("boom")):
            _mod._push_state_gcs("s", {"x": 1})  # must not raise

    def test_read_state_gcs_returns_blob_json(self):
        fake_blob = MagicMock()
        fake_blob.exists = MagicMock(return_value=True)
        fake_blob.download_as_text = MagicMock(return_value='{"phase": "engaging"}')
        fake_bucket = MagicMock()
        fake_bucket.blob = MagicMock(return_value=fake_blob)
        fake_client = MagicMock()
        fake_client.bucket = MagicMock(return_value=fake_bucket)
        with patch.dict("os.environ", {"YTFACTORY_STATE_BUCKET": "bk"}), \
             patch.object(_mod, "_gcs_client", return_value=fake_client):
            result = _mod._read_state_gcs("s")
        self.assertEqual(result, {"phase": "engaging"})

    def test_read_state_gcs_caches(self):
        """Two reads in a row should hit the cache (only one client call)."""
        fake_blob = MagicMock()
        fake_blob.exists = MagicMock(return_value=True)
        fake_blob.download_as_text = MagicMock(return_value='{"phase": "x"}')
        fake_bucket = MagicMock()
        fake_bucket.blob = MagicMock(return_value=fake_blob)
        fake_client = MagicMock()
        fake_client.bucket = MagicMock(return_value=fake_bucket)
        with patch.dict("os.environ", {"YTFACTORY_STATE_BUCKET": "bk"}), \
             patch.object(_mod, "_gcs_client", return_value=fake_client) as gcli:
            r1 = _mod._read_state_gcs("s")
            r2 = _mod._read_state_gcs("s")
        self.assertEqual(r1, r2)
        # Client invoked once on first read; second served from cache.
        self.assertEqual(gcli.call_count, 1)

    def test_read_state_gcs_blob_missing_returns_none(self):
        fake_blob = MagicMock()
        fake_blob.exists = MagicMock(return_value=False)
        fake_bucket = MagicMock()
        fake_bucket.blob = MagicMock(return_value=fake_blob)
        fake_client = MagicMock()
        fake_client.bucket = MagicMock(return_value=fake_bucket)
        with patch.dict("os.environ", {"YTFACTORY_STATE_BUCKET": "bk"}), \
             patch.object(_mod, "_gcs_client", return_value=fake_client):
            self.assertIsNone(_mod._read_state_gcs("s"))

    def test_request_stop_writes_both_local_and_gcs(self):
        fake_local = FakePath("/tmp/burner_engage_x.stop", exists=False)
        with patch.object(_mod, "_stop_path", return_value=fake_local), \
             patch.object(_mod, "_write_stop_sentinel_gcs") as gcs:
            request_stop("x")
        self.assertNotEqual(fake_local._text_data, "")
        gcs.assert_called_once_with("x")

    def test_clear_stop_sentinel_deletes_blob(self):
        fake_blob = MagicMock()
        fake_blob.exists = MagicMock(return_value=True)
        fake_bucket = MagicMock()
        fake_bucket.blob = MagicMock(return_value=fake_blob)
        fake_client = MagicMock()
        fake_client.bucket = MagicMock(return_value=fake_bucket)
        with patch.dict("os.environ", {"YTFACTORY_STATE_BUCKET": "bk"}), \
             patch.object(_mod, "_gcs_client", return_value=fake_client):
            _mod.clear_stop_sentinel("s")
        fake_blob.delete.assert_called_once()

    def test_read_state_prefers_gcs_over_local(self):
        fake_local = FakePath("local.json", exists=True)
        fake_local._text_data = '{"phase": "from-local"}'
        with patch.object(_mod, "_state_path", return_value=fake_local), \
             patch.object(_mod, "_state_bucket", return_value="bk"), \
             patch.object(_mod, "_read_state_gcs",
                          return_value={"phase": "from-cloud"}):
            result = read_state("s")
        self.assertEqual(result["phase"], "from-cloud")

    def test_read_state_falls_back_to_local_when_gcs_empty(self):
        fake_local = FakePath("local.json", exists=True)
        fake_local._text_data = '{"phase": "local-only"}'
        with patch.object(_mod, "_state_path", return_value=fake_local), \
             patch.object(_mod, "_state_bucket", return_value="bk"), \
             patch.object(_mod, "_read_state_gcs", return_value=None):
            result = read_state("s")
        self.assertEqual(result["phase"], "local-only")


class TestDeprecatedClickers(unittest.TestCase):
    def test_click_like_raises(self):
        with self.assertRaises(NotImplementedError):
            _click_like(MagicMock())

    def test_click_subscribe_raises(self):
        with self.assertRaises(NotImplementedError):
            _click_subscribe(MagicMock())


class TestHumanPause(unittest.TestCase):
    def test_sleeps(self):
        with patch("time.sleep") as mock_sleep:
            _human_pause(0.0, 0.0)
        mock_sleep.assert_called_once()


# ── run() ────────────────────────────────────────────────────────────────────

def _make_catalog_entry(video_id="vid1", channel="ch1"):
    e = MagicMock()
    e.video_id = video_id
    e.channel = channel
    e.channel_label = channel
    e.title = "Test Video"
    e.url = f"https://youtube.com/watch?v={video_id}"
    return e


class TestRun(unittest.TestCase):
    def _base_patches(self, extra=None):
        """Return a dict of patches common to all run() tests."""
        burners = [{
            "slug": "burner1", "channel_id": "UC_burner",
            "email": "burner@e.com", "has_token": True, "profile_known": True,
        }]
        patches = {
            "list_burner_channels": burners,
            "email": "Profile 1",
            "catalog": [_make_catalog_entry()],
            "real_chrome_pids": [],
        }
        if extra:
            patches.update(extra)
        return patches

    def _run_with_patches(self, slug: str, patches: dict, pw_setup=None):
        burners_list = patches["list_burner_channels"]
        profile = patches["email"]
        catalog = patches["catalog"]
        chrome_pids = patches["real_chrome_pids"]

        mock_sp, pw, browser, ctx, page = make_fake_playwright(
            "https://www.youtube.com/watch?v=vid1"
        )

        if pw_setup:
            pw_setup(pw, browser, ctx, page)

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        def fake_popen_sub(cmd, **kwargs):
            return MagicMock(stdout="12345\n")

        with patch.object(_mod, "list_burner_channels", return_value=burners_list), \
             patch.object(_mod, "_email_to_chrome_profile", return_value=profile), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_like",
                   return_value=("liked", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
                   return_value=("subscribed", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_burner_attached.switch_to_burner_brand",
                   return_value=True), \
             patch("pipeline.cross_engage.cross_engage_burner_attached.switch_to_burner_brand", return_value=True), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("subprocess.run",
                   return_value=MagicMock(stdout=" ".join(chrome_pids) + "\n")), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path",
                          return_value=FakePath("stop", exists=False)), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0), \
             patch("random.choice", side_effect=lambda lst: lst[0]):
            from pipeline.cross_engage.burner_engage import run
            return run(slug)

    def test_slug_not_burner(self):
        from pipeline.cross_engage.burner_engage import run
        with patch.object(_mod, "list_burner_channels", return_value=[]):
            rc = run("no_such_slug")
        self.assertEqual(rc, 2)

    def test_no_email_returns_2(self):
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": None}]
        with patch.object(_mod, "list_burner_channels", return_value=burners):
            rc = run("b1")
        self.assertEqual(rc, 2)

    def test_no_chrome_profile_returns_2(self):
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value=None):
            rc = run("b1")
        self.assertEqual(rc, 2)

    def test_empty_catalog_returns_2(self):
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=[]):
            rc = run("b1")
        self.assertEqual(rc, 2)

    def test_cookie_bridge_failure(self):
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        catalog = [_make_catalog_entry()]
        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("subprocess.run", return_value=MagicMock(stdout="")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies",
                   side_effect=Exception("bridge fail")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path", return_value=FakePath("stop", exists=False)), \
             patch("time.sleep"):
            rc = run("b1")
        self.assertEqual(rc, 1)

    def test_chrome_launch_failure(self):
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        catalog = [_make_catalog_entry()]
        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("subprocess.run", return_value=MagicMock(stdout="")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   side_effect=Exception("launch fail")), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path", return_value=FakePath("stop", exists=False)), \
             patch("time.sleep"):
            rc = run("b1")
        self.assertEqual(rc, 1)

    def test_real_chrome_running_skips_bridge(self):
        """When real Chrome is running, we skip cookie bridge."""
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        catalog = [_make_catalog_entry()]
        mock_sp, pw, browser, ctx, page = make_fake_playwright(
            "https://www.youtube.com/watch?v=vid1"
        )
        # Stop file appears after first tick to exit the watch loop
        stop_calls = {"n": 0}
        fake_stop = FakePath("stop", exists=False)

        def stop_exists():
            stop_calls["n"] += 1
            return stop_calls["n"] > 5

        fake_stop.exists = stop_exists
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        bridge_mock = MagicMock()

        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies", bridge_mock), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_like",
                   return_value=("liked", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
                   return_value=("subscribed", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_burner_attached.switch_to_burner_brand", return_value=True), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("subprocess.run", return_value=MagicMock(stdout="99999\n")), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path", return_value=fake_stop), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0), \
             patch("random.choice", side_effect=lambda lst: lst[0] if lst else None):
            rc = run("b1")
        bridge_mock.assert_not_called()

    def test_run_engage_loop_success_and_watch(self):
        """Cover the full engage+watch loop with stop signal."""
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC_b1", "email": "e@e.com"}]
        catalog = [_make_catalog_entry("v1", "ch1"), _make_catalog_entry("v2", "ch1")]
        mock_sp, pw, browser, ctx, page = make_fake_playwright(
            "https://www.youtube.com/watch?v=v1"
        )
        # Provide 2 pages (one per video) plus the page
        page2 = MagicMock()
        page2.url = "https://www.youtube.com/watch?v=v2"
        ctx.pages = [page, page2]
        ctx.new_page.side_effect = [page, page2]

        stop_calls = {"n": 0}
        fake_stop = FakePath("stop", exists=False)

        def stop_exists():
            stop_calls["n"] += 1
            return stop_calls["n"] > 10  # stop after engage phase

        fake_stop.exists = stop_exists
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        # Like states: first call unliked, second call liked (success)
        like_calls = {"ch1": 0}

        def probe_like_impl(p):
            like_calls["ch1"] += 1
            if like_calls["ch1"] % 2 == 1:
                btn = MagicMock()
                btn.dispatch_event.return_value = None
                return "unliked", btn
            return "liked", MagicMock()

        sub_calls = {"n": 0}

        def probe_sub_impl(p):
            sub_calls["n"] += 1
            if sub_calls["n"] % 2 == 1:
                btn = MagicMock()
                btn.scroll_into_view_if_needed.return_value = None
                btn.click.return_value = None
                return "unsubscribed", btn
            return "subscribed", MagicMock()

        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_like",
                   side_effect=probe_like_impl), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
                   side_effect=probe_sub_impl), \
             patch("pipeline.cross_engage.cross_engage_burner_attached.switch_to_burner_brand", return_value=True), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("subprocess.run", return_value=MagicMock(stdout="")), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path", return_value=fake_stop), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.5), \
             patch("random.choice", side_effect=lambda lst: lst[0]):
            rc = run("b1")
        self.assertEqual(rc, 0)

    def test_run_sign_in_redirect(self):
        """Cover the accounts.google.com redirect branch inside engage loop."""
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        catalog = [_make_catalog_entry("v1")]
        mock_sp, pw, browser, ctx, page = make_fake_playwright()
        page.url = "https://accounts.google.com/signin"
        # ctx.pages returns the sign-in page; stop after first tick
        ctx.pages = [page]
        ctx.new_page.return_value = page

        fake_stop = FakePath("stop", exists=False)
        stop_calls = {"n": 0}

        def stop_exists():
            stop_calls["n"] += 1
            return stop_calls["n"] > 3

        fake_stop.exists = stop_exists
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_burner_attached.switch_to_burner_brand", return_value=True), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("subprocess.run", return_value=MagicMock(stdout="")), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path", return_value=fake_stop), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0), \
             patch("random.choice", side_effect=lambda lst: lst[0]):
            rc = run("b1")

    def test_run_stop_during_engage(self):
        """Cover stop signal during engage phase (between video iterations)."""
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        catalog = [_make_catalog_entry("v1")]
        mock_sp, pw, browser, ctx, page = make_fake_playwright(
            "https://www.youtube.com/watch?v=v1"
        )
        ctx.pages = [page]
        ctx.new_page.return_value = page

        # Stop file exists from the start — immediately stops
        fake_stop = FakePath("stop", exists=True)
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_like",
                   return_value=("liked", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
                   return_value=("subscribed", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_burner_attached.switch_to_burner_brand", return_value=True), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("subprocess.run", return_value=MagicMock(stdout="")), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path", return_value=fake_stop), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0):
            rc = run("b1")
        self.assertEqual(rc, 0)

    def test_run_no_pages_after_engage(self):
        """Cover the 'no tabs opened — nothing to cycle' branch.

        Post 2026-05-10 refactor: Phase 0 opens all tabs upfront; if
        every ctx.new_page() (or page.goto) raises, video_pages stays
        empty and the worker exits with phase=failed / rc=1 BEFORE
        entering the cycle loop. Replaces the old test for the
        Phase-2 "no http pages" branch which no longer exists.
        """
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        catalog = [_make_catalog_entry("v1")]
        mock_sp, pw, browser, ctx, page = make_fake_playwright(
            "https://www.youtube.com/watch?v=v1"
        )
        # Force Phase 0 to fail on every tab open. Either path works
        # (ctx.new_page raise OR page.goto raise) but new_page is
        # cleaner because it also exercises the broad except in the
        # Phase 0 loop.
        ctx.new_page.side_effect = RuntimeError("page-open denied")

        fake_stop = FakePath("stop", exists=False)
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_like",
                   return_value=("liked", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
                   return_value=("subscribed", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_burner_attached.switch_to_burner_brand", return_value=True), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("subprocess.run", return_value=MagicMock(stdout="")), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path", return_value=fake_stop), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0):
            rc = run("b1")
        self.assertEqual(rc, 1)

    def test_run_tab_dies_in_watch_loop(self):
        """Cover the tab-dies-in-watch-loop branch (all tabs gone → failed)."""
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        catalog = [_make_catalog_entry("v1")]
        mock_sp, pw, browser, ctx, page = make_fake_playwright(
            "https://www.youtube.com/watch?v=v1"
        )
        ctx.pages = [page]
        ctx.new_page.return_value = page
        # bring_to_front fails → tab dies path
        page.bring_to_front.side_effect = Exception("tab dead")

        fake_stop = FakePath("stop", exists=False)
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_like",
                   return_value=("liked", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
                   return_value=("subscribed", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_burner_attached.switch_to_burner_brand", return_value=True), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("subprocess.run", return_value=MagicMock(stdout="")), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path", return_value=fake_stop), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0), \
             patch("random.choice", side_effect=lambda lst: lst[0]):
            rc = run("b1")
        # "all tabs died" breaks the while loop → state.phase="failed" → returns 0
        self.assertEqual(rc, 0)

    def test_run_playwright_crash(self):
        """Cover the outer except branch (worker crashed)."""
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        catalog = [_make_catalog_entry("v1")]
        mock_sp, pw, browser, ctx, page = make_fake_playwright()

        # connect_over_cdp raises so the entire with block fails
        pw.chromium.connect_over_cdp.side_effect = Exception("CDP dead")

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_burner_attached.switch_to_burner_brand", return_value=True), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("subprocess.run", return_value=MagicMock(stdout="")), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path", return_value=FakePath("stop", exists=False)), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0):
            rc = run("b1")
        self.assertEqual(rc, 1)

    def test_run_proc_kill_on_terminate_fail(self):
        """Cover the finally: proc.kill() when proc.terminate()+wait() both fail."""
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        catalog = [_make_catalog_entry("v1")]
        mock_sp, pw, browser, ctx, page = make_fake_playwright(
            "https://www.youtube.com/watch?v=v1"
        )
        ctx.pages = [page]
        ctx.new_page.return_value = page

        fake_stop = FakePath("stop", exists=True)  # stop immediately
        mock_proc = MagicMock()
        mock_proc.wait.side_effect = Exception("wait timeout")

        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_like",
                   return_value=("liked", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
                   return_value=("subscribed", MagicMock())), \
             patch("pipeline.cross_engage.cross_engage_burner_attached.switch_to_burner_brand", return_value=True), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("subprocess.run", return_value=MagicMock(stdout="")), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path", return_value=fake_stop), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0):
            run("b1")
        mock_proc.kill.assert_called()

    def test_run_engage_loop_exception_on_video(self):
        """Cover exception inside the per-video try block."""
        from pipeline.cross_engage.burner_engage import run
        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        catalog = [_make_catalog_entry("v1")]
        mock_sp, pw, browser, ctx, page = make_fake_playwright()

        # Make new_page() raise so the per-video try/except fires
        ctx.new_page.side_effect = Exception("page error")
        ctx.pages = []

        fake_stop = FakePath("stop", exists=False)
        stop_c = {"n": 0}

        def stop_ex():
            stop_c["n"] += 1
            return stop_c["n"] > 3

        fake_stop.exists = stop_ex
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             patch("pipeline.cross_engage.cross_engage_burner_attached.switch_to_burner_brand", return_value=True), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("subprocess.run", return_value=MagicMock(stdout="")), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path", return_value=fake_stop), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=0.0):
            run("b1")

    def _run_single_video_edge(self, *, page_url="https://www.youtube.com/watch?v=vid1",
                               stop_exists=None, probe_like=None, probe_subscribe=None,
                               page_setup=None, proc_setup=None, random_uniform=0.0):
        from pipeline.cross_engage.burner_engage import run

        burners = [{"slug": "b1", "channel_id": "UC", "email": "e@e.com"}]
        catalog = [_make_catalog_entry("vid1", "ch1")]
        mock_sp, pw, browser, ctx, page = make_fake_playwright(page_url)
        ctx.pages = [page]
        ctx.new_page.return_value = page
        if page_setup:
            page_setup(page, ctx)

        fake_stop = FakePath("stop", exists=False)
        if stop_exists is None:
            calls = {"n": 0}
            def stop_exists():
                calls["n"] += 1
                return calls["n"] > 1
        fake_stop.exists = stop_exists

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        if proc_setup:
            proc_setup(mock_proc)

        if probe_like is None:
            probe_like = ("liked", MagicMock())
        if probe_subscribe is None:
            probe_subscribe = ("subscribed", MagicMock())

        like_patch = patch(
            "pipeline.cross_engage.cross_engage_via_playwright._probe_like",
            side_effect=probe_like if callable(probe_like) else None,
            return_value=probe_like if not callable(probe_like) else None,
        )
        sub_patch = patch(
            "pipeline.cross_engage.cross_engage_via_playwright._probe_subscribe",
            side_effect=probe_subscribe if callable(probe_subscribe) else None,
            return_value=probe_subscribe if not callable(probe_subscribe) else None,
        )

        with patch.object(_mod, "list_burner_channels", return_value=burners), \
             patch.object(_mod, "_email_to_chrome_profile", return_value="Profile 1"), \
             patch("pipeline.utils.catalog.list_catalog", return_value=catalog), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.bridge_cookies"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright._clear_singleton"), \
             patch("pipeline.cross_engage.cross_engage_via_playwright.launch_chrome_for",
                   return_value=(mock_proc, "12345")), \
             like_patch, sub_patch, \
             patch("pipeline.cross_engage.cross_engage_burner_attached.switch_to_burner_brand", return_value=True), \
             patch("playwright.sync_api.sync_playwright", mock_sp), \
             patch("subprocess.run", return_value=MagicMock(stdout="")), \
             patch.object(_mod, "_save_state"), \
             patch.object(_mod, "_stop_path", return_value=fake_stop), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=random_uniform), \
             patch("random.choice", side_effect=lambda lst: lst[0]):
            rc = run("b1")
        return rc, mock_proc, page

    def test_run_consent_modal_click_exceptions_continue(self):
        def setup(page, ctx):
            consent_btn = MagicMock()
            consent_btn.click.side_effect = Exception("modal")
            page.locator.return_value.first = consent_btn

        rc, _proc, _page = self._run_single_video_edge(page_setup=setup)
        self.assertEqual(rc, 0)

    def test_run_like_click_exception_sets_error(self):
        like_btn = MagicMock()
        like_btn.dispatch_event.side_effect = Exception("like blocked")

        rc, _proc, _page = self._run_single_video_edge(
            probe_like=("unliked", like_btn),
            probe_subscribe=("subscribed", MagicMock()),
        )
        self.assertEqual(rc, 0)

    def test_run_subscribe_click_exception_sets_error(self):
        sub_btn = MagicMock()
        sub_btn.click.side_effect = Exception("sub blocked")

        rc, _proc, _page = self._run_single_video_edge(
            probe_like=("liked", MagicMock()),
            probe_subscribe=("unsubscribed", sub_btn),
        )
        self.assertEqual(rc, 0)

    def test_run_watch_loop_maps_catalog_url_to_page(self):
        calls = {"n": 0}
        def stop_exists():
            calls["n"] += 1
            return calls["n"] > 3

        rc, _proc, page = self._run_single_video_edge(
            page_url="https://youtube.com/watch?v=vid1",
            stop_exists=stop_exists,
            random_uniform=0.0,
        )
        self.assertEqual(rc, 0)
        page.bring_to_front.assert_called()

    def test_run_proc_kill_exception_swallowed(self):
        def proc_setup(proc):
            proc.terminate.side_effect = Exception("terminate failed")
            proc.kill.side_effect = Exception("kill failed")

        rc, proc, _page = self._run_single_video_edge(proc_setup=proc_setup)
        self.assertEqual(rc, 0)
        proc.kill.assert_called_once()


# ── _cli() ────────────────────────────────────────────────────────────────────

class TestCli(unittest.TestCase):
    def _run_cli(self, argv, extra_patches=None):
        from pipeline.cross_engage.burner_engage import _cli
        import io
        patches = {
            "list_burner_channels": [{"slug": "b1", "channel_id": "UC", "title": "B1",
                                       "profile_known": True}],
        }
        if extra_patches:
            patches.update(extra_patches)

        with patch("sys.argv", ["burner_engage"] + argv), \
             patch.object(_mod, "list_burner_channels",
                          return_value=patches["list_burner_channels"]):
            return _cli()

    def test_list_cmd(self):
        rc = self._run_cli(["list"])
        self.assertEqual(rc, 0)

    def test_list_no_email_mark(self):
        burners = [{"slug": "b1", "channel_id": "UC", "title": "B1", "profile_known": False}]
        with patch("sys.argv", ["be", "list"]), \
             patch.object(_mod, "list_burner_channels", return_value=burners):
            from pipeline.cross_engage.burner_engage import _cli
            rc = _cli()
        self.assertEqual(rc, 0)

    def test_stop_cmd(self):
        fake_stop = FakePath("stop")
        with patch.object(_mod, "_stop_path", return_value=fake_stop):
            rc = self._run_cli(["stop", "b1"])
        self.assertEqual(rc, 0)

    def test_status_cmd_with_state(self):
        data = {"slug": "b1", "phase": "watching"}
        with patch.object(_mod, "read_state", return_value=data):
            rc = self._run_cli(["status", "b1"])
        self.assertEqual(rc, 0)

    def test_status_cmd_no_state(self):
        with patch.object(_mod, "read_state", return_value=None):
            rc = self._run_cli(["status", "b1"])
        self.assertEqual(rc, 0)

    def test_run_cmd(self):
        with patch.object(_mod, "run", return_value=0) as mock_run:
            rc = self._run_cli(["run", "b1"])
        self.assertEqual(rc, 0)
        mock_run.assert_called_once_with("b1", headless=False, mode="like_subscribe_view")

    def test_run_cmd_headless(self):
        with patch.object(_mod, "run", return_value=0) as mock_run:
            rc = self._run_cli(["run", "b1", "--headless"])
        mock_run.assert_called_once_with("b1", headless=True, mode="like_subscribe_view")

    def test_run_cmd_mode(self):
        """--mode plumbs through to run()."""
        with patch.object(_mod, "run", return_value=0) as mock_run:
            rc = self._run_cli(["run", "b1", "--mode", "subscribe_only"])
        self.assertEqual(rc, 0)
        mock_run.assert_called_once_with("b1", headless=False, mode="subscribe_only")

    def test_unknown_parsed_command_returns_2(self):
        from pipeline.cross_engage.burner_engage import _cli
        import argparse

        with patch("argparse.ArgumentParser.parse_args",
                   return_value=argparse.Namespace(cmd="unexpected")):
            rc = _cli()
        self.assertEqual(rc, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
