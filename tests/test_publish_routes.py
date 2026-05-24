"""Tests for the one-click-publish control-plane surface.

Pins:
  - POST /api/jobs/{id}/publish requires the PIN session (when PIN auth
    is enabled in the env).
  - POST /api/jobs/{id}/publish validates ``visibility`` against the
    public/unlisted/private allowlist.
  - POST /api/jobs/{id}/publish persists auto-generated metadata to the
    job doc as ``publish_metadata``.
  - POST /api/jobs/{id}/publish calls the YouTube upload handler with
    the generated metadata (we patch
    ``pipeline.upload.upload.youtube_upload`` and assert the call shape).
  - GET /api/jobs/{id}/publish/preview returns the generated metadata
    without performing any upload.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

# Configure the env BEFORE importing the route module — same shape as
# the existing test_render_routes.py / test_routes_render_routes.py.
os.environ["YTFACTORY_AGENT_TOKEN"] = "test-token"
os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"
# Disable the PIN gate by default for these tests (auth_enabled() reads
# YTFACTORY_OPERATOR_PIN; absence → no-op). The auth-required test below
# sets it explicitly and clears it again.
os.environ.pop("YTFACTORY_OPERATOR_PIN", None)
# Force sim mode so the success path doesn't actually try to hit YouTube.
os.environ["YTFACTORY_SIM_WORKER"] = "1"

import httpx  # noqa: E402

from control.core import jobs as jobs_mod  # noqa: E402
from control.core import rate_limit  # noqa: E402
from control.core.queue import reset_queue  # noqa: E402
from control.routes.render_routes import router as render_router  # noqa: E402


def _make_app():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(render_router)
    return app


def _done_job(job_id: str = "j-pub", *, with_script: bool = True) -> None:
    """Seed a STATUS_DONE job with enough script payload to generate
    publish metadata without raising MissingMetadataInputError."""
    proposal = {"topic": "A really compelling test topic"}
    if with_script:
        proposal["title_options"] = ["The Right Title For This Test"]
        proposal["hook"] = "A hook line that gives the generator something to chew on"
        proposal["summary"] = "A summary describing the rendered short."
    jobs_mod.create_job(
        job_id, channel="mystoriesanimated", topic=proposal["topic"],
        proposal=proposal,
    )
    jobs_mod.get_jobs().update(
        job_id, status=jobs_mod.STATUS_DONE, stage="done",
        short_uri="sim://placeholder.mp4",
    )


class PublishPreviewTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_queue()
        jobs_mod.reset_jobs()
        rate_limit.reset_backend()

    async def test_preview_returns_full_metadata_shape(self) -> None:
        _done_job("j-preview")
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/jobs/j-preview/publish/preview")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # Every PublishMetadata field must be on the wire.
        for field in (
            "title",
            "description",
            "hashtags",
            "tags",
            "category_id",
            "default_language",
            "made_for_kids",
        ):
            self.assertIn(field, body, f"missing field {field!r}")
        self.assertTrue(body["title"])
        self.assertTrue(body["description"])
        self.assertTrue(body["hashtags"], "hashtags must never be empty")

    async def test_preview_404_when_job_missing(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/jobs/nope/publish/preview")
        self.assertEqual(r.status_code, 404)

    async def test_preview_422_when_no_title_inputs(self) -> None:
        # Create a job with no title source AND no topic — the metadata
        # generator must raise, and the route must surface 422.
        jobs_mod.create_job(
            "j-empty", channel="auto", topic="",
            proposal={"topic": ""},
        )
        jobs_mod.get_jobs().update(
            "j-empty", status=jobs_mod.STATUS_DONE, stage="done",
        )
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/jobs/j-empty/publish/preview")
        self.assertEqual(r.status_code, 422, r.text)


class PublishPostTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_queue()
        jobs_mod.reset_jobs()
        rate_limit.reset_backend()

    async def test_post_publish_requires_visibility_in_allowlist(self) -> None:
        _done_job("j-vis")
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/jobs/j-vis/publish", json={"visibility": "world"},
            )
        self.assertEqual(r.status_code, 422, r.text)

    async def test_post_publish_409_when_job_not_done(self) -> None:
        jobs_mod.create_job(
            "j-pending", channel="auto", topic="t",
            proposal={"topic": "t", "title_options": ["X"]},
        )
        # status stays at STATUS_PENDING
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/jobs/j-pending/publish", json={"visibility": "unlisted"},
            )
        self.assertEqual(r.status_code, 409, r.text)

    async def test_post_publish_persists_metadata_to_job_doc(self) -> None:
        _done_job("j-persist")
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/jobs/j-persist/publish",
                json={"visibility": "unlisted"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # Response includes the generated metadata.
        self.assertIsNotNone(body.get("publish_metadata"))
        self.assertTrue(body["publish_metadata"]["title"])
        # Persisted to the Firestore doc.
        doc = jobs_mod.get_job("j-persist")
        self.assertIsNotNone(doc)
        assert doc is not None
        self.assertIn("publish_metadata", doc)
        pm = doc["publish_metadata"]
        self.assertEqual(pm["visibility"], "unlisted")
        self.assertTrue(pm["title"])
        self.assertTrue(pm["hashtags"])

    async def test_post_publish_404_when_job_missing(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/jobs/nope/publish", json={"visibility": "unlisted"},
            )
        self.assertEqual(r.status_code, 404)

    async def test_post_publish_sets_youtube_url_on_sim_mode(self) -> None:
        _done_job("j-sim")
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/jobs/j-sim/publish",
                json={"visibility": "public", "scheduled_publish_at": None},
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["youtube_url"])
        self.assertTrue(body["youtube_url"].startswith("https://youtu.be/sim-"))
        doc = jobs_mod.get_job("j-sim")
        assert doc is not None
        self.assertEqual(doc["youtube_url"], body["youtube_url"])

    async def test_post_publish_calls_youtube_upload_with_generated_metadata(
        self,
    ) -> None:
        """When sim mode is off and the local mp4 exists, the route must
        delegate to ``pipeline.upload.upload.youtube_upload`` and forward
        the generator's output — title, description, tags, category,
        privacy. This pins the integration boundary against the existing
        upload handler so future refactors can't quietly drop fields.
        """
        import tempfile

        _done_job("j-real")
        # Create a real local mp4 so the route doesn't early-return 501.
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"\x00" * 64)
            local_mp4 = f.name
        try:
            jobs_mod.get_jobs().update("j-real", preview_local_path=local_mp4)

            fake_result = {
                "video_id": "abc123",
                "url": "https://youtu.be/abc123",
                "uploaded_at": "2026-05-24T00:00:00Z",
            }
            # Temporarily disable sim so the real path runs, and patch
            # the upload handler.
            with patch.dict(os.environ, {"YTFACTORY_SIM_WORKER": "0"}):
                with patch(
                    "pipeline.upload.upload.youtube_upload",
                    return_value=fake_result,
                ) as mock_upload:
                    app = _make_app()
                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                        r = await c.post(
                            "/api/jobs/j-real/publish",
                            json={"visibility": "unlisted"},
                        )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(mock_upload.called)
            # Inspect the call kwargs — every generator field must
            # reach the upload handler.
            _args, kwargs = mock_upload.call_args
            self.assertTrue(kwargs.get("title"))
            self.assertTrue(kwargs.get("description"))
            self.assertEqual(kwargs.get("privacy"), "unlisted")
            self.assertIsInstance(kwargs.get("tags"), list)
            self.assertTrue(kwargs.get("category_id"))
            # Response surfaces the youtube_url from the upload handler.
            self.assertEqual(
                r.json().get("youtube_url"), "https://youtu.be/abc123",
            )
        finally:
            os.unlink(local_mp4)


class PublishAuthTest(unittest.IsolatedAsyncioTestCase):
    """When YTFACTORY_OPERATOR_PIN is set, /publish + /publish/preview
    must reject requests without the session cookie."""

    def setUp(self) -> None:
        reset_queue()
        jobs_mod.reset_jobs()
        rate_limit.reset_backend()

    async def test_post_publish_refused_without_session_when_pin_enabled(
        self,
    ) -> None:
        _done_job("j-auth")
        # Set the PIN so auth_enabled() flips on.
        with patch.dict(os.environ, {"YTFACTORY_OPERATOR_PIN": "0000"}):
            app = _make_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                r = await c.post(
                    "/api/jobs/j-auth/publish",
                    json={"visibility": "unlisted"},
                )
        self.assertEqual(r.status_code, 401, r.text)


if __name__ == "__main__":
    unittest.main()
