"""Extra tests for control/render_routes.py — covers cancel_job + health.

test_render_routes.py already covers POST /api/render and GET /api/jobs/{id}.
This file pushes the module to 100% by exercising:
  - POST /api/jobs/{job_id}/cancel  (404, InMemoryQueue branch, FirestoreQueue branch)
  - GET  /api/health               (with agents, zero-cap division guard)
"""
from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import MagicMock, patch

import httpx

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

from tests._helpers import PROJECT_ROOT  # noqa: F401

from control import jobs as jobs_mod, rate_limit  # noqa: E402
from control.queue import reset_queue  # noqa: E402
from control.render_routes import router as render_router  # noqa: E402
from control.schema import TaskStatus  # noqa: E402


def _make_app():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(render_router)
    return app


class CancelJobTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_queue()
        jobs_mod.reset_jobs()
        rate_limit.reset_backend()

    async def test_cancel_job_not_found_404(self):
        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post("/api/jobs/notexist/cancel")
        self.assertEqual(r.status_code, 404)

    async def test_cancel_job_in_memory_queue(self):
        """InMemoryQueue branch: queued tasks for this job get cancelled."""
        from control.queue import get_queue, InMemoryQueue
        from control.schema import TaskKind, TaskEnvelope

        job_id = "test-cancel-job-01"
        jobs_mod.get_jobs().create(
            job_id,
            channel="testchan", topic="t", proposal={}, status="pending", stage="queued",
        )
        q = get_queue()
        self.assertIsInstance(q, InMemoryQueue)
        task = TaskEnvelope(
            task_id=uuid.uuid4().hex,
            job_id=job_id,
            kind=TaskKind.RENDER_SHORT,
            payload={"job_id": job_id},
        )
        q.enqueue(task)
        task_id = task.task_id
        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["job_id"], job_id)
        self.assertGreaterEqual(body["cancelled_tasks"], 1)
        t = q.get(task_id)
        self.assertEqual(t.status, TaskStatus.FAILED)

    async def test_cancel_job_firestore_queue_branch(self):
        """FirestoreQueue isinstance branch: exercises the Firestore cancel path."""
        from control.queue import FirestoreQueue

        job_id = "fs-job-cancel-01"
        jobs_mod.get_jobs().create(
            job_id,
            channel="testchan", topic="t", proposal={}, status="running", stage="render",
        )
        mock_fs_queue = MagicMock(spec=FirestoreQueue)
        mock_snap = MagicMock()
        mock_snap.reference = MagicMock()
        mock_db_instance = MagicMock()
        mock_db_instance.project = "test-project"
        mock_fs_queue._db = mock_db_instance

        mock_collection = MagicMock()
        mock_db_instance.collection.return_value = mock_collection
        mock_where1 = MagicMock()
        mock_collection.where.return_value = mock_where1
        mock_where2 = MagicMock()
        mock_where1.where.return_value = mock_where2
        mock_where2.stream.return_value = [mock_snap]

        with patch("control.render_routes.get_queue", return_value=mock_fs_queue), \
             patch("google.cloud.firestore.Client", return_value=mock_db_instance):
            app = _make_app()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["job_id"], job_id)
        mock_snap.reference.update.assert_called_once()

    async def test_cancel_job_firestore_exception_swallowed(self):
        """Firestore exception in cancel is swallowed silently (lines 145-146)."""
        from control.queue import FirestoreQueue

        job_id = "fs-cancel-err-01"
        jobs_mod.get_jobs().create(
            job_id,
            channel="testchan", topic="t", proposal={}, status="running", stage="render",
        )
        mock_fs_queue = MagicMock(spec=FirestoreQueue)
        mock_db_instance = MagicMock()
        mock_db_instance.project = "test-project"
        mock_fs_queue._db = mock_db_instance

        with patch("control.render_routes.get_queue", return_value=mock_fs_queue), \
             patch("google.cloud.firestore.Client", side_effect=RuntimeError("no auth")):
            app = _make_app()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["job_id"], job_id)
        self.assertEqual(body["cancelled_tasks"], 0)


class HealthEndpointTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_queue()
        jobs_mod.reset_jobs()
        rate_limit.reset_backend()

    async def test_health_with_agents(self):
        mock_snap = MagicMock()
        mock_snap.mlx_free_pct = 80.0
        mock_snap.kokoro_warm = True
        mock_snap.mflux_warm = False
        mock_snap.on_battery = False
        with patch("control.agent_routes.get_last_seen", return_value={
            "agent-1": (1000.0, mock_snap),
        }), patch("control.render_routes.rate_limit") as mock_rl:
            mock_rl.daily_spend_usd.return_value = 0.05
            mock_rl.daily_cap_usd.return_value = 5.0
            app = _make_app()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.get("/api/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["agents"]), 1)
        self.assertAlmostEqual(body["azure_spend_pct"], 1.0)

    async def test_health_zero_cap_no_division_error(self):
        """daily_cap_usd=0 -> azure_spend_pct=None."""
        with patch("control.agent_routes.get_last_seen", return_value={}), \
             patch("control.render_routes.rate_limit") as mock_rl:
            mock_rl.daily_spend_usd.return_value = 0.0
            mock_rl.daily_cap_usd.return_value = 0.0
            app = _make_app()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["azure_spend_pct"])


if __name__ == "__main__":
    unittest.main()
