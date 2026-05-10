"""Tests for control/render_routes.py — POST /api/render + GET /api/jobs/{id}."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ["YTFACTORY_AGENT_TOKEN"] = "test-token"
os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"

import httpx  # noqa: E402

from control import jobs as jobs_mod, rate_limit  # noqa: E402
from control.queue import get_queue, reset_queue  # noqa: E402
from control.render_routes import router as render_router  # noqa: E402
from control.schema import TaskKind, TaskStatus  # noqa: E402


def _make_app():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(render_router)
    return app


class PostRenderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_queue()
        jobs_mod.reset_jobs()
        rate_limit.reset_backend()

    async def test_post_render_enqueues_render_short_task_and_creates_job(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/render", json={
                "channel": "sportstoriesanimated",
                "topic": "Aguero 93:20",
                "notes": "Tifo line-art",
                "length_s": 55,
                "source_kind": "auto",
            })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["job_id"])
        self.assertTrue(body["task_id"])
        # Job doc created.
        job = jobs_mod.get_job(body["job_id"])
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["channel"], "sportstoriesanimated")
        self.assertEqual(job["topic"], "Aguero 93:20")
        # Queue has the right kind, payload, and status.
        task = get_queue().get(body["task_id"])
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.kind, TaskKind.RENDER_SHORT)
        self.assertEqual(task.status, TaskStatus.QUEUED)
        self.assertEqual(task.job_id, body["job_id"])
        self.assertEqual(task.payload["topic"], "Aguero 93:20")
        self.assertEqual(task.payload["length_s"], 55)

    async def test_post_render_422_on_empty_topic(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/render", json={
                "channel": "auto", "topic": "", "length_s": 55,
            })
        self.assertEqual(r.status_code, 422)

    async def test_post_render_422_on_whitespace_only_topic(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/render", json={
                "channel": "auto", "topic": "   \n\t ", "length_s": 55,
            })
        self.assertEqual(r.status_code, 422)

    async def test_post_render_clamps_short_to_20_120_and_long_to_7200(self) -> None:
        # Two posts → reset the daily quota between them so we measure
        # clamping, not rate-limit behaviour.
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.post("/api/render", json={"channel": "auto", "topic": "x", "length_s": 5})
            rate_limit.reset_backend()
            # > 120 is treated as long-form and clamped to ≤ 7200s (2hr)
            r2 = await client.post("/api/render", json={"channel": "auto", "topic": "x", "length_s": 9999})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        q = get_queue()
        from control.queue import InMemoryQueue
        assert isinstance(q, InMemoryQueue)
        lengths = sorted(t.payload["length_s"] for t in q._tasks.values())  # type: ignore[attr-defined]
        self.assertEqual(lengths, [20, 7200])

    async def test_post_render_long_form_value_passes_through(self) -> None:
        # 30 / 60 / 120-min selections from the create UI must propagate
        # to the queue payload unchanged (within the 121–7200s band).
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for secs in (1800, 3600, 7200):
                rate_limit.reset_backend()
                r = await client.post(
                    "/api/render",
                    json={"channel": "auto", "topic": "x", "length_s": secs},
                )
                self.assertEqual(r.status_code, 200)
        q = get_queue()
        from control.queue import InMemoryQueue
        assert isinstance(q, InMemoryQueue)
        lengths = sorted(t.payload["length_s"] for t in q._tasks.values())  # type: ignore[attr-defined]
        self.assertEqual(lengths, [1800, 3600, 7200])

    async def test_post_render_429_after_quota(self) -> None:
        # Force the confirm quota to 1 for this test — production default
        # bumped to 20 so the operator can iterate, but we still want to
        # cover the 429 path.
        rate_limit.reset_backend()
        with patch.dict(rate_limit._DEFAULTS, {"confirm": 1}):
            app = _make_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r1 = await client.post("/api/render", json={"channel": "auto", "topic": "x", "length_s": 55})
                r2 = await client.post("/api/render", json={"channel": "auto", "topic": "x", "length_s": 55})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 429)

    async def test_validation_runs_before_rate_limit(self) -> None:
        # Empty topic should NOT consume the per-IP quota.
        rate_limit.reset_backend()
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.post("/api/render", json={"channel": "auto", "topic": "", "length_s": 55})
            r2 = await client.post("/api/render", json={"channel": "auto", "topic": "real", "length_s": 55})
        self.assertEqual(r1.status_code, 422)
        self.assertEqual(r2.status_code, 200)


class GetJobTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_queue()
        jobs_mod.reset_jobs()
        rate_limit.reset_backend()

    async def test_404_for_unknown_job(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/jobs/not-a-real-id")
        self.assertEqual(r.status_code, 404)

    async def test_get_job_returns_status_fields(self) -> None:
        jobs_mod.create_job("jx", channel="mystoriesanimated",
                            topic="AITA cake", proposal={"length_s": 55})
        jobs_mod.mark_stage("jx", status=jobs_mod.STATUS_RENDERING, stage="render")
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/jobs/jx")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["job_id"], "jx")
        self.assertEqual(body["channel"], "mystoriesanimated")
        self.assertEqual(body["topic"], "AITA cake")
        self.assertEqual(body["status"], "rendering")
        self.assertEqual(body["stage"], "render")
        # No mp4 yet.
        self.assertIsNone(body["short_uri"])
        self.assertIsNone(body["short_signed_url"])

    async def test_done_job_includes_signed_url(self) -> None:
        jobs_mod.create_job("jdone", channel="auto", topic="t", proposal={})
        jobs_mod.mark_done("jdone", short_uri="gs://ytfactory-prod-artifacts/jobs/jdone/short.mp4",
                           youtube_url="https://youtu.be/abc")
        from control import storage
        with patch.object(storage, "signed_url", return_value="https://signed.example/x") as mock_su:
            app = _make_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/jobs/jdone")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["short_uri"], "gs://ytfactory-prod-artifacts/jobs/jdone/short.mp4")
        self.assertEqual(body["short_signed_url"], "https://signed.example/x")
        self.assertEqual(body["youtube_url"], "https://youtu.be/abc")
        mock_su.assert_called_once()
        # ttl ~10 min default.
        kwargs = mock_su.call_args.kwargs
        self.assertEqual(kwargs.get("method"), "GET")

    async def test_done_with_signed_url_failure_does_not_500(self) -> None:
        jobs_mod.create_job("jbad", channel="auto", topic="t", proposal={})
        jobs_mod.mark_done("jbad", short_uri="gs://b/short.mp4")
        from control import storage
        with patch.object(storage, "signed_url", side_effect=RuntimeError("no creds")):
            app = _make_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/jobs/jbad")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNone(body["short_signed_url"])  # gracefully degraded
        self.assertEqual(body["short_uri"], "gs://b/short.mp4")  # raw URI still surfaced

    async def test_failed_job_surfaces_error(self) -> None:
        jobs_mod.create_job("jfail", channel="auto", topic="t", proposal={})
        jobs_mod.mark_failed("jfail", stage="render", error="boom")
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/jobs/jfail")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["stage"], "render")
        self.assertEqual(body["error"], "boom")


if __name__ == "__main__":
    unittest.main()
