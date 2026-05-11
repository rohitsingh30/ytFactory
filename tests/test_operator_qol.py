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


class QueueEndpointTest(unittest.IsolatedAsyncioTestCase):
    """Regression tests for /api/queue.

    Born from the 2026-05-11 outage where the Studio Queue page showed
    "Empty" in the Completed column for two days while Firestore actually
    held 18+ terminal jobs. Root cause was a missing composite index
    (``jobs(status ASC, updated_at DESC)``) on the
    ``where(status in [...]).order_by(updated_at)`` query — combined with
    a single try/except around BOTH the active-queue and terminal-queue
    Firestore calls that silently swallowed the index error and blanked
    Completed without telling the operator.

    These tests pin the post-fix invariants:
      1. In-memory backend: queued/running/completed buckets are
         classified by status correctly + sorted newest-first.
      2. Each Firestore query has its OWN try/except so a failure on the
         terminal query cannot blank queued/running.
      3. Failures are SURFACED in ``warnings[section]`` — we never again
         silently lie about an empty queue."""

    def setUp(self) -> None:
        reset_queue()
        jobs_mod.reset_jobs()
        rate_limit.reset_backend()

    async def _get_queue(self) -> dict:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/queue")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def test_in_memory_buckets_jobs_by_status(self) -> None:
        # Three jobs across the three live statuses + three terminal.
        jobs_mod.create_job("p1", channel="auto", topic="pending one", proposal={})
        jobs_mod.create_job("r1", channel="auto", topic="running one", proposal={})
        jobs_mod.get_jobs().update("r1", status="rendering", stage="images")
        jobs_mod.create_job("u1", channel="auto", topic="uploading one", proposal={})
        jobs_mod.get_jobs().update("u1", status="uploading", stage="gcs_upload")
        jobs_mod.create_job("d1", channel="auto", topic="done one", proposal={})
        jobs_mod.get_jobs().update("d1", status="done", stage="done")
        jobs_mod.create_job("f1", channel="auto", topic="failed one", proposal={})
        jobs_mod.get_jobs().update("f1", status="failed", stage="dispatching", error="boom")
        jobs_mod.create_job("c1", channel="auto", topic="cancelled one", proposal={})
        jobs_mod.get_jobs().update("c1", status="cancelled", stage="cancelled")

        body = await self._get_queue()

        self.assertEqual([j["topic"] for j in body["queued"]], ["pending one"])
        self.assertEqual(
            sorted(j["topic"] for j in body["running"]),
            ["running one", "uploading one"],
        )
        # Completed includes done + failed + cancelled (newest-first, but
        # all three were created in this test so the relative order will
        # follow updated_at — just assert the SET membership which is the
        # invariant the user cares about: failed/cancelled MUST appear).
        completed_topics = {j["topic"] for j in body["completed"]}
        self.assertEqual(
            completed_topics,
            {"done one", "failed one", "cancelled one"},
        )
        # No Firestore in this test → warnings should be empty.
        self.assertEqual(body.get("warnings", {}), {})

    async def test_firestore_terminal_failure_does_not_blank_active_queue(self) -> None:
        """The original bug: missing composite index → terminal query 400s
        → ALL columns blanked because of one shared try/except.

        After the fix: active query still returns its results; the
        terminal query's failure is surfaced in warnings.completed."""
        # Force the Firestore branch by swapping the backend to a MagicMock
        # that is NOT an instance of _MemoryJobs.
        from google.api_core.exceptions import FailedPrecondition
        from control.routes import render_routes as rr

        # Build a fake non-memory backend so the route takes the Firestore branch.
        fake_backend = MagicMock()
        # Two distinct query builders so we can simulate "active OK,
        # terminal raises" — the original bug shape.
        active_q = MagicMock()
        active_snap = MagicMock()
        active_snap.id = "abc123"
        active_snap.to_dict.return_value = {
            "channel": "mystoriesanimated", "topic": "live render",
            "status": "pending", "stage": "queued",
        }
        active_q.where.return_value.limit.return_value.stream.return_value = iter([active_snap])

        terminal_q = MagicMock()
        terminal_q.where.return_value.order_by.return_value.limit.return_value.stream.side_effect = (
            FailedPrecondition("400 The query requires an index. ...")
        )

        fake_db = MagicMock()
        # Two consecutive db.collection("jobs") calls — first returns
        # the active-query builder, second returns the terminal one.
        fake_db.collection.side_effect = [active_q, terminal_q]

        fake_firestore_module = MagicMock()
        fake_firestore_module.Client.return_value = fake_db
        fake_firestore_module.Query.DESCENDING = "DESCENDING"

        # _MemoryJobs check uses isinstance — ensure our mock isn't one.
        with patch.object(rr.jobs_mod, "get_jobs", return_value=fake_backend), \
             patch.dict("sys.modules", {"google.cloud.firestore": fake_firestore_module}):
            body = await self._get_queue()

        # Active query worked → queued column shows the real pending job.
        self.assertEqual(len(body["queued"]), 1)
        self.assertEqual(body["queued"][0]["topic"], "live render")
        # Terminal query failed → completed is empty BUT warnings
        # explains why so the UI can render an actionable banner.
        self.assertEqual(body["completed"], [])
        warnings = body.get("warnings", {})
        self.assertIn("completed", warnings)
        self.assertIn("composite index", warnings["completed"].lower())
        # Active sections should NOT carry the terminal error (the
        # original bug was that they did, via a shared try/except).
        self.assertNotIn("queued", warnings)
        self.assertNotIn("running", warnings)


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
