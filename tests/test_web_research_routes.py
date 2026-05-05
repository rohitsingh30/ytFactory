"""Tests for the /api/research/* routes added to web/server.py.

Uses FastAPI's TestClient so we never bind a real port. The routes
delegate to pipeline.research; we point research at a tempdir fixture
to keep the test hermetic.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests._helpers import PROJECT_ROOT  # noqa: F401

from fastapi.testclient import TestClient

from pipeline import research
from pipeline.research import aggregator as _research_mod
from web import server
from tests.test_pipeline_research import _build_fixture_tree, _ResearchPatcher


class ResearchRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _build_fixture_tree(self.tmp)
        self.patcher = _ResearchPatcher(self.tmp)
        self.patcher.__enter__()
        # The server's RESEARCH_DIR is read at import time. Re-bind it.
        self._orig_server_research = server.RESEARCH_DIR
        server.RESEARCH_DIR = _research_mod.RESEARCH_DIR
        # Pre-build so the auto-build-on-first-hit path doesn't race.
        _research_mod.rebuild(quiet=True)
        self.client = TestClient(server.app)

    def tearDown(self):
        server.RESEARCH_DIR = self._orig_server_research
        self.patcher.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_videos_route_returns_rows(self):
        r = self.client.get("/api/research/videos")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("videos", body)
        # 3 sports YouTube videos + 1 history = 4 (matches the new fixture).
        self.assertEqual(len(body["videos"]), 4)

    def test_channels_route_returns_one_row_per_config(self):
        r = self.client.get("/api/research/channels")
        self.assertEqual(r.status_code, 200)
        rows = r.json()["channels"]
        self.assertEqual({row["kind"] for row in rows}, {"channel"})
        self.assertEqual(
            {row["channel"] for row in rows},
            {"sportstoriesanimated", "historyrecapped"},
        )

    def test_learnings_route_returns_memory_and_critique(self):
        r = self.client.get("/api/research/learnings")
        self.assertEqual(r.status_code, 200)
        sources = {row["source"] for row in r.json()["learnings"]}
        self.assertEqual(sources, {"memory", "critique"})

    def test_rebuild_returns_counts(self):
        r = self.client.post("/api/research/rebuild")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["videos"], 4)
        self.assertGreater(body["channels"], 0)
        self.assertGreater(body["learnings"], 0)
        self.assertNotIn("analytics", body)  # default: no network call

    def test_rebuild_with_refresh_analytics_calls_fetcher(self):
        # Patch youtube_stats.fetch_all so the route is hermetic.
        from pipeline.research import youtube as youtube_stats
        called = {"n": 0}

        def _fake(**kw):
            called["n"] += 1
            return {"uploads": 0, "fetched": 0, "missing_auth": 0}

        original = youtube_stats.fetch_all
        youtube_stats.fetch_all = _fake
        try:
            r = self.client.post("/api/research/rebuild?refresh_analytics=1")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(called["n"], 1)
            self.assertIn("analytics", r.json())
        finally:
            youtube_stats.fetch_all = original


class AutoBuildOnFirstHitTest(unittest.TestCase):
    """If data/research/*.jsonl don't exist yet, hitting an endpoint
    must build them lazily — so the dashboard works on a fresh checkout."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _build_fixture_tree(self.tmp)
        self.patcher = _ResearchPatcher(self.tmp)
        self.patcher.__enter__()
        self._orig_server_research = server.RESEARCH_DIR
        server.RESEARCH_DIR = _research_mod.RESEARCH_DIR
        # Note: NO pre-build; this is what we're testing.
        self.client = TestClient(server.app)

    def tearDown(self):
        server.RESEARCH_DIR = self._orig_server_research
        self.patcher.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_videos_endpoint_lazy_builds(self):
        # The fixture seeds data/research/analytics/<slug>.json but NOT
        # the videos.jsonl. Confirm it doesn't exist before, exists after.
        videos_path = _research_mod.RESEARCH_DIR / "videos.jsonl"
        if videos_path.exists():
            videos_path.unlink()
        for n in ("channels.jsonl", "learnings.jsonl"):
            p = _research_mod.RESEARCH_DIR / n
            if p.exists():
                p.unlink()
        r = self.client.get("/api/research/videos")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(videos_path.exists())


if __name__ == "__main__":
    unittest.main()
