"""100% coverage for control/dashboard_routes.py."""
from __future__ import annotations

import io
import json
import os
import shutil
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import httpx
from fastapi import FastAPI

from tests._helpers import PROJECT_ROOT
import control.dashboard_routes as dr


SCRATCH = PROJECT_ROOT / "tests" / "_scratch_routes_dashboard"


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(dr.router)
    return app


class _Resp:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    def read(self) -> bytes:
        return self._body


def _http_error(code: int = 403, body: bytes = b'{"error":{"message":"quota"}}') -> HTTPError:
    return HTTPError("https://api", code, "bad", hdrs=None, fp=io.BytesIO(body))


class DashboardBase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True)
        dr._STATS_CACHE.clear()
        dr._CHANNEL_STATS_CACHE.clear()
        dr._ACCOUNT_TO_CHANNEL_ID.clear()
        dr._LAST_ENUMERATE_SOURCE = None
        dr._LAST_FETCH_ERROR = None
        self._old_api = os.environ.get("YOUTUBE_API_KEY")
        os.environ.pop("YOUTUBE_API_KEY", None)

    def tearDown(self) -> None:
        if self._old_api is None:
            os.environ.pop("YOUTUBE_API_KEY", None)
        else:
            os.environ["YOUTUBE_API_KEY"] = self._old_api
        shutil.rmtree(SCRATCH, ignore_errors=True)
        dr._STATS_CACHE.clear()
        dr._CHANNEL_STATS_CACHE.clear()
        dr._ACCOUNT_TO_CHANNEL_ID.clear()
        dr._LAST_ENUMERATE_SOURCE = None
        dr._LAST_FETCH_ERROR = None


class EnumerateUploadsTest(DashboardBase):
    def test_gcs_disk_dedupe_and_source_tracking(self) -> None:
        chan = SCRATCH / "chan"
        (chan / "uploads").mkdir(parents=True)
        (chan / "config.yaml").write_text("name: chan")
        (chan / "uploads" / "diskonly.json").write_text(json.dumps({"video_id": "diskvid", "slug": "diskslug"}))
        (chan / "uploads" / "dup.json").write_text(json.dumps({"video_id": "dupvid", "slug": "dup"}))
        (chan / "uploads" / "novid.json").write_text(json.dumps({"slug": "novid"}))
        (chan / "uploads" / "bad.json").write_text("{")
        (chan / "uploads" / "skip.x.json").write_text(json.dumps({"video_id": "x"}))
        (chan / "niche" / "uploads").mkdir(parents=True)
        (chan / "niche" / "uploads" / "nested.json").write_text(json.dumps({"video_id": "nested"}))
        (SCRATCH / "notchan").mkdir()

        gcs_rows = [("chan", "dup", {"video_id": "gcsdup"}), ("chan", "dup", {"video_id": "gcsdup2"}), ("gcschan", "gcslug", {"video_id": "gcsvid"}), ("bad", "no", {})]
        with patch.object(dr, "PROJECT_ROOT", SCRATCH), \
             patch("control.storage.list_upload_records", return_value=gcs_rows):
            rows = dr._enumerate_uploads()
        keys = {(c, s, v) for c, s, v, _ in rows}
        self.assertIn(("gcschan", "gcslug", "gcsvid"), keys)
        self.assertIn(("chan", "diskslug", "diskvid"), keys)
        self.assertIn(("chan", "nested", "nested"), keys)
        self.assertIn(("chan", "dup", "gcsdup"), keys)
        self.assertNotIn(("chan", "dup", "dupvid"), keys)
        self.assertIn("gcs:2", dr._LAST_ENUMERATE_SOURCE or "")
        self.assertIn("disk:2", dr._LAST_ENUMERATE_SOURCE or "")

    def test_gcs_error_and_empty_sources(self) -> None:
        with patch.object(dr, "PROJECT_ROOT", SCRATCH), \
             patch("control.storage.list_upload_records", side_effect=RuntimeError("no gcs")):
            rows = dr._enumerate_uploads()
        self.assertEqual(rows, [])
        self.assertIn("gcs:err(RuntimeError)", dr._LAST_ENUMERATE_SOURCE or "")

        with patch.object(dr, "PROJECT_ROOT", SCRATCH), \
             patch("control.storage.list_upload_records", return_value=[]):
            rows = dr._enumerate_uploads()
        self.assertEqual(rows, [])
        self.assertEqual(dr._LAST_ENUMERATE_SOURCE, "empty")


class FetchStatsTest(DashboardBase):
    def test_fetch_stats_no_key_and_empty_ids(self) -> None:
        self.assertEqual(dr._fetch_stats(["vid"]), {})
        self.assertEqual(dr._LAST_FETCH_ERROR, "YOUTUBE_API_KEY not set")
        os.environ["YOUTUBE_API_KEY"] = "key"
        self.assertEqual(dr._fetch_stats([]), {})

    def test_fetch_stats_success_marks_visible_and_refreshes_channels(self) -> None:
        os.environ["YOUTUBE_API_KEY"] = "key"
        ids = [f"v{i}" for i in range(51)]
        first = {"items": [{"id": "v0", "statistics": {"viewCount": "10", "likeCount": "2", "commentCount": "1"}, "status": {"privacyStatus": "public"}, "snippet": {"channelId": "cid"}}]}
        second = {"items": []}
        with patch("urllib.request.urlopen", side_effect=[_Resp(first), _Resp(second)]), \
             patch.object(dr, "_refresh_channel_stats") as refresh:
            out = dr._fetch_stats(ids)
        self.assertTrue(out["v0"]["api_visible"])
        self.assertFalse(out["v1"]["api_visible"])
        self.assertEqual(out["v0"]["viewCount"], 10)
        refresh.assert_called_once_with(["cid"], "key")

    def test_fetch_stats_http_and_generic_errors(self) -> None:
        os.environ["YOUTUBE_API_KEY"] = "key"
        with patch("urllib.request.urlopen", side_effect=_http_error(403)):
            out = dr._fetch_stats(["v"])
        self.assertFalse(out["v"]["api_visible"])
        self.assertIn("HTTP 403", dr._LAST_FETCH_ERROR or "")

        with patch("urllib.request.urlopen", side_effect=_http_error(500, b"not json")):
            dr._fetch_stats(["v"])
        self.assertIn("HTTP 500", dr._LAST_FETCH_ERROR or "")

        with patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
            dr._fetch_stats(["v"])
        self.assertIn("RuntimeError: offline", dr._LAST_FETCH_ERROR or "")

    def test_refresh_channel_stats_success_and_errors(self) -> None:
        dr._refresh_channel_stats([], "key")
        data = {"items": [
            {"id": "cid", "statistics": {"subscriberCount": "5", "viewCount": "100", "videoCount": "7", "hiddenSubscriberCount": True}, "snippet": {"title": "Channel"}},
            {"statistics": {}, "snippet": {}},
        ]}
        with patch("urllib.request.urlopen", return_value=_Resp(data)):
            dr._refresh_channel_stats(["cid"], "key")
        self.assertEqual(dr._CHANNEL_STATS_CACHE["cid"]["subscriber_count"], 5)
        self.assertTrue(dr._CHANNEL_STATS_CACHE["cid"]["hidden_subscribers"])

        for exc in (_http_error(), URLError("down"), OSError("io"), ValueError("weird")):
            with patch("urllib.request.urlopen", side_effect=exc):
                dr._refresh_channel_stats(["other"], "key")

    def test_refresh_and_ensure_fresh(self) -> None:
        with patch.object(dr, "_fetch_stats", return_value={"v": {"viewCount": 1}}):
            dr._refresh(["v"])
        self.assertEqual(dr._STATS_CACHE["v"]["viewCount"], 1)
        self.assertIn("fetched_at", dr._STATS_CACHE["v"])

        now = time.time()
        dr._STATS_CACHE["fresh"] = {"fetched_at": now}
        dr._STATS_CACHE["old"] = {"fetched_at": now - dr._STATS_TTL_S - 1}
        with patch.object(dr, "_refresh") as refresh:
            dr._ensure_fresh(["fresh", "old", "missing"])
        refresh.assert_called_once_with(["old", "missing"])
        with patch.object(dr, "_refresh") as refresh:
            dr._ensure_fresh(["fresh"])
        refresh.assert_not_called()


class DashboardRoutesTest(DashboardBase, unittest.IsolatedAsyncioTestCase):
    async def _client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app()), base_url="http://test")

    async def test_dashboard_static_favicon_and_stub_routes(self) -> None:
        static = SCRATCH / "static"
        static.mkdir()
        (static / "dashboard.html").write_text("<html>ok</html>")
        with patch.object(dr, "STATIC_DIR", static):
            async with await self._client() as c:
                page = await c.get("/dashboard")
                favicon = await c.get("/favicon.ico")
                # /overview is now served by control.routes.telemetry_routes
                # (real handler). The dashboard router only stubs the endpoints
                # without a real impl yet — /llm + /latency.
                telemetry = await c.get("/api/telemetry/llm")
                research = await c.get("/api/research/videos")
                rebuild = await c.post("/api/research/rebuild")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(favicon.status_code, 204)
        self.assertIn("warning", telemetry.json())
        self.assertIn("items", research.json())
        self.assertFalse(rebuild.json()["ok"])

        with patch.object(dr, "STATIC_DIR", SCRATCH / "missing"):
            async with await self._client() as c:
                missing = await c.get("/dashboard")
        self.assertEqual(missing.status_code, 404)

    async def test_dashboard_videos_empty(self) -> None:
        with patch.object(dr, "_enumerate_uploads", return_value=[]):
            async with await self._client() as c:
                r = await c.get("/api/dashboard/videos")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["totals"]["videos"], 0)
        self.assertIn("No upload records", r.json()["warning"])

    async def test_dashboard_videos_cached_no_api_key_warning(self) -> None:
        fetched = time.time() - 10
        dr._STATS_CACHE.update({
            "vid1": {"viewCount": 10, "likeCount": 2, "commentCount": 1, "fetched_at": fetched, "api_visible": True, "privacy_live": "private", "channel_id": "cid1"},
            "vid2": {"viewCount": None, "likeCount": None, "commentCount": None, "fetched_at": fetched, "api_visible": False, "privacy_live": None},
        })
        dr._CHANNEL_STATS_CACHE["cid1"] = {"subscriber_count": 9, "hidden_subscribers": False}
        uploads = [
            ("acct", "shortslug", "vid1", {"title": None, "uploaded_at": "2026-01-02", "privacy": "public", "mp4_path": "short.mp4"}),
            ("acct", "longslug", "vid2", {"title": "Long", "uploaded_at": "2026-01-01", "privacy": "unlisted", "url": "https://watch", "mp4_path": "movie.mov"}),
        ]
        dr._LAST_ENUMERATE_SOURCE = "test"
        with patch.object(dr, "_enumerate_uploads", return_value=uploads), \
             patch.object(dr, "_ensure_fresh") as ensure:
            async with await self._client() as c:
                r = await c.get("/api/dashboard/videos")
        self.assertEqual(r.status_code, 200, r.text)
        ensure.assert_called_once_with(["vid1", "vid2"])
        body = r.json()
        self.assertEqual(body["totals"]["videos"], 2)
        self.assertEqual(body["totals"]["views"], 10)
        self.assertEqual(body["totals"]["subscribers"], 9)
        self.assertIn("YOUTUBE_API_KEY not set", body["warning"])
        videos = body["channels"][0]["videos"]
        self.assertEqual(videos[0]["watch_url"], "https://youtube.com/shorts/vid1")
        self.assertEqual(videos[1]["watch_url"], "https://watch")
        self.assertEqual(videos[0]["privacy"], "private")
        self.assertEqual(videos[1]["privacy"], "unlisted")

    async def test_dashboard_videos_refresh_api_error_and_private_resolution(self) -> None:
        os.environ["YOUTUBE_API_KEY"] = "key"
        fetched = time.time()
        dr._LAST_FETCH_ERROR = "quota exceeded"
        dr._STATS_CACHE.update({
            "vid1": {"viewCount": 1, "likeCount": 0, "commentCount": 0, "fetched_at": fetched, "api_visible": False, "privacy_live": None},
            "vid2": {"viewCount": 2, "likeCount": 1, "commentCount": 1, "fetched_at": fetched, "api_visible": True, "privacy_live": None},
        })
        uploads = [
            ("zacct", "fallback", "vid1", {"slug": "fallback", "privacy": "public"}),
            ("zacct", "youtu", "vid2", {"slug": "youtu", "privacy": "public"}),
        ]
        dr._LAST_ENUMERATE_SOURCE = "test"
        with patch.object(dr, "_enumerate_uploads", return_value=uploads), \
             patch.object(dr, "_refresh") as refresh:
            async with await self._client() as c:
                r = await c.get("/api/dashboard/videos?refresh=true")
        self.assertEqual(r.status_code, 200, r.text)
        refresh.assert_called_once_with(["vid1", "vid2"])
        body = r.json()
        self.assertIn("YouTube API error", body["warning"])
        videos = body["channels"][0]["videos"]
        self.assertEqual(videos[0]["privacy"], "private/unlisted")
        self.assertEqual(videos[0]["watch_url"], "https://youtu.be/vid1")
        self.assertEqual(body["totals"]["comments"], 1)


if __name__ == "__main__":
    unittest.main()
