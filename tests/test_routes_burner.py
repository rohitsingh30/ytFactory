"""Tests for control/routes/burner_routes.py — 100% line coverage."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

import httpx

from control.routes.burner_routes import router
from fastapi import FastAPI


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


class TestListBurners(unittest.IsolatedAsyncioTestCase):
    async def test_list_empty(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[]):
            with patch("control.routes.burner_routes.catalog.catalog_count", return_value=42):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.get("/api/burner_channels")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["burners"], [])
        self.assertEqual(data["catalog_size"], 42)

    async def test_list_with_burners(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner1", "profile_known": True}
        state = {"phase": "liking", "last_action_at": "2026-01-01", "last_action_msg": "ok"}
        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]):
            with patch("control.routes.burner_routes.burner_engage.read_state",
                       return_value=state):
                with patch("control.routes.burner_routes.burner_engage.is_running",
                           return_value=True):
                    with patch("control.routes.burner_routes.catalog.catalog_count",
                               return_value=5):
                        async with httpx.AsyncClient(transport=transport,
                                                     base_url="http://test") as client:
                            r = await client.get("/api/burner_channels")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data["burners"]), 1)
        self.assertEqual(data["burners"][0]["phase"], "liking")
        self.assertTrue(data["burners"][0]["running"])

    async def test_list_with_null_state(self) -> None:
        """Burner with no state (never run)."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner2", "profile_known": False}
        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]):
            with patch("control.routes.burner_routes.burner_engage.read_state",
                       return_value=None):
                with patch("control.routes.burner_routes.burner_engage.is_running",
                           return_value=False):
                    with patch("control.routes.burner_routes.catalog.catalog_count",
                               return_value=0):
                        async with httpx.AsyncClient(transport=transport,
                                                     base_url="http://test") as client:
                            r = await client.get("/api/burner_channels")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["burners"][0]["phase"])


class TestGetCatalog(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_returns_rows(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        rows = [{"video_id": "abc", "title": "Test"}]
        with patch("control.routes.burner_routes.catalog.list_catalog_dicts",
                   return_value=rows):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/burner_channels/catalog")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["videos"][0]["video_id"], "abc")


class TestStartEngage(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_slug_404(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[]):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/burner_channels/unknown/engage")
        self.assertEqual(r.status_code, 404)

    async def test_no_profile_known_409(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner1", "profile_known": False}
        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/burner_channels/burner1/engage")
        self.assertEqual(r.status_code, 409)

    async def test_already_running(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner1", "profile_known": True}
        state = {"phase": "liking"}
        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]):
            with patch("control.routes.burner_routes.burner_engage.is_running",
                       return_value=True):
                with patch("control.routes.burner_routes.burner_engage.read_state",
                           return_value=state):
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://test") as client:
                        r = await client.post("/api/burner_channels/burner1/engage")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(data["started"])
        self.assertEqual(data["reason"], "already_running")

    async def test_spawn_new_process(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        burner = {"slug": "burner1", "profile_known": True}
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_fp = MagicMock()

        with patch("control.routes.burner_routes.burner_engage.list_burner_channels",
                   return_value=[burner]):
            with patch("control.routes.burner_routes.burner_engage.is_running",
                       return_value=False):
                with patch("control.routes.burner_routes.subprocess.Popen",
                           return_value=mock_proc):
                    with patch.object(Path, "mkdir"):
                        with patch.object(Path, "open", return_value=mock_fp):
                            mock_fp.__enter__ = MagicMock(return_value=mock_fp)
                            mock_fp.__exit__ = MagicMock(return_value=False)
                            async with httpx.AsyncClient(transport=transport,
                                                         base_url="http://test") as client:
                                r = await client.post("/api/burner_channels/burner1/engage")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["started"])
        self.assertEqual(data["pid"], 12345)


class TestPollEngage(unittest.IsolatedAsyncioTestCase):
    async def test_no_state_404(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.burner_routes.burner_engage.read_state",
                   return_value=None):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/burner_channels/burner1/engage")
        self.assertEqual(r.status_code, 404)

    async def test_with_state(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        state = {"phase": "liking", "last_action_at": "2026-01-01", "last_action_msg": "ok"}
        with patch("control.routes.burner_routes.burner_engage.read_state",
                   return_value=state):
            with patch("control.routes.burner_routes.burner_engage.is_running",
                       return_value=True):
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://test") as client:
                    r = await client.get("/api/burner_channels/burner1/engage")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["phase"], "liking")
        self.assertTrue(data["running"])


class TestStopEngage(unittest.IsolatedAsyncioTestCase):
    async def test_stop_engage(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.burner_routes.burner_engage.request_stop") as mock_stop:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/burner_channels/burner1/engage/stop")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["stop_requested"])
        self.assertEqual(data["slug"], "burner1")
        mock_stop.assert_called_once_with("burner1")


if __name__ == "__main__":
    unittest.main()
