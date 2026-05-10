"""Tests for operator quality-of-life: cancel, health, owner-IP bypass."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ["YTFACTORY_AGENT_TOKEN"] = "test-token"
os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"

import httpx  # noqa: E402

from control.core import jobs as jobs_mod, rate_limit  # noqa: E402
from control.core.queue import get_queue, reset_queue  # noqa: E402
from control.routes.render_routes import router as render_router  # noqa: E402
from control.core.schema import TaskEnvelope, TaskKind, TaskStatus  # noqa: E402


def _make_app():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(render_router)
    return app


class CancelJobTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_queue()
        jobs_mod.reset_jobs()
        rate_limit.reset_backend()

    async def test_cancel_unknown_job_404(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/jobs/nope/cancel")
        self.assertEqual(r.status_code, 404)

    async def test_cancel_drains_queued_tasks_and_marks_job(self) -> None:
        # Set up: one job, two queued tasks for it, one task for a different job.
        jobs_mod.create_job("jc", channel="auto", topic="t", proposal={})
        q = get_queue()
        q.enqueue(TaskEnvelope(task_id="t1", job_id="jc", kind=TaskKind.RENDER_SHORT))
        q.enqueue(TaskEnvelope(task_id="t2", job_id="jc", kind=TaskKind.YOUTUBE_UPLOAD))
        q.enqueue(TaskEnvelope(task_id="t3", job_id="other", kind=TaskKind.NOOP))

        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/jobs/jc/cancel")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["cancelled_tasks"], 2)
        self.assertEqual(body["job_status"], "cancelled")

        # Job marked cancelled.
        doc = jobs_mod.get_job("jc")
        assert doc is not None
        self.assertEqual(doc["status"], "cancelled")
        self.assertEqual(doc["stage"], "cancelled")
        self.assertEqual(doc["error"], "cancelled by operator")

        # The two job-jc tasks moved to FAILED; the other task untouched.
        self.assertEqual(q.get("t1").status, TaskStatus.FAILED)  # type: ignore[union-attr]
        self.assertEqual(q.get("t2").status, TaskStatus.FAILED)  # type: ignore[union-attr]
        self.assertEqual(q.get("t3").status, TaskStatus.QUEUED)  # type: ignore[union-attr]

    async def test_cancel_leaves_already_done_tasks_alone(self) -> None:
        jobs_mod.create_job("jd", channel="auto", topic="t", proposal={})
        q = get_queue()
        q.enqueue(TaskEnvelope(
            task_id="td", job_id="jd", kind=TaskKind.RENDER_SHORT, status=TaskStatus.DONE,
        ))
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/jobs/jd/cancel")
        self.assertEqual(r.status_code, 200)
        # Done task stays done.
        self.assertEqual(q.get("td").status, TaskStatus.DONE)  # type: ignore[union-attr]


class HealthEndpointTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        rate_limit.reset_backend()
        from control.routes.agent_routes import _LAST_SEEN
        _LAST_SEEN.clear()

    async def test_health_reports_no_agents_initially(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["agents"], [])
        self.assertEqual(body["azure_spend_usd_today"], 0)
        self.assertGreater(body["azure_spend_cap_usd"], 0)

    async def test_health_surfaces_agent_after_heartbeat(self) -> None:
        # Inject an agent presence directly (bypass the heartbeat route).
        from control.routes.agent_routes import _LAST_SEEN
        from control.core.schema import AgentResources
        import time

        _LAST_SEEN["mac-1"] = (time.time(), AgentResources(
            agent_id="mac-1", mlx_free_pct=42.0, kokoro_warm=True, on_battery=False,
        ))

        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/health")
        body = r.json()
        self.assertEqual(len(body["agents"]), 1)
        a = body["agents"][0]
        self.assertEqual(a["agent_id"], "mac-1")
        self.assertAlmostEqual(a["mlx_free_pct"], 42.0)
        self.assertTrue(a["kokoro_warm"])
        self.assertGreaterEqual(a["seconds_ago"], 0)

    async def test_health_surfaces_spend_progress(self) -> None:
        rate_limit.reset_backend()
        # Burn ~half the cap with synthetic token usage.
        cap = rate_limit.daily_cap_usd()
        cost_per_1k = float(os.environ.get("YTFACTORY_AZURE_COST_PER_1K_TOK_USD", "0.005"))
        target_tokens = int(0.5 * cap / cost_per_1k * 1000)
        rate_limit.record_token_usage(target_tokens, 0)

        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/health")
        body = r.json()
        self.assertGreater(body["azure_spend_usd_today"], 0)
        self.assertGreater(body["azure_spend_pct"], 40)
        self.assertLess(body["azure_spend_pct"], 60)


class OwnerIpBypassTest(unittest.TestCase):
    def setUp(self) -> None:
        rate_limit.reset_backend()

    def _req(self, ip: str):
        req = MagicMock()
        req.headers = {}
        req.client = MagicMock()
        req.client.host = ip
        return req

    def test_owner_ip_bypasses_quota(self):
        # Patch the module-level allowlist.
        with patch.object(rate_limit, "_OWNER_IPS", frozenset({"203.0.113.1"})):
            req = self._req("203.0.113.1")
            # Burn way past the chat quota — should never raise.
            for _ in range(rate_limit.quota_for("chat") + 50):
                rate_limit.check_and_increment(req, "chat")

    def test_non_owner_still_rate_limited(self):
        with patch.object(rate_limit, "_OWNER_IPS", frozenset({"203.0.113.1"})):
            from fastapi import HTTPException
            req = self._req("198.51.100.99")  # not in allowlist
            for _ in range(rate_limit.quota_for("chat")):
                rate_limit.check_and_increment(req, "chat")
            with self.assertRaises(HTTPException) as cm:
                rate_limit.check_and_increment(req, "chat")
            self.assertEqual(cm.exception.status_code, 429)

    def test_is_owner_ip_helper(self):
        with patch.object(rate_limit, "_OWNER_IPS", frozenset({"1.2.3.4"})):
            self.assertTrue(rate_limit.is_owner_ip("1.2.3.4"))
            self.assertFalse(rate_limit.is_owner_ip("1.2.3.5"))


if __name__ == "__main__":
    unittest.main()
