"""Tests for pipeline/research/youtube.py — YouTube stats fetcher.

All YouTube API calls are mocked.  No network, no credentials.
"""

from __future__ import annotations

import json
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
    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = lambda *a, **kw: youtube_obj
    errors = types.ModuleType("googleapiclient.errors")

    class HttpError(Exception):
        def __init__(self, resp, content=b""):
            self.resp = resp
            self.content = content
            super().__init__(str(resp))

    errors.HttpError = HttpError

    sys.modules["googleapiclient"] = types.ModuleType("googleapiclient")
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


if __name__ == "__main__":
    unittest.main()
