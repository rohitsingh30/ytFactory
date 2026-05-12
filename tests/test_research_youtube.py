"""Tests for pipeline/research/youtube.py — YouTube stats fetcher.

All YouTube API calls are mocked.  No network, no credentials.
"""

from __future__ import annotations

import json
import os
import sys
import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.research import youtube as yt_mod


# ---------------------------------------------------------------------------
# Fake googleapiclient stubs (same pattern as test_pipeline_youtube_stats.py)
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeChannels:
    def __init__(self, payload=None, raise_http=False):
        self._payload = payload
        self._raise_http = raise_http

    def list(self, **kw):
        if self._raise_http:
            raise _make_http_error(403)
        if self._payload is None:
            return _FakeRequest({"items": []})
        return _FakeRequest({"items": [self._payload]})


class _FakePlaylistItems:
    def __init__(self, pages_by_token: dict, raise_http=False):
        self._pages = pages_by_token
        self._raise_http = raise_http

    def list(self, *, part, playlistId, maxResults, pageToken):
        if self._raise_http:
            raise _make_http_error(403)
        return _FakeRequest(self._pages.get(pageToken, {"items": [], "nextPageToken": None}))


class _FakeVideos:
    def __init__(self, items_by_id: dict, raise_http=False):
        self._items = items_by_id
        self._raise_http = raise_http

    def list(self, *, part, id):
        if self._raise_http:
            raise _make_http_error(403)
        ids = id.split(",")
        items = [self._items[i] for i in ids if i in self._items]
        return _FakeRequest({"items": items})


class _FakeYouTube:
    def __init__(self, *, channels, playlist_items, videos):
        self._channels = channels
        self._playlist_items = playlist_items
        self._videos = videos

    def channels(self):
        return self._channels

    def playlistItems(self):
        return self._playlist_items

    def videos(self):
        return self._videos


def _make_http_error(status: int):
    """Create a minimal googleapiclient.errors.HttpError-like exception."""
    err_mod = sys.modules.get("googleapiclient.errors")
    if err_mod is None:
        err_mod = types.ModuleType("googleapiclient.errors")
        sys.modules["googleapiclient.errors"] = err_mod
    if not hasattr(err_mod, "HttpError"):
        class HttpError(Exception):
            def __init__(self, resp, content=b""):
                self.resp = resp
                self.content = content
                super().__init__(str(resp))
        err_mod.HttpError = HttpError

    class _Resp:
        def __init__(self, s):
            self.status = s
    return err_mod.HttpError(_Resp(status), b"forbidden")


def _install_fake_google(youtube_obj):
    """Install fake googleapiclient.{discovery,errors} into sys.modules.

    POLLUTION-SAFE via :func:`tests._helpers.make_fake_googleapiclient_pkg`
    + :func:`make_fake_googleapiclient_errors` — see those docstrings
    for the rationale (otherwise ``test_upload_youtube`` later in the
    alphabetical order trips
    ``ModuleNotFoundError: 'googleapiclient' is not a package`` OR
    ``ImportError: cannot import name 'BatchError'`` on its
    ``from googleapiclient.http import MediaFileUpload``).
    Fixed 2026-05-11.
    """
    from tests._helpers import (
        make_fake_googleapiclient_errors,
        make_fake_googleapiclient_pkg,
    )

    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = lambda *a, **kw: youtube_obj
    errors = make_fake_googleapiclient_errors()

    class HttpError(Exception):
        def __init__(self, resp, content=b""):
            self.resp = resp
            self.content = content
            super().__init__(str(resp))

    errors.HttpError = HttpError

    fake_pkg = make_fake_googleapiclient_pkg()
    fake_pkg.discovery = discovery
    fake_pkg.errors = errors

    sys.modules["googleapiclient"] = fake_pkg
    sys.modules["googleapiclient.discovery"] = discovery
    sys.modules["googleapiclient.errors"] = errors


# ---------------------------------------------------------------------------
# iter_channel_configs
# ---------------------------------------------------------------------------


class IterChannelConfigsTest(unittest.TestCase):

    def test_returns_account_and_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chan = root / "mychan"
            chan.mkdir()
            (chan / "config.yaml").write_text("upload:\n  account: myvid\n")
            with patch.object(yt_mod, "__builtins__", __builtins__):
                with patch("pipeline.research.youtube.PROJECT_ROOT", root):
                    result = yt_mod.iter_channel_configs()
        self.assertIn(("myvid", "mychan"), result)

    def test_falls_back_to_dir_name_when_no_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chan = root / "mychan"
            chan.mkdir()
            (chan / "config.yaml").write_text("name: My Channel\n")
            with patch("pipeline.research.youtube.PROJECT_ROOT", root):
                result = yt_mod.iter_channel_configs()
        accounts = [a for a, _ in result]
        self.assertIn("mychan", accounts)

    def test_skips_non_channel_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pipeline").mkdir()
            (root / ".git").mkdir()
            chan = root / "mychan"
            chan.mkdir()
            (chan / "config.yaml").write_text("name: mychan\n")
            with patch("pipeline.research.youtube.PROJECT_ROOT", root):
                result = yt_mod.iter_channel_configs()
        dirs = [d for _, d in result]
        self.assertNotIn("pipeline", dirs)
        self.assertNotIn(".git", dirs)

    def test_skips_yaml_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chan = root / "badchan"
            chan.mkdir()
            (chan / "config.yaml").write_text(":\n  - [unclosed")
            with patch("pipeline.research.youtube.PROJECT_ROOT", root):
                result = yt_mod.iter_channel_configs()
        dirs = [d for _, d in result]
        self.assertIn("badchan", dirs)  # falls back to dir name, still included

    def test_skips_dirs_without_config_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "noconfig").mkdir()
            with patch("pipeline.research.youtube.PROJECT_ROOT", root):
                result = yt_mod.iter_channel_configs()
        self.assertEqual(result, [])

    def test_centralised_registry_takes_precedence(self):
        """Post-2026-05-10 hygiene pass: pipeline/channels/<slug>.yaml is
        the canonical channel registry. Cloud images have no per-channel
        root dirs, so iter_channel_configs() MUST find accounts via the
        registry. Both sources merge; registry wins on dup slugs."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "pipeline" / "channels"
            registry.mkdir(parents=True)
            (registry / "centralchan.yaml").write_text(
                "name: CentralChan\nupload:\n  account: centralacct\n"
            )
            # Also drop a per-channel root dir to verify both are found.
            legacy = root / "legacychan"
            legacy.mkdir()
            (legacy / "config.yaml").write_text(
                "upload:\n  account: legacyacct\n"
            )
            with patch("pipeline.research.youtube.PROJECT_ROOT", root):
                result = yt_mod.iter_channel_configs()
        # Index by channel_dir (the second element).
        by_dir = {d: a for a, d in result}
        self.assertEqual(by_dir.get("centralchan"), "centralacct")
        self.assertEqual(by_dir.get("legacychan"), "legacyacct")

    def test_registry_dedupes_against_legacy(self):
        """When the same slug exists in both the registry AND as a legacy
        per-channel dir, the registry entry wins (cloud is the source of
        truth post-hygiene-pass)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "pipeline" / "channels"
            registry.mkdir(parents=True)
            (registry / "dupchan.yaml").write_text(
                "upload:\n  account: registry_account\n"
            )
            legacy = root / "dupchan"
            legacy.mkdir()
            (legacy / "config.yaml").write_text(
                "upload:\n  account: legacy_account\n"
            )
            with patch("pipeline.research.youtube.PROJECT_ROOT", root):
                result = yt_mod.iter_channel_configs()
        # Only one entry for dupchan, and it's the registry one.
        dupchan_entries = [a for a, d in result if d == "dupchan"]
        self.assertEqual(dupchan_entries, ["registry_account"])


# ---------------------------------------------------------------------------
# D5 — fixture lockdown: writing into the real YOUTUBE_DIR during tests
# raises a loud RuntimeError instead of silently corrupting the cache
# ---------------------------------------------------------------------------


class FixtureLockdownTest(unittest.TestCase):

    def test_fetch_account_refuses_real_dir_during_pytest(self):
        """The autouse isolate_research_dirs fixture has already pointed
        ``YOUTUBE_DIR`` at a tmp dir, so we have to UNDO that to simulate
        a buggy test that forgot to monkey-patch.

        ``_assert_safe_to_write`` is gated on ``PYTEST_CURRENT_TEST``
        being set — pytest sets it automatically, but the CI runs the
        suite via ``python -m unittest`` where the env var is absent.
        Set it explicitly so the guard fires under both runners.
        """
        from pipeline.paths import RESEARCH_DIR as _real_research

        real_youtube_dir = _real_research / "youtube"
        with patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "FixtureLockdownTest"}), \
             patch.object(yt_mod, "YOUTUBE_DIR", real_youtube_dir):
            with self.assertRaises(RuntimeError) as cm:
                yt_mod.fetch_account("anyaccount", quiet=True)
            self.assertIn("Refusing to write", str(cm.exception))
            self.assertIn("YOUTUBE_DIR", str(cm.exception))

    def test_guard_no_op_outside_pytest(self):
        """If PYTEST_CURRENT_TEST is unset, the guard does nothing — the
        production path (cloud Job, laptop CLI) must not raise."""
        import os as _os
        saved = _os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            # Should not raise even with YOUTUBE_DIR pointed at the real dir.
            from pipeline.paths import RESEARCH_DIR as _real_research
            with patch.object(yt_mod, "YOUTUBE_DIR", _real_research / "youtube"):
                yt_mod._assert_safe_to_write("anyaccount")
        finally:
            if saved is not None:
                _os.environ["PYTEST_CURRENT_TEST"] = saved

    def test_guard_no_op_in_gcs_mode(self):
        """When YTFACTORY_STATE_BUCKET is set, writes go to GCS not the
        local FS so the guard is irrelevant — must not raise."""
        with patch.dict("os.environ", {"YTFACTORY_STATE_BUCKET": "fake"}):
            from pipeline.paths import RESEARCH_DIR as _real_research
            with patch.object(yt_mod, "YOUTUBE_DIR", _real_research / "youtube"):
                # Should not raise.
                yt_mod._assert_safe_to_write("anyaccount")


# ---------------------------------------------------------------------------
# _build_youtube: auth failure → None
# ---------------------------------------------------------------------------


class BuildYoutubeTest(unittest.TestCase):

    def test_auth_failure_returns_none(self):
        with patch("pipeline.upload.authenticate", side_effect=Exception("no creds")):
            result = yt_mod._build_youtube("myaccount")
        self.assertIsNone(result)

    def test_returns_built_client_on_success(self):
        fake_yt = MagicMock()
        _install_fake_google(fake_yt)
        with patch("pipeline.upload.authenticate", return_value=MagicMock()):
            result = yt_mod._build_youtube("myaccount")
        self.assertIsNotNone(result)

    def test_refresh_token_lost_propagates(self):
        # Audit T1.8 — pre-fix the broad except Exception swallowed
        # this typed exception → silent all-nulls degrade. Now it
        # bubbles so cloud-side callers can route the signal to
        # /api/admin/token-health for the daily cron alert.
        from pipeline.upload.upload import RefreshTokenLost
        with patch(
            "pipeline.upload.authenticate",
            side_effect=RefreshTokenLost("myaccount"),
        ):
            with self.assertRaises(RefreshTokenLost):
                yt_mod._build_youtube("myaccount")


# ---------------------------------------------------------------------------
# _save_cache / _load_cache / _list_cached_accounts — provider-aware (B1)
# ---------------------------------------------------------------------------


class CacheStoreFSBackendTest(unittest.TestCase):
    """When YTFACTORY_STATE_BUCKET is unset, cache I/O hits the local FS."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._patch_yt_dir = patch.object(yt_mod, "YOUTUBE_DIR", self.tmp)
        self._patch_yt_dir.start()
        self._patch_env = patch.dict("os.environ", {}, clear=False)
        self._patch_env.start()
        # Make sure the env var is not set during this test.
        import os as _os
        _os.environ.pop("YTFACTORY_STATE_BUCKET", None)
        # Clear in-process read cache between tests (writes already do).
        yt_mod._READ_CACHE.clear()

    def tearDown(self):
        self._patch_env.stop()
        self._patch_yt_dir.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_then_load_roundtrip(self):
        payload = {"account": "x", "channel": {"title": "X"}, "videos": []}
        yt_mod._save_cache("x", payload)
        loaded = yt_mod._load_cache("x")
        self.assertEqual(loaded, payload)

    def test_load_missing_returns_none(self):
        self.assertIsNone(yt_mod._load_cache("nope"))

    def test_list_cached_accounts(self):
        yt_mod._save_cache("a", {"account": "a"})
        yt_mod._save_cache("b", {"account": "b"})
        self.assertEqual(yt_mod._list_cached_accounts(), ["a", "b"])

    def test_save_invalidates_read_cache(self):
        yt_mod._save_cache("a", {"account": "a", "v": 1})
        first = yt_mod._load_cache("a")
        self.assertEqual(first.get("v"), 1)
        # Sleep is necessary to bump mtime on filesystems with 1s
        # resolution; instead just bump the file directly.
        import os as _os, time as _time
        _time.sleep(0.01)
        yt_mod._save_cache("a", {"account": "a", "v": 2})
        second = yt_mod._load_cache("a")
        self.assertEqual(second.get("v"), 2)


class CacheStoreGCSBackendTest(unittest.TestCase):
    """When YTFACTORY_STATE_BUCKET is set, cache I/O hits GCS."""

    def setUp(self):
        # Patch _gcs_blob to a fake that records uploads + serves them
        # back on download. Mirrors the real google.cloud.storage.Blob
        # surface (upload_from_string, reload, download_as_bytes,
        # generation).
        self._store: dict[str, tuple[bytes, int]] = {}
        self._gen = [0]

        outer = self

        class FakeBlob:
            def __init__(self, key):
                self.key = key
                self.generation = None

            def upload_from_string(self, body, content_type=None):
                if isinstance(body, str):
                    body = body.encode("utf-8")
                outer._gen[0] += 1
                outer._store[self.key] = (body, outer._gen[0])

            def reload(self):
                if self.key not in outer._store:
                    from google.api_core import exceptions as gax
                    raise gax.NotFound(f"missing: {self.key}")
                self.generation = outer._store[self.key][1]

            def download_as_bytes(self):
                return outer._store[self.key][0]

        class FakeClient:
            def list_blobs(self, bucket, prefix):
                for key in sorted(outer._store):
                    if not key.startswith(prefix):
                        continue
                    b = FakeBlob(key)
                    b.name = key
                    yield b

        self._fake_client = FakeClient()
        self._patches = [
            patch.dict("os.environ", {"YTFACTORY_STATE_BUCKET": "fake-bucket"}),
            patch.object(yt_mod, "_gcs_blob", lambda bucket, key: FakeBlob(key)),
        ]
        for p in self._patches:
            p.start()
        # Patch the storage client used by _list_cached_accounts.
        import google.cloud.storage  # noqa: F401 — make sure module importable
        self._patch_client = patch(
            "google.cloud.storage.Client", lambda project=None: self._fake_client
        )
        self._patch_client.start()
        yt_mod._READ_CACHE.clear()

    def tearDown(self):
        self._patch_client.stop()
        for p in reversed(self._patches):
            p.stop()
        yt_mod._READ_CACHE.clear()

    def test_save_writes_to_gcs(self):
        yt_mod._save_cache("acct", {"account": "acct", "n": 7})
        self.assertIn("data/research/youtube/acct.json", self._store)
        body, gen = self._store["data/research/youtube/acct.json"]
        import json as _j
        self.assertEqual(_j.loads(body)["n"], 7)
        self.assertGreater(gen, 0)

    def test_load_reads_from_gcs(self):
        yt_mod._save_cache("acct", {"account": "acct", "n": 7})
        loaded = yt_mod._load_cache("acct")
        self.assertEqual(loaded.get("n"), 7)

    def test_load_missing_returns_none(self):
        self.assertIsNone(yt_mod._load_cache("nope"))

    def test_list_cached_accounts_walks_gcs(self):
        yt_mod._save_cache("a", {"account": "a"})
        yt_mod._save_cache("b", {"account": "b"})
        self.assertEqual(yt_mod._list_cached_accounts(), ["a", "b"])

    def test_read_cache_keyed_on_generation(self):
        yt_mod._save_cache("acct", {"account": "acct", "v": 1})
        first = yt_mod._load_cache("acct")
        self.assertEqual(first.get("v"), 1)
        yt_mod._save_cache("acct", {"account": "acct", "v": 2})
        second = yt_mod._load_cache("acct")
        self.assertEqual(second.get("v"), 2)


# ---------------------------------------------------------------------------
# _fetch_channel
# ---------------------------------------------------------------------------


def _make_channel_item(
    *,
    channel_id="UCtest",
    title="Test Channel",
    subscriber_count="100",
    view_count="1000",
    video_count="10",
    hidden=False,
    uploads_playlist="PLtest",
    has_high_thumb=False,
):
    thumbs: dict = {}
    if has_high_thumb:
        thumbs["high"] = {"url": "https://high.jpg"}
    thumbs["default"] = {"url": "https://default.jpg"}

    return {
        "id": channel_id,
        "snippet": {
            "title": title,
            "description": "desc",
            "thumbnails": thumbs,
            "customUrl": "@test",
        },
        "statistics": {
            "subscriberCount": subscriber_count,
            "viewCount": view_count,
            "videoCount": video_count,
            "hiddenSubscriberCount": hidden,
        },
        "contentDetails": {"relatedPlaylists": {"uploads": uploads_playlist}},
        "brandingSettings": {"image": {"bannerExternalUrl": "https://banner.jpg"}},
    }


class FetchChannelTest(unittest.TestCase):

    def test_returns_channel_meta(self):
        item = _make_channel_item(has_high_thumb=True)
        yt = _FakeYouTube(
            channels=_FakeChannels(payload=item),
            playlist_items=_FakePlaylistItems({}),
            videos=_FakeVideos({}),
        )
        result = yt_mod._fetch_channel(yt)
        self.assertEqual(result["id"], "UCtest")
        self.assertEqual(result["subscriber_count"], 100)
        self.assertEqual(result["uploads_playlist"], "PLtest")

    def test_returns_none_when_no_items(self):
        yt = _FakeYouTube(
            channels=_FakeChannels(payload=None),
            playlist_items=_FakePlaylistItems({}),
            videos=_FakeVideos({}),
        )
        result = yt_mod._fetch_channel(yt)
        self.assertIsNone(result)

    def test_returns_none_on_http_error(self):
        yt = _FakeYouTube(
            channels=_FakeChannels(raise_http=True),
            playlist_items=_FakePlaylistItems({}),
            videos=_FakeVideos({}),
        )
        _install_fake_google(yt)
        result = yt_mod._fetch_channel(yt)
        self.assertIsNone(result)

    def test_missing_stats_counts_as_none(self):
        item = {
            "id": "UCtest2",
            "snippet": {"title": "T", "thumbnails": {}, "description": ""},
            "statistics": {},  # no subscriberCount / viewCount / videoCount
            "contentDetails": {"relatedPlaylists": {"uploads": "PL"}},
            "brandingSettings": {},
        }
        yt = _FakeYouTube(
            channels=_FakeChannels(payload=item),
            playlist_items=_FakePlaylistItems({}),
            videos=_FakeVideos({}),
        )
        result = yt_mod._fetch_channel(yt)
        self.assertIsNone(result["subscriber_count"])
        self.assertIsNone(result["view_count"])
        self.assertIsNone(result["video_count"])


# ---------------------------------------------------------------------------
# _fetch_uploads_playlist
# ---------------------------------------------------------------------------


class FetchUploadsPlaylistTest(unittest.TestCase):

    def test_empty_playlist_id_returns_empty(self):
        yt = MagicMock()
        result = yt_mod._fetch_uploads_playlist(yt, "")
        self.assertEqual(result, [])

    def test_paginates_multiple_pages(self):
        pages = {
            None: {
                "items": [{"contentDetails": {"videoId": "V1"}},
                          {"contentDetails": {"videoId": "V2"}}],
                "nextPageToken": "page2",
            },
            "page2": {
                "items": [{"contentDetails": {"videoId": "V3"}}],
            },
        }
        yt = _FakeYouTube(
            channels=_FakeChannels(),
            playlist_items=_FakePlaylistItems(pages),
            videos=_FakeVideos({}),
        )
        result = yt_mod._fetch_uploads_playlist(yt, "PLtest")
        self.assertEqual(result, ["V1", "V2", "V3"])

    def test_http_error_stops_pagination(self):
        yt = _FakeYouTube(
            channels=_FakeChannels(),
            playlist_items=_FakePlaylistItems({}, raise_http=True),
            videos=_FakeVideos({}),
        )
        _install_fake_google(yt)
        result = yt_mod._fetch_uploads_playlist(yt, "PLtest")
        self.assertEqual(result, [])

    def test_skips_items_without_video_id(self):
        pages = {
            None: {
                "items": [
                    {"contentDetails": {}},  # no videoId
                    {"contentDetails": {"videoId": "VALID"}},
                ],
            },
        }
        yt = _FakeYouTube(
            channels=_FakeChannels(),
            playlist_items=_FakePlaylistItems(pages),
            videos=_FakeVideos({}),
        )
        result = yt_mod._fetch_uploads_playlist(yt, "PLtest")
        self.assertEqual(result, ["VALID"])


# ---------------------------------------------------------------------------
# _parse_iso8601_duration
# ---------------------------------------------------------------------------


class ParseISO8601DurationTest(unittest.TestCase):

    def test_none_returns_none(self):
        self.assertIsNone(yt_mod._parse_iso8601_duration(None))

    def test_no_pt_prefix_returns_none(self):
        self.assertIsNone(yt_mod._parse_iso8601_duration("1H30M"))

    def test_hours_minutes_seconds(self):
        self.assertEqual(yt_mod._parse_iso8601_duration("PT1H30M15S"), 5415)

    def test_minutes_only(self):
        self.assertEqual(yt_mod._parse_iso8601_duration("PT5M"), 300)

    def test_seconds_only(self):
        self.assertEqual(yt_mod._parse_iso8601_duration("PT45S"), 45)

    def test_no_num_before_letter_returns_none(self):
        # 'M' with no digit before it
        self.assertIsNone(yt_mod._parse_iso8601_duration("PTM"))

    def test_unknown_unit_returns_none(self):
        self.assertIsNone(yt_mod._parse_iso8601_duration("PT5X"))

    def test_trailing_digit_returns_none(self):
        # Leftover digit after parsing loop
        self.assertIsNone(yt_mod._parse_iso8601_duration("PT5M10"))


# ---------------------------------------------------------------------------
# _fetch_videos_batch
# ---------------------------------------------------------------------------


def _make_video_item(vid: str, **overrides) -> dict:
    item = {
        "id": vid,
        "snippet": {
            "title": f"Title {vid}",
            "description": "",
            "publishedAt": "2024-01-01T00:00:00Z",
            "channelId": "UCtest",
            "channelTitle": "Test",
            "thumbnails": {"maxres": {"url": "https://maxres.jpg"}},
            "tags": ["sports"],
            "categoryId": "17",
        },
        "statistics": {
            "viewCount": "1000",
            "likeCount": "50",
            "commentCount": "5",
            "favoriteCount": "0",
        },
        "contentDetails": {"duration": "PT1M30S"},
        "status": {"privacyStatus": "public", "madeForKids": False},
    }
    item.update(overrides)
    return item


class FetchVideosBatchTest(unittest.TestCase):

    def test_fetches_and_returns_by_id(self):
        vid_item = _make_video_item("VID1")
        yt = _FakeYouTube(
            channels=_FakeChannels(),
            playlist_items=_FakePlaylistItems({}),
            videos=_FakeVideos({"VID1": vid_item}),
        )
        result = yt_mod._fetch_videos_batch(yt, ["VID1"])
        self.assertIn("VID1", result)
        row = result["VID1"]
        self.assertEqual(row["view_count"], 1000)
        self.assertEqual(row["duration_s"], 90)

    def test_batches_at_50_ids(self):
        ids = [f"V{i:03d}" for i in range(60)]
        items = {vid: _make_video_item(vid) for vid in ids}
        calls_made = []

        class TrackingVideos:
            def list(self, *, part, id):
                calls_made.append(id.split(","))
                these_ids = id.split(",")
                return _FakeRequest({"items": [items[i] for i in these_ids if i in items]})

        yt = _FakeYouTube(
            channels=_FakeChannels(),
            playlist_items=_FakePlaylistItems({}),
            videos=TrackingVideos(),
        )
        result = yt_mod._fetch_videos_batch(yt, ids)
        self.assertEqual(len(calls_made), 2)  # 50 + 10
        self.assertEqual(len(result), 60)

    def test_http_error_skips_chunk(self):
        yt = _FakeYouTube(
            channels=_FakeChannels(),
            playlist_items=_FakePlaylistItems({}),
            videos=_FakeVideos({}, raise_http=True),
        )
        _install_fake_google(yt)
        result = yt_mod._fetch_videos_batch(yt, ["VID1"])
        self.assertEqual(result, {})

    def test_missing_stats_fields_produce_none(self):
        vid_item = {
            "id": "VX",
            "snippet": {"title": "T", "description": "", "publishedAt": "",
                        "channelId": "", "channelTitle": "", "thumbnails": {}, "tags": []},
            "statistics": {},  # no counts
            "contentDetails": {"duration": None},
            "status": {"privacyStatus": "private"},
        }
        yt = _FakeYouTube(
            channels=_FakeChannels(),
            playlist_items=_FakePlaylistItems({}),
            videos=_FakeVideos({"VX": vid_item}),
        )
        result = yt_mod._fetch_videos_batch(yt, ["VX"])
        self.assertIsNone(result["VX"]["view_count"])
        self.assertIsNone(result["VX"]["duration_s"])

    def test_thumbnail_fallback_ladder(self):
        # No maxres/standard/high → falls back to medium
        vid_item = {
            "id": "VF",
            "snippet": {
                "title": "T", "description": "", "publishedAt": "",
                "channelId": "", "channelTitle": "",
                "thumbnails": {"medium": {"url": "https://medium.jpg"}},
                "tags": [],
            },
            "statistics": {},
            "contentDetails": {"duration": "PT30S"},
            "status": {},
        }
        yt = _FakeYouTube(
            channels=_FakeChannels(),
            playlist_items=_FakePlaylistItems({}),
            videos=_FakeVideos({"VF": vid_item}),
        )
        result = yt_mod._fetch_videos_batch(yt, ["VF"])
        self.assertEqual(result["VF"]["thumbnail_url"], "https://medium.jpg")


# ---------------------------------------------------------------------------
# fetch_account
# ---------------------------------------------------------------------------


class FetchAccountTest(unittest.TestCase):

    def _build_fake_yt(self, channel_item=None, playlist_pages=None, video_items=None):
        if playlist_pages is None:
            playlist_pages = {}
        if video_items is None:
            video_items = {}
        return _FakeYouTube(
            channels=_FakeChannels(payload=channel_item),
            playlist_items=_FakePlaylistItems(playlist_pages),
            videos=_FakeVideos(video_items),
        )

    def test_auth_failure_returns_none_and_prints(self):
        with patch("pipeline.research.youtube._build_youtube", return_value=None):
            with patch("builtins.print"):
                result = yt_mod.fetch_account("acct")
        self.assertIsNone(result)

    def test_no_channel_returns_none(self):
        fake_yt = self._build_fake_yt(channel_item=None)
        with patch("pipeline.research.youtube._build_youtube", return_value=fake_yt):
            with patch("builtins.print"):
                result = yt_mod.fetch_account("acct")
        self.assertIsNone(result)

    def test_auth_failure_quiet(self):
        with patch("pipeline.research.youtube._build_youtube", return_value=None):
            result = yt_mod.fetch_account("acct", quiet=True)
        self.assertIsNone(result)

    def test_no_channel_quiet(self):
        fake_yt = self._build_fake_yt(channel_item=None)
        with patch("pipeline.research.youtube._build_youtube", return_value=fake_yt):
            result = yt_mod.fetch_account("acct", quiet=True)
        self.assertIsNone(result)

    def test_writes_cache_file(self):
        channel_item = _make_channel_item(uploads_playlist="PLtest")
        playlist_pages = {
            None: {"items": [{"contentDetails": {"videoId": "V1"}}]},
        }
        video_items = {"V1": _make_video_item("V1")}
        fake_yt = self._build_fake_yt(channel_item, playlist_pages, video_items)

        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            with patch("pipeline.research.youtube._build_youtube", return_value=fake_yt):
                with patch.object(yt_mod, "YOUTUBE_DIR", yt_dir):
                    with patch("builtins.print"):
                        result = yt_mod.fetch_account("acct")

            self.assertIsNotNone(result)
            self.assertEqual(result["account"], "acct")
            self.assertEqual(len(result["videos"]), 1)
            self.assertTrue((yt_dir / "acct.json").exists())

    def test_no_video_ids_skips_batch(self):
        """uploads_playlist is empty → no videos.list call."""
        channel_item = _make_channel_item(uploads_playlist="")
        # Empty playlist pages → empty video_ids list
        fake_yt = self._build_fake_yt(channel_item, {}, {})

        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp) / "youtube"
            with patch("pipeline.research.youtube._build_youtube", return_value=fake_yt):
                with patch.object(yt_mod, "YOUTUBE_DIR", yt_dir):
                    with patch("builtins.print"):
                        result = yt_mod.fetch_account("acct")

        self.assertIsNotNone(result)
        self.assertEqual(result["videos"], [])


# ---------------------------------------------------------------------------
# fetch_all
# ---------------------------------------------------------------------------


class FetchAllTest(unittest.TestCase):

    def test_no_channels_returns_zero_summary(self):
        with patch("pipeline.research.youtube.iter_channel_configs", return_value=[]):
            with patch("builtins.print"):
                result = yt_mod.fetch_all()
        self.assertEqual(result["channels"], 0)

    def test_no_channels_quiet(self):
        with patch("pipeline.research.youtube.iter_channel_configs", return_value=[]):
            result = yt_mod.fetch_all(quiet=True)
        self.assertEqual(result["channels"], 0)

    def test_deduplicates_same_account(self):
        # Two channel dirs sharing the same account
        configs = [("acct1", "chan1"), ("acct1", "chan2"), ("acct2", "chan3")]
        fetch_results = {"acct1": {"account": "acct1", "videos": []},
                         "acct2": None}

        def fake_fetch(account, *, quiet=False):
            return fetch_results.get(account)

        with patch("pipeline.research.youtube.iter_channel_configs", return_value=configs):
            with patch("pipeline.research.youtube.fetch_account", side_effect=fake_fetch):
                result = yt_mod.fetch_all(quiet=True)

        self.assertEqual(result["channels"], 2)  # 2 distinct accounts
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["missing_auth"], 1)
        # T1.8: empty lists default for backward-compat consumers.
        self.assertEqual(result["refresh_token_lost"], 0)
        self.assertEqual(result["lost_accounts"], [])

    def test_refresh_token_lost_counted_distinctly(self):
        """Audit T1.8 — fetch_all must surface RefreshTokenLost
        accounts in their own counter + named list so the cron alert
        / dashboard / token-health endpoint can route the operator
        to 're-OAuth this channel' (vs the generic 'auth missing'
        bucket which often resolves on retry)."""
        from pipeline.upload.upload import RefreshTokenLost
        configs = [("a-bad", "ch_a"), ("b-ok", "ch_b"), ("c-bad", "ch_c")]

        def fake_fetch(account, *, quiet=False):
            if account in ("a-bad", "c-bad"):
                raise RefreshTokenLost(account)
            return {"account": account, "videos": []}

        with patch("pipeline.research.youtube.iter_channel_configs", return_value=configs):
            with patch("pipeline.research.youtube.fetch_account", side_effect=fake_fetch):
                with patch("builtins.print"):
                    result = yt_mod.fetch_all(quiet=False)

        self.assertEqual(result["channels"], 3)
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["refresh_token_lost"], 2)
        self.assertEqual(result["lost_accounts"], ["a-bad", "c-bad"])
        # missing_auth bucket is reserved for the OTHER kind of
        # failure (transient / non-typed).
        self.assertEqual(result["missing_auth"], 0)

    def test_no_channels_returns_zero_summary_includes_lost_keys(self):
        # Ensure the empty-channels short-circuit also returns the new
        # keys so consumers can blindly read them.
        with patch("pipeline.research.youtube.iter_channel_configs", return_value=[]):
            with patch("builtins.print"):
                result = yt_mod.fetch_all()
        self.assertIn("refresh_token_lost", result)
        self.assertIn("lost_accounts", result)
        self.assertEqual(result["refresh_token_lost"], 0)
        self.assertEqual(result["lost_accounts"], [])


# ---------------------------------------------------------------------------
# load_account / load_all_cached
# ---------------------------------------------------------------------------


class LoadAccountTest(unittest.TestCase):

    def test_returns_none_when_file_missing(self):
        with patch.object(yt_mod, "YOUTUBE_DIR", Path("/nonexistent/__yt__")):
            result = yt_mod.load_account("acct")
        self.assertIsNone(result)

    def test_returns_payload_when_file_exists(self):
        payload = {"account": "acct", "videos": []}
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp)
            (yt_dir / "acct.json").write_text(json.dumps(payload))
            with patch.object(yt_mod, "YOUTUBE_DIR", yt_dir):
                result = yt_mod.load_account("acct")
        self.assertEqual(result["account"], "acct")

    def test_returns_none_on_bad_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp)
            (yt_dir / "acct.json").write_text("{bad json}")
            with patch.object(yt_mod, "YOUTUBE_DIR", yt_dir):
                result = yt_mod.load_account("acct")
        self.assertIsNone(result)


class LoadAllCachedTest(unittest.TestCase):

    def test_missing_youtube_dir_returns_empty(self):
        with patch.object(yt_mod, "YOUTUBE_DIR", Path("/nonexistent/__yt__")):
            result = yt_mod.load_all_cached()
        self.assertEqual(result, [])

    def test_loads_all_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt_dir = Path(tmp)
            (yt_dir / "a.json").write_text(json.dumps({"account": "a"}))
            (yt_dir / "b.json").write_text(json.dumps({"account": "b"}))
            (yt_dir / "bad.json").write_text("{bad}")
            with patch.object(yt_mod, "YOUTUBE_DIR", yt_dir):
                result = yt_mod.load_all_cached()
        accounts = [r["account"] for r in result]
        self.assertIn("a", accounts)
        self.assertIn("b", accounts)
        self.assertEqual(len(result), 2)


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------


class YouTubeCLITest(unittest.TestCase):

    def _run(self, argv, fake_fetch=None, fake_fetch_all=None, fake_assets=None):
        if fake_fetch is None:
            fake_fetch = MagicMock(return_value=None)
        if fake_fetch_all is None:
            fake_fetch_all = MagicMock(return_value={"channels": 0, "fetched": 0,
                                                      "missing_auth": 0, "videos": 0})
        with patch("sys.argv", ["pipeline.youtube_stats"] + argv):
            with patch("pipeline.research.youtube.fetch_account", fake_fetch):
                with patch("pipeline.research.youtube.fetch_all", fake_fetch_all):
                    # Mock channel_assets so we don't need real downloads
                    fake_ca = MagicMock()
                    fake_ca.download_for_account = MagicMock()
                    fake_ca.download_for_accounts = MagicMock()
                    with patch.dict("sys.modules", {"pipeline.research.channel_assets": fake_ca}):
                        yt_mod.main()

    def test_cli_all_accounts(self):
        self._run([])

    def test_cli_specific_account(self):
        self._run(["--account", "myacct"])

    def test_cli_quiet(self):
        import io
        with patch("sys.stdout", io.StringIO()) as mock_out:
            self._run(["--quiet"])
            output = mock_out.getvalue()
        self.assertIn("{", output)

    def test_cli_quiet_with_account(self):
        import io
        fake_fetch = MagicMock(return_value={"account": "a", "videos": [{"x": 1}]})
        with patch("sys.stdout", io.StringIO()):
            self._run(["--account", "a", "--quiet"], fake_fetch=fake_fetch)


# ---------------------------------------------------------------------------
# E2 — `--token-status` CLI subcommand
# ---------------------------------------------------------------------------


class TokenStatusCLITest(unittest.TestCase):

    def test_returns_zero_when_all_ok(self):
        from io import StringIO

        with patch.object(yt_mod, "iter_channel_configs",
                          return_value=[("acct1", "chan1"), ("acct2", "chan2")]):
            with patch("pipeline.upload.upload.inspect_token_status",
                       side_effect=lambda a: {"account": a, "state": "ok",
                                              "expiry": "2099-01-01"}):
                out = StringIO()
                with patch("sys.stdout", out):
                    rc = yt_mod._cmd_token_status(quiet=False)
        self.assertEqual(rc, 0)
        self.assertIn("All 2 account(s) ok", out.getvalue())

    def test_returns_nonzero_when_any_bad(self):
        from io import StringIO

        with patch.object(yt_mod, "iter_channel_configs",
                          return_value=[("acct1", "c1"), ("acct2", "c2")]):
            statuses = {"acct1": {"account": "acct1", "state": "ok",
                                  "expiry": "2099-01-01"},
                        "acct2": {"account": "acct2", "state": "no_refresh_token",
                                  "expiry": "1970-01-01"}}
            with patch("pipeline.upload.upload.inspect_token_status",
                       side_effect=lambda a: statuses[a]):
                out = StringIO()
                with patch("sys.stdout", out):
                    rc = yt_mod._cmd_token_status(quiet=False)
        self.assertEqual(rc, 1)
        self.assertIn("1/2 account(s) need attention", out.getvalue())
        self.assertIn("re-auth required", out.getvalue())

    def test_quiet_mode_emits_ndjson(self):
        from io import StringIO

        with patch.object(yt_mod, "iter_channel_configs",
                          return_value=[("acct1", "c1")]):
            with patch("pipeline.upload.upload.inspect_token_status",
                       return_value={"account": "acct1", "state": "ok",
                                     "expiry": "2099-01-01"}):
                out = StringIO()
                with patch("sys.stdout", out):
                    rc = yt_mod._cmd_token_status(quiet=True)
        self.assertEqual(rc, 0)
        # NDJSON: one JSON object per line.
        line = out.getvalue().strip()
        import json as _json
        parsed = _json.loads(line)
        self.assertEqual(parsed["account"], "acct1")
        self.assertEqual(parsed["state"], "ok")

    def test_returns_one_when_no_channels(self):
        with patch.object(yt_mod, "iter_channel_configs", return_value=[]):
            from io import StringIO
            out = StringIO()
            with patch("sys.stdout", out):
                rc = yt_mod._cmd_token_status(quiet=False)
        self.assertEqual(rc, 1)
        self.assertIn("No channels discovered", out.getvalue())


if __name__ == "__main__":
    unittest.main()
