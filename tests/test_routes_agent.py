"""Tests for control/agent_routes.py — 100% line coverage."""
from __future__ import annotations

import os
import time
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-agent-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

import httpx

from control.routes.agent_routes import router, _q, get_last_seen, _LAST_SEEN
from control.core.schema import (
    AgentResources,
    HeartbeatRequest,
    LeaseRequest,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
)
from fastapi import FastAPI

_TOKEN = "test-agent-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_ENV = {"YTFACTORY_AGENT_TOKEN": _TOKEN}


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _make_task() -> TaskEnvelope:
    return TaskEnvelope(
        task_id=str(uuid.uuid4()),
        job_id="job-1",
        kind=TaskKind.RENDER_SHORT,
        status=TaskStatus.LEASED,
    )


class TestQHelper(unittest.TestCase):
    def test_q_returns_queue(self) -> None:
        q = _q()
        self.assertIsNotNone(q)


class TestGetLastSeen(unittest.TestCase):
    def test_returns_dict(self) -> None:
        result = get_last_seen()
        self.assertIsInstance(result, dict)


class TestHeartbeat(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_ok(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch.dict(os.environ, _ENV):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post(
                    "/agent/heartbeat",
                    json={
                        "resources": {
                            "agent_id": "laptop-1",
                            "mlx_free_pct": 80.0,
                            "gpu_mem_pct": 20.0,
                        }
                    },
                    headers=_AUTH,
                )
        self.assertEqual(r.status_code, 200)
        self.assertIn("laptop-1", _LAST_SEEN)


class TestLease(unittest.IsolatedAsyncioTestCase):
    async def test_lease_immediate_task(self) -> None:
        """Returns task immediately when queue has one."""
        task = _make_task()
        mock_queue = MagicMock()
        mock_queue.lease.return_value = task
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.agent_routes.get_queue", return_value=mock_queue):
            with patch.dict(os.environ, _ENV):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.post(
                        "/agent/lease",
                        json={"agent_id": "laptop-1", "caps": ["render_short"]},
                        headers=_AUTH,
                    )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsNotNone(data["task"])
        self.assertEqual(data["wait_s"], 0)

    async def test_lease_deadline_timeout(self) -> None:
        """Returns no task when deadline is immediately hit."""
        mock_queue = MagicMock()
        mock_queue.lease.return_value = None
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        start = time.time()
        call_count = 0

        def fake_time():
            nonlocal call_count
            call_count += 1
            return start if call_count <= 1 else start + 31.0

        with patch("control.routes.agent_routes.get_queue", return_value=mock_queue):
            with patch("control.routes.agent_routes.time.time", side_effect=fake_time):
                with patch.dict(os.environ, _ENV):
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                        r = await client.post(
                            "/agent/lease",
                            json={"agent_id": "laptop-1", "caps": ["render_short"]},
                            headers=_AUTH,
                        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsNone(data["task"])
        self.assertEqual(data["wait_s"], 30)

    async def test_lease_returns_task_after_sleep(self) -> None:
        """Covers asyncio.sleep branch: first poll None, second returns task."""
        mock_queue = MagicMock()
        task = _make_task()
        mock_queue.lease.side_effect = [None, task]
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        start = time.time()
        call_count = [0]

        def fake_time():
            call_count[0] += 1
            return start  # deadline never reached

        with patch("control.routes.agent_routes.get_queue", return_value=mock_queue):
            with patch("control.routes.agent_routes.time.time", side_effect=fake_time):
                with patch("control.routes.agent_routes.asyncio.sleep", new_callable=AsyncMock):
                    with patch.dict(os.environ, _ENV):
                        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                            r = await client.post(
                                "/agent/lease",
                                json={"agent_id": "laptop-1", "caps": ["render_short"]},
                                headers=_AUTH,
                            )
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.json()["task"])

    async def test_ack_not_found(self) -> None:
        mock_queue = MagicMock()
        mock_queue.get.return_value = None
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.agent_routes.get_queue", return_value=mock_queue):
            with patch.dict(os.environ, _ENV):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.post(
                        "/agent/ack/nonexistent",
                        json={"agent_id": "laptop-1", "status": "ok"},
                        headers=_AUTH,
                    )
        self.assertEqual(r.status_code, 404)

    async def test_ack_success(self) -> None:
        task = _make_task()
        mock_queue = MagicMock()
        mock_queue.get.return_value = task
        mock_queue.ack.return_value = None
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.agent_routes.get_queue", return_value=mock_queue):
            with patch.dict(os.environ, _ENV):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.post(
                        f"/agent/ack/{task.task_id}",
                        json={"agent_id": "laptop-1", "status": "ok", "output_uri": "gs://b/out.mp4"},
                        headers=_AUTH,
                    )
        self.assertEqual(r.status_code, 200)
        mock_queue.ack.assert_called_once()

    async def test_ack_failed(self) -> None:
        task = _make_task()
        mock_queue = MagicMock()
        mock_queue.get.return_value = task
        mock_queue.ack.return_value = None
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.agent_routes.get_queue", return_value=mock_queue):
            with patch.dict(os.environ, _ENV):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.post(
                        f"/agent/ack/{task.task_id}",
                        json={"agent_id": "laptop-1", "status": "error", "error": "OOM"},
                        headers=_AUTH,
                    )
        self.assertEqual(r.status_code, 200)
        _, kwargs = mock_queue.ack.call_args
        self.assertFalse(kwargs.get("ok"))


if __name__ == "__main__":
    unittest.main()
