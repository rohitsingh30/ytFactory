"""Tests for pipeline.youtube_stats — public-stats fetcher.

We never call the real API. Instead:
  - patch ``pipeline.upload.authenticate`` to return a sentinel
  - patch ``googleapiclient.discovery.build`` to return a fake youtube
    service whose ``.videos().list().execute()`` returns canned rows
  - assert the fetcher writes the right per-slug JSON and batches by
    50 ids per call
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

from pipeline import youtube_stats


# ---- fake googleapiclient -----------------------------------------------


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeVideos:
    """Records every videos.list() call so tests can assert batching."""

    def __init__(self, return_by_id):
        self.return_by_id = return_by_id
        self.calls: list[list[str]] = []

    def list(self, *, part: str, id: str):
        ids = id.split(",")
        self.calls.append(ids)
        items = [
            {"id": vid, "statistics": stats}
            for vid, stats in self.return_by_id.items()
            if vid in ids
        ]
        return _FakeRequest({"items": items})


class _FakeYouTube:
    def __init__(self, fake_videos: _FakeVideos):
        self._videos = fake_videos

    def videos(self):
        return self._videos


def _install_fake_googleapiclient(fake_videos: _FakeVideos) -> None:
    """Inject a fake googleapiclient.discovery / .errors into sys.modules
    so the real package is never imported. Idempotent."""
    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = lambda *a, **kw: _FakeYouTube(fake_videos)
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


# ---- shared fixture -----------------------------------------------------


def _build_uploads_tree(root: Path, slug_to_vid: dict[str, str], account: str) -> None:
    base = root / "data" / "uploads" / account
    base.mkdir(parents=True)
    for slug, vid in slug_to_vid.items():
        (base / f"{slug}.json").write_text(
            json.dumps(
                {
                    "slug": slug,
                    "video_id": vid,
                    "url": f"https://youtu.be/{vid}",
                    "account": account,
                }
            )
        )


class _PointAtTmp:
    """Re-point youtube_stats.{ANALYTICS_DIR,UPLOADS_DIR} at a tempdir."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._saved: dict[str, Path] = {}

    def __enter__(self):
        self._saved = {
            "ANALYTICS_DIR": youtube_stats.ANALYTICS_DIR,
            "UPLOADS_DIR": youtube_stats.UPLOADS_DIR,
        }
        youtube_stats.ANALYTICS_DIR = self.root / "data" / "research" / "analytics"
        youtube_stats.UPLOADS_DIR = self.root / "data" / "uploads"
        return self

    def __exit__(self, *a):
        for k, v in self._saved.items():
            setattr(youtube_stats, k, v)


# ---- tests --------------------------------------------------------------


class FetchAllTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Three uploaded videos under one account.
        _build_uploads_tree(
            self.tmp,
            {"slug-a": "VID_A", "slug-b": "VID_B", "slug-c": "VID_C"},
            "sportstoriesanimated",
        )
        self.fake_videos = _FakeVideos(
            {
                "VID_A": {"viewCount": "1000", "likeCount": "50", "commentCount": "5"},
                "VID_B": {"viewCount": "2000", "likeCount": "100", "commentCount": "20"},
                # VID_C absent → simulates the API not returning a row
            }
        )
        _install_fake_googleapiclient(self.fake_videos)
        # Patch authenticate to return a sentinel — no real auth.
        from pipeline import upload as up
        self._orig_auth = up.authenticate
        up.authenticate = lambda **kw: object()
        self.point = _PointAtTmp(self.tmp).__enter__()

    def tearDown(self):
        self.point.__exit__(None, None, None)
        from pipeline import upload as up
        up.authenticate = self._orig_auth
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_one_json_per_slug_present_in_response(self):
        summary = youtube_stats.fetch_all(quiet=True)
        self.assertEqual(summary["uploads"], 3)
        self.assertEqual(summary["fetched"], 3)  # all three slugs get a file even if some have null counts
        for slug in ("slug-a", "slug-b", "slug-c"):
            p = youtube_stats.ANALYTICS_DIR / f"{slug}.json"
            self.assertTrue(p.exists())

    def test_view_counts_parsed_as_int(self):
        youtube_stats.fetch_all(quiet=True)
        a = json.loads((youtube_stats.ANALYTICS_DIR / "slug-a.json").read_text())
        self.assertEqual(a["view_count"], 1000)
        self.assertEqual(a["like_count"], 50)
        self.assertEqual(a["comment_count"], 5)

    def test_missing_in_response_yields_null_counts(self):
        youtube_stats.fetch_all(quiet=True)
        c = json.loads((youtube_stats.ANALYTICS_DIR / "slug-c.json").read_text())
        self.assertIsNone(c["view_count"])
        self.assertIsNone(c["like_count"])

    def test_includes_fetched_at_iso(self):
        youtube_stats.fetch_all(quiet=True)
        a = json.loads((youtube_stats.ANALYTICS_DIR / "slug-a.json").read_text())
        self.assertTrue(a["fetched_at"].startswith("20"))
        self.assertIn("T", a["fetched_at"])  # ISO format check


class BatchingTest(unittest.TestCase):
    """videos.list caps at 50 IDs per call — verify the chunking."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # 73 uploads → expect 2 calls (50 + 23).
        _build_uploads_tree(
            self.tmp,
            {f"slug-{i:03d}": f"VID_{i:03d}" for i in range(73)},
            "mystoriesanimated",
        )
        self.fake_videos = _FakeVideos({})
        _install_fake_googleapiclient(self.fake_videos)
        from pipeline import upload as up
        self._orig_auth = up.authenticate
        up.authenticate = lambda **kw: object()
        self.point = _PointAtTmp(self.tmp).__enter__()

    def tearDown(self):
        self.point.__exit__(None, None, None)
        from pipeline import upload as up
        up.authenticate = self._orig_auth
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chunks_at_50(self):
        # The API would have returned an empty items list; the fetcher
        # treats that as "no auth or API error" and skips writing.
        # To exercise chunking we need the response to be non-empty for
        # both calls. Pre-seed the fake with one matching id per chunk.
        self.fake_videos.return_by_id = {
            "VID_000": {"viewCount": "1"},
            "VID_050": {"viewCount": "2"},
        }
        youtube_stats.fetch_all(quiet=True)
        chunk_sizes = [len(call) for call in self.fake_videos.calls]
        self.assertEqual(chunk_sizes, [50, 23])


class NoAuthTest(unittest.TestCase):
    """If authenticate() raises, the account is skipped — no exception
    propagates, missing_auth counter increments."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _build_uploads_tree(self.tmp, {"only-one": "VID_X"}, "sportstoriesanimated")
        _install_fake_googleapiclient(_FakeVideos({}))
        from pipeline import upload as up
        self._orig_auth = up.authenticate

        def _raise(**kw):
            raise RuntimeError("no token")

        up.authenticate = _raise
        self.point = _PointAtTmp(self.tmp).__enter__()

    def tearDown(self):
        self.point.__exit__(None, None, None)
        from pipeline import upload as up
        up.authenticate = self._orig_auth
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_skips_without_raising(self):
        summary = youtube_stats.fetch_all(quiet=True)
        self.assertEqual(summary["uploads"], 1)
        self.assertEqual(summary["fetched"], 0)
        self.assertEqual(summary["missing_auth"], 1)


class LoadForSlugTest(unittest.TestCase):
    def test_returns_none_when_missing(self):
        self.tmp = Path(tempfile.mkdtemp())
        try:
            with _PointAtTmp(self.tmp):
                self.assertIsNone(youtube_stats.load_for_slug("ghost"))
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
