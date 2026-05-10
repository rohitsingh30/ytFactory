"""Tests for control/scheduler_routes.py — 100% line coverage."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

import httpx

from control.scheduler_routes import router, _require_auth
from fastapi import FastAPI, HTTPException


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


class TestRequireAuth(unittest.TestCase):
    def test_no_token_env_raises_503(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "YTFACTORY_AGENT_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                _require_auth(None)
            self.assertEqual(ctx.exception.status_code, 503)

    def test_missing_authorization_raises_401(self) -> None:
        with patch.dict(os.environ, {"YTFACTORY_AGENT_TOKEN": "secret"}):
            with self.assertRaises(HTTPException) as ctx:
                _require_auth(None)
            self.assertEqual(ctx.exception.status_code, 401)

    def test_bad_auth_prefix_raises_401(self) -> None:
        with patch.dict(os.environ, {"YTFACTORY_AGENT_TOKEN": "secret"}):
            with self.assertRaises(HTTPException) as ctx:
                _require_auth("Basic abc")
            self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_token_raises_403(self) -> None:
        with patch.dict(os.environ, {"YTFACTORY_AGENT_TOKEN": "secret"}):
            with self.assertRaises(HTTPException) as ctx:
                _require_auth("Bearer wrongtoken")
            self.assertEqual(ctx.exception.status_code, 403)

    def test_correct_token_passes(self) -> None:
        with patch.dict(os.environ, {"YTFACTORY_AGENT_TOKEN": "secret"}):
            _require_auth("Bearer secret")  # should not raise


class TestSchedulerRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_tick_returns_dict(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        mock_result = {"action": "enqueued", "count": 1}
        with patch("control.scheduler_routes.scheduler.tick", return_value=mock_result):
            with patch.dict(os.environ, {"YTFACTORY_AGENT_TOKEN": "test-token"}):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.post(
                        "/api/scheduler/tick",
                        headers={"Authorization": "Bearer test-token"},
                    )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), mock_result)

    async def test_state_returns_dict(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        mock_state = {"last_tick": "2026-01-01", "queue_depth": 0}
        with patch("control.scheduler_routes.scheduler._read_state", return_value=mock_state):
            with patch.dict(os.environ, {"YTFACTORY_AGENT_TOKEN": "test-token"}):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.get(
                        "/api/scheduler/state",
                        headers={"Authorization": "Bearer test-token"},
                    )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), mock_state)

    async def test_tick_no_token_env_503(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "YTFACTORY_AGENT_TOKEN"}
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch.dict(os.environ, env, clear=True):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/api/scheduler/tick")
        self.assertEqual(r.status_code, 503)

    async def test_tick_wrong_token_403(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch.dict(os.environ, {"YTFACTORY_AGENT_TOKEN": "secret"}):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post(
                    "/api/scheduler/tick",
                    headers={"Authorization": "Bearer wrongtoken"},
                )
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
