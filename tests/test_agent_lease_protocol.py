"""End-to-end test of the agent lease protocol.

Brings up the in-memory queue, a FastAPI app with the agent routes, and an
asyncio agent. Enqueues a noop task and verifies it round-trips through:
    enqueue → lease → run (noop_worker) → ack → status=DONE
"""
from __future__ import annotations

import asyncio
import os
import unittest

# Set token + memory backend BEFORE importing control.* so the queue factory picks them up.
os.environ["YTFACTORY_AGENT_TOKEN"] = "test-token-do-not-use-in-prod"
os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"

import httpx  # noqa: E402

from control.agent_routes import router as agent_router  # noqa: E402
from control.queue import get_queue, new_task_id  # noqa: E402
from agent import runner  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.main import _lease_one  # noqa: E402
from shared.schema import TaskEnvelope, TaskKind, TaskStatus  # noqa: E402


def _make_app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(agent_router)
    return app


class AgentLeaseProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_noop_round_trip(self) -> None:
        # Reuse the singleton in-memory queue across server + test.
        q = get_queue()

        # Sanity: noop_worker is registered.
        self.assertIn(TaskKind.NOOP, runner._REGISTRY)

        # Enqueue a noop task.
        task = TaskEnvelope(task_id=new_task_id(), job_id="job-1", kind=TaskKind.NOOP, payload={"hello": "world"})
        q.enqueue(task)

        cfg = AgentConfig(
            agent_id="test-agent",
            control_url="http://test",
            auth_token="test-token-do-not-use-in-prod",
            heartbeat_interval_s=15.0,
            lease_caps=("noop",),
            lease_ttl_s=60,
        )

        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ran = await _lease_one(cfg, client)

        self.assertTrue(ran, "agent should have processed exactly one task")

        final = q.get(task.task_id)
        self.assertIsNotNone(final)
        assert final is not None  # mypy
        self.assertEqual(final.status, TaskStatus.DONE)
        self.assertEqual(final.output_uri, f"noop://done/{task.task_id}")
        self.assertEqual(final.attempts, 1)

    async def test_unauth_lease_rejected(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/agent/lease",
                json={"agent_id": "x", "caps": ["noop"], "lease_ttl_s": 60},
            )
        self.assertEqual(r.status_code, 401)

    async def test_heartbeat_records_resources(self) -> None:
        from control.agent_routes import get_last_seen, _LAST_SEEN
        from shared.schema import AgentResources, HeartbeatRequest

        _LAST_SEEN.clear()
        body = HeartbeatRequest(
            resources=AgentResources(agent_id="hb-test", mlx_free_pct=42.0)
        ).model_dump(mode="json")

        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/agent/heartbeat",
                json=body,
                headers={"Authorization": "Bearer test-token-do-not-use-in-prod"},
            )
        self.assertEqual(r.status_code, 200)
        seen = get_last_seen()
        self.assertIn("hb-test", seen)
        self.assertAlmostEqual(seen["hb-test"][1].mlx_free_pct, 42.0)


if __name__ == "__main__":
    unittest.main()
