"""Tests for pipeline.youtube_stats — YouTube-driven enumeration + stats.

We never call the real API. Instead:
  - patch ``pipeline.upload.upload.authenticate`` to return a sentinel
  - inject a fake ``googleapiclient`` whose service stubs return canned
    payloads for ``channels.list``, ``playlistItems.list``, and
    ``videos.list``
  - assert the fetcher writes the right per-account cache shape and
    paginates / batches correctly
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.research import youtube as youtube_stats


# ---- fake googleapiclient -----------------------------------------------


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeChannels:
    def __init__(self, channel_payload):
        self.payload = channel_payload
        self.calls = 0

    def list(self, *, part, mine):
        self.calls += 1
        return _FakeRequest({"items": [self.payload]} if self.payload else {"items": []})


class _FakePlaylistItems:
    """Multiple pages keyed by pageToken."""

    def __init__(self, pages_by_token: dict[str | None, dict]):
        self.pages_by_token = pages_by_token
        self.calls: list[dict] = []

    def list(self, *, part, playlistId, maxResults, pageToken):
        self.calls.append(
            {"playlistId": playlistId, "pageToken": pageToken, "maxResults": maxResults}
        )
        return _FakeRequest(self.pages_by_token.get(pageToken, {"items": []}))


class _FakeVideos:
    def __init__(self, return_by_id):
        self.return_by_id = return_by_id
        self.calls: list[list[str]] = []

    def list(self, *, part, id):
        ids = id.split(",")
        self.calls.append(ids)
        items = [
            {**({"id": vid}), **payload}
            for vid, payload in self.return_by_id.items()
            if vid in ids
        ]
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


def _install_fake_googleapiclient(youtube: _FakeYouTube) -> None:
    """Inject a fake ``googleapiclient`` package into sys.modules."""
    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = lambda *a, **kw: youtube
    errors = types.ModuleType("googleapiclient.errors")

    class HttpError(Exception):
        pass

    errors.HttpError = HttpError
    pkg = types.ModuleType("googleapiclient")
    pkg.discovery = discovery
    pkg.errors = errors
    sys.modules["googleapiclient"] = pkg
    sys.modules["googleapiclient.discovery"] = discovery
    sys.modules["googleapiclient.errors"] = errors


# ---- shared fixtures ----------------------------------------------------


def _write_channel_dir(root: Path, name: str, *, account: str | None = None) -> None:
    """Create a minimal ``<channel>/config.yaml`` so iter_channel_configs
    picks the channel up."""
    chan = root / name
    chan.mkdir(parents=True)
    cfg = f"name: {name}\n"
    if account:
        cfg += f"upload:\n  account: {account}\n"
    (chan / "config.yaml").write_text(cfg)


class _PointAtTmp:
    """Re-point youtube_stats path globals at a tempdir."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._saved: dict[str, Path] = {}

    def __enter__(self):
        self._saved = {
            "PROJECT_ROOT": youtube_stats.PROJECT_ROOT,
            "RESEARCH_DIR": youtube_stats.RESEARCH_DIR,
            "YOUTUBE_DIR": youtube_stats.YOUTUBE_DIR,
        }
        youtube_stats.PROJECT_ROOT = self.root
        youtube_stats.RESEARCH_DIR = self.root / "data" / "research"
        youtube_stats.YOUTUBE_DIR = self.root / "data" / "research" / "youtube"
        return self

    def __exit__(self, *a):
        for k, v in self._saved.items():
            setattr(youtube_stats, k, v)


# ---- tests --------------------------------------------------------------


class FetchAccountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _write_channel_dir(self.tmp, "sportsrecapped", account="sportsrecapped")

        # 3 videos, two pages of playlistItems (2 + 1) to verify pagination.
        channel_payload = {
            "id": "UC_sports",
            "snippet": {"title": "SportsRecapped", "description": "Sports"},
            "statistics": {
                "subscriberCount": "100",
                "viewCount": "12345",
                "videoCount": "3",
                "hiddenSubscriberCount": False,
            },
            "contentDetails": {"relatedPlaylists": {"uploads": "UU_sports"}},
        }
        self.channels = _FakeChannels(channel_payload)
        self.playlist = _FakePlaylistItems({
            None: {
                "items": [
                    {"contentDetails": {"videoId": "VID_A"}},
                    {"contentDetails": {"videoId": "VID_B"}},
                ],
                "nextPageToken": "PAGE2",
            },
            "PAGE2": {
                "items": [{"contentDetails": {"videoId": "VID_C"}}],
            },
        })
        self.videos = _FakeVideos({
            "VID_A": {
                "snippet": {
                    "title": "Vid A",
                    "publishedAt": "2026-05-02T18:00:00Z",
                    "channelId": "UC_sports",
                    "channelTitle": "SportsRecapped",
                    "thumbnails": {"high": {"url": "https://img/a.jpg"}},
                    "categoryId": "17",
                },
                "statistics": {"viewCount": "1000", "likeCount": "50", "commentCount": "5"},
                "contentDetails": {"duration": "PT1M"},
                "status": {"privacyStatus": "public"},
            },
            "VID_B": {
                "snippet": {
                    "title": "Vid B",
                    "publishedAt": "2026-05-03T18:00:00Z",
                    "thumbnails": {"default": {"url": "https://img/b.jpg"}},
                },
                "statistics": {"viewCount": "200"},
                "contentDetails": {"duration": "PT55S"},
                "status": {"privacyStatus": "private"},
            },
            "VID_C": {
                "snippet": {"title": "Vid C", "thumbnails": {}},
                "statistics": {},
                "contentDetails": {"duration": "PT2M30S"},
                "status": {},
            },
        })
        _install_fake_googleapiclient(_FakeYouTube(
            channels=self.channels, playlist_items=self.playlist, videos=self.videos,
        ))
        from pipeline.upload import upload as up
        self._orig_auth = up.authenticate
        up.authenticate = lambda **kw: object()
        self.point = _PointAtTmp(self.tmp).__enter__()

    def tearDown(self):
        self.point.__exit__(None, None, None)
        from pipeline.upload import upload as up
        up.authenticate = self._orig_auth
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_account_cache(self):
        result = youtube_stats.fetch_account("sportsrecapped", quiet=True)
        self.assertIsNotNone(result)
        cache = json.loads(
            (youtube_stats.YOUTUBE_DIR / "sportsrecapped.json").read_text()
        )
        self.assertEqual(cache["account"], "sportsrecapped")
        self.assertEqual(cache["channel"]["id"], "UC_sports")
        self.assertEqual(cache["channel"]["subscriber_count"], 100)
        self.assertEqual(len(cache["videos"]), 3)

    def test_video_rows_have_full_shape(self):
        youtube_stats.fetch_account("sportsrecapped", quiet=True)
        cache = json.loads(
            (youtube_stats.YOUTUBE_DIR / "sportsrecapped.json").read_text()
        )
        a = next(v for v in cache["videos"] if v["video_id"] == "VID_A")
        self.assertEqual(a["title"], "Vid A")
        self.assertEqual(a["view_count"], 1000)
        self.assertEqual(a["like_count"], 50)
        self.assertEqual(a["duration_s"], 60)
        self.assertEqual(a["privacy"], "public")
        self.assertEqual(a["thumbnail_url"], "https://img/a.jpg")
        self.assertEqual(a["url"], "https://youtu.be/VID_A")

    def test_missing_stats_become_none(self):
        youtube_stats.fetch_account("sportsrecapped", quiet=True)
        cache = json.loads(
            (youtube_stats.YOUTUBE_DIR / "sportsrecapped.json").read_text()
        )
        c = next(v for v in cache["videos"] if v["video_id"] == "VID_C")
        self.assertIsNone(c["view_count"])
        self.assertIsNone(c["like_count"])
        self.assertEqual(c["duration_s"], 150)  # 2m30s

    def test_paginates_playlist_items(self):
        youtube_stats.fetch_account("sportsrecapped", quiet=True)
        # 2 calls: pageToken=None then pageToken="PAGE2".
        self.assertEqual([c["pageToken"] for c in self.playlist.calls], [None, "PAGE2"])

    def test_preserves_playlist_order(self):
        youtube_stats.fetch_account("sportsrecapped", quiet=True)
        cache = json.loads(
            (youtube_stats.YOUTUBE_DIR / "sportsrecapped.json").read_text()
        )
        self.assertEqual(
            [v["video_id"] for v in cache["videos"]], ["VID_A", "VID_B", "VID_C"]
        )


class VideosBatchingTest(unittest.TestCase):
    """videos.list caps at 50 IDs per call — verify chunking on a 73-video channel."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _write_channel_dir(self.tmp, "biggie", account="biggie")
        ids = [f"V_{i:03d}" for i in range(73)]
        # One playlist page with 73 items (plausible if API allows it,
        # and good enough for the test — youtube_stats batches videos.list,
        # not playlistItems).
        playlist = _FakePlaylistItems({
            None: {"items": [{"contentDetails": {"videoId": v}} for v in ids]}
        })
        channels = _FakeChannels({
            "id": "UC_b",
            "snippet": {"title": "Biggie"},
            "statistics": {},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU_b"}},
        })
        self.videos = _FakeVideos({
            v: {"snippet": {"title": v, "thumbnails": {}}, "statistics": {}, "contentDetails": {}, "status": {}}
            for v in ids
        })
        _install_fake_googleapiclient(_FakeYouTube(
            channels=channels, playlist_items=playlist, videos=self.videos,
        ))
        from pipeline.upload import upload as up
        self._orig_auth = up.authenticate
        up.authenticate = lambda **kw: object()
        self.point = _PointAtTmp(self.tmp).__enter__()

    def tearDown(self):
        self.point.__exit__(None, None, None)
        from pipeline.upload import upload as up
        up.authenticate = self._orig_auth
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chunks_videos_list_at_50(self):
        youtube_stats.fetch_account("biggie", quiet=True)
        chunk_sizes = [len(call) for call in self.videos.calls]
        self.assertEqual(chunk_sizes, [50, 23])


class NoAuthTest(unittest.TestCase):
    """If authenticate() raises, fetch_account returns None — no exception."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _write_channel_dir(self.tmp, "ghosted", account="ghosted")
        # Even though no API call should reach these, install harmless fakes.
        _install_fake_googleapiclient(_FakeYouTube(
            channels=_FakeChannels(None),
            playlist_items=_FakePlaylistItems({}),
            videos=_FakeVideos({}),
        ))
        from pipeline.upload import upload as up
        self._orig_auth = up.authenticate
        def _raise(**kw):
            raise RuntimeError("no token")
        up.authenticate = _raise
        self.point = _PointAtTmp(self.tmp).__enter__()

    def tearDown(self):
        self.point.__exit__(None, None, None)
        from pipeline.upload import upload as up
        up.authenticate = self._orig_auth
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fetch_account_returns_none(self):
        self.assertIsNone(youtube_stats.fetch_account("ghosted", quiet=True))

    def test_fetch_all_skips_and_counts_missing_auth(self):
        summary = youtube_stats.fetch_all(quiet=True)
        self.assertEqual(summary["channels"], 1)
        self.assertEqual(summary["fetched"], 0)
        self.assertEqual(summary["missing_auth"], 1)


class IterChannelConfigsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _write_channel_dir(self.tmp, "sportsrecapped", account="sportsrecapped")
        _write_channel_dir(self.tmp, "historyrecapped", account="historyrecapped")
        # A non-channel dir — must NOT be picked up.
        (self.tmp / "pipeline").mkdir()
        (self.tmp / "pipeline" / "config.yaml").write_text("name: ignored\n")
        # A channel dir without config.yaml — also skipped.
        (self.tmp / "untouched").mkdir()
        self.point = _PointAtTmp(self.tmp).__enter__()

    def tearDown(self):
        self.point.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_only_top_level_channel_dirs(self):
        accounts = youtube_stats.iter_channel_configs()
        names = sorted(a for a, _ in accounts)
        self.assertEqual(names, ["historyrecapped", "sportsrecapped"])


class LoadAccountTest(unittest.TestCase):
    def test_returns_none_when_missing(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            with _PointAtTmp(tmp):
                self.assertIsNone(youtube_stats.load_account("ghost"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
