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
from pathlib import Path
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


class PublishCloudMp4Test(unittest.IsolatedAsyncioTestCase):
    """C1 2026-05-24 — cloud-rendered jobs have no preview_local_path;
    their mp4 lives at gs://<bucket>/jobs/<id>/short.mp4. The publish
    route must download the mp4 to a temp file and feed THAT to
    youtube_upload, not raise 501."""

    def setUp(self) -> None:
        reset_queue()
        jobs_mod.reset_jobs()
        rate_limit.reset_backend()

    async def test_cloud_rendered_job_downloads_short_uri_then_uploads(self) -> None:
        import tempfile

        _done_job("j-cloud")
        # NO preview_local_path — just a gs:// short_uri (the cloud
        # worker shape).
        jobs_mod.get_jobs().update(
            "j-cloud",
            short_uri="gs://ytfactory-prod-v3-artifacts/jobs/j-cloud/short.mp4",
        )

        # Fake storage.download writes a real temp mp4 so the upload
        # mock can see it.
        downloaded_paths: list[str] = []

        def fake_download(uri: str, local_path: str) -> Path:
            downloaded_paths.append(uri)
            p = Path(local_path)
            p.write_bytes(b"\x00" * 16)
            return p

        fake_result = {
            "video_id": "cloud123",
            "url": "https://youtu.be/cloud123",
            "uploaded_at": "2026-05-24T00:00:00Z",
        }
        with patch.dict(os.environ, {"YTFACTORY_SIM_WORKER": "0"}):
            with patch("control.core.storage.download", side_effect=fake_download):
                with patch(
                    "pipeline.upload.upload.youtube_upload",
                    return_value=fake_result,
                ) as mock_upload:
                    app = _make_app()
                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                        r = await c.post(
                            "/api/jobs/j-cloud/publish",
                            json={"visibility": "unlisted"},
                        )

        self.assertEqual(r.status_code, 200, r.text)
        # storage.download was called with the canonical gs:// URI.
        self.assertEqual(len(downloaded_paths), 1)
        self.assertTrue(downloaded_paths[0].startswith("gs://"))
        self.assertIn("j-cloud/short.mp4", downloaded_paths[0])
        # youtube_upload was called with the downloaded temp path
        # (Path object, .suffix == ".mp4").
        self.assertTrue(mock_upload.called)
        args, _kwargs = mock_upload.call_args
        self.assertTrue(str(args[0]).endswith(".mp4"))

    async def test_cloud_rendered_job_returns_422_when_no_short_uri_and_no_local(self) -> None:
        _done_job("j-no-mp4")
        # Wipe short_uri so neither path resolves.
        jobs_mod.get_jobs().update("j-no-mp4", short_uri="sim://nope.mp4")
        with patch.dict(os.environ, {"YTFACTORY_SIM_WORKER": "0"}):
            app = _make_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                r = await c.post(
                    "/api/jobs/j-no-mp4/publish",
                    json={"visibility": "unlisted"},
                )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("short_uri", r.text)

    async def test_cloud_rendered_job_returns_502_when_download_fails(self) -> None:
        _done_job("j-dl-fail")
        jobs_mod.get_jobs().update(
            "j-dl-fail",
            short_uri="gs://ytfactory-prod-v3-artifacts/jobs/j-dl-fail/short.mp4",
        )
        with patch.dict(os.environ, {"YTFACTORY_SIM_WORKER": "0"}):
            with patch(
                "control.core.storage.download",
                side_effect=RuntimeError("network borked"),
            ):
                app = _make_app()
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                    r = await c.post(
                        "/api/jobs/j-dl-fail/publish",
                        json={"visibility": "unlisted"},
                    )
        self.assertEqual(r.status_code, 502, r.text)


class PublishQuotaPlaywrightFallbackTest(unittest.IsolatedAsyncioTestCase):
    """C3 2026-05-24 — when youtube_upload raises QuotaExceededError,
    the route attempts the playwright fallback. When playwright isn't
    available either, the route returns ``status="queued_for_manual"``
    (NOT 500) so the UI can surface the skill-run instruction."""

    def setUp(self) -> None:
        reset_queue()
        jobs_mod.reset_jobs()
        rate_limit.reset_backend()

    async def test_quota_exceeded_then_playwright_unavailable_returns_queued(self) -> None:
        import tempfile

        from pipeline.upload.upload import QuotaExceededError

        _done_job("j-quota")
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"\x00" * 16)
            local_mp4 = f.name
        try:
            jobs_mod.get_jobs().update("j-quota", preview_local_path=local_mp4)

            quota_exc = QuotaExceededError("quotaExceeded", "daily limit hit")
            with patch.dict(os.environ, {"YTFACTORY_SIM_WORKER": "0"}):
                with patch(
                    "pipeline.upload.upload.youtube_upload",
                    side_effect=quota_exc,
                ):
                    # Force playwright path unavailable by removing the
                    # env precondition.
                    with patch.dict(os.environ, {"YTFACTORY_CHROME_USER_DATA_DIR": ""}):
                        app = _make_app()
                        transport = httpx.ASGITransport(app=app)
                        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                            r = await c.post(
                                "/api/jobs/j-quota/publish",
                                json={"visibility": "public"},
                            )
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["status"], "queued_for_manual")
            self.assertIn("quota", body.get("error", "").lower())
            # Persisted state reflects the queued status.
            doc = jobs_mod.get_job("j-quota")
            assert doc is not None
            self.assertEqual(
                doc["publish_metadata"]["upload_method"], "queued_for_manual",
            )
            self.assertIn("queued_reason", doc["publish_metadata"])
        finally:
            os.unlink(local_mp4)

    async def test_quota_exceeded_then_playwright_success_returns_done(self) -> None:
        import tempfile

        from pipeline.upload.upload import QuotaExceededError

        _done_job("j-quota-pw")
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"\x00" * 16)
            local_mp4 = f.name
        try:
            jobs_mod.get_jobs().update("j-quota-pw", preview_local_path=local_mp4)

            quota_exc = QuotaExceededError("quotaExceeded", "limit")
            fake_pw_result = {
                "video_id": "pw_abc",
                "url": "https://youtu.be/pw_abc",
                "uploaded_at": "2026-05-24T00:00:00Z",
            }
            with patch.dict(os.environ, {"YTFACTORY_SIM_WORKER": "0"}):
                with patch(
                    "pipeline.upload.upload.youtube_upload",
                    side_effect=quota_exc,
                ):
                    with patch(
                        "pipeline.upload.playwright_upload.playwright_upload",
                        return_value=fake_pw_result,
                    ) as mock_pw:
                        app = _make_app()
                        transport = httpx.ASGITransport(app=app)
                        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                            r = await c.post(
                                "/api/jobs/j-quota-pw/publish",
                                json={"visibility": "unlisted"},
                            )
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["status"], "done")
            self.assertEqual(body["youtube_url"], "https://youtu.be/pw_abc")
            self.assertTrue(mock_pw.called)
            doc = jobs_mod.get_job("j-quota-pw")
            assert doc is not None
            self.assertEqual(doc["publish_metadata"]["upload_method"], "playwright")
        finally:
            os.unlink(local_mp4)

    async def test_non_quota_upload_error_does_not_trigger_playwright(self) -> None:
        """Generic UploadError (e.g. refresh-token-lost, 5xx) must NOT
        fall through to playwright — only quota-class 403s do."""
        import tempfile

        from pipeline.upload.upload import UploadError

        _done_job("j-non-quota")
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"\x00" * 16)
            local_mp4 = f.name
        try:
            jobs_mod.get_jobs().update("j-non-quota", preview_local_path=local_mp4)
            with patch.dict(os.environ, {"YTFACTORY_SIM_WORKER": "0"}):
                with patch(
                    "pipeline.upload.upload.youtube_upload",
                    side_effect=UploadError("oauth token revoked"),
                ):
                    with patch(
                        "pipeline.upload.playwright_upload.playwright_upload",
                    ) as mock_pw:
                        app = _make_app()
                        transport = httpx.ASGITransport(app=app)
                        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                            r = await c.post(
                                "/api/jobs/j-non-quota/publish",
                                json={"visibility": "unlisted"},
                            )
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["status"], "failed")
            self.assertIn("oauth", body.get("error", "").lower())
            # Playwright must NOT have been invoked.
            self.assertFalse(mock_pw.called)
        finally:
            os.unlink(local_mp4)


class PublishPlaywrightModuleEnvCheckTest(unittest.TestCase):
    """Pin :func:`pipeline.upload.playwright_upload._check_environment`'s
    failure modes — every missing precondition must raise RuntimeError
    with a specific message, never silently no-op."""

    def test_check_raises_when_user_data_dir_unset(self) -> None:
        from pipeline.upload.playwright_upload import _check_environment

        with patch.dict(os.environ, {"YTFACTORY_CHROME_USER_DATA_DIR": ""}, clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                _check_environment()
            self.assertIn("YTFACTORY_CHROME_USER_DATA_DIR", str(ctx.exception))

    def test_check_raises_when_user_data_dir_does_not_exist(self) -> None:
        from pipeline.upload.playwright_upload import _check_environment

        with patch.dict(
            os.environ,
            {"YTFACTORY_CHROME_USER_DATA_DIR": "/definitely/not/a/real/path/xyz"},
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _check_environment()
            self.assertIn("does not", str(ctx.exception))


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
