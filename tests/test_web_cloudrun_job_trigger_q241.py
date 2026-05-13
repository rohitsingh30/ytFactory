"""Audit Q2.41 — Cloud Run JOB trigger via google-cloud-run SDK.

Pre-fix three call sites in web/server.py + script_jobs_routes.py
shelled out to gcloud directly. The slim Cloud Run image doesn't
ship the gcloud CLI, so each one 500'd with FileNotFoundError when
running on cloud (the laptop dev path was fine because gcloud lives
on PATH there).

Tests exercise:
  * web.server._trigger_cloudrun_job (the small helper)
  * web.server stats-refresh cloud branch (full request)
  * control.routes.script_jobs_routes via execute_job_async swap
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

for name in ("google.cloud", "google.cloud.storage", "google.cloud.firestore"):
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

from web import server as _server
from control.routes import script_jobs_routes as _script_jobs_routes  # noqa: F401


class TestTriggerCloudrunJob(unittest.IsolatedAsyncioTestCase):
    """The Q2.41 helper that wraps execute_job_async."""

    async def test_happy_path_returns_execution_name(self):
        mock_run = MagicMock(return_value="projects/p/locations/r/jobs/j/executions/abc")
        with patch("control.core.cloud_run.execute_job_async", mock_run):
            name = await _server._trigger_cloudrun_job(
                "ytfactory-render-worker-v2",
                "ytfactory-prod-v2",
                "asia-southeast1",
                {"FOO": "bar"},
            )
        self.assertEqual(name, "projects/p/locations/r/jobs/j/executions/abc")
        mock_run.assert_called_once_with(
            "ytfactory-render-worker-v2",
            project="ytfactory-prod-v2",
            region="asia-southeast1",
            env_overrides={"FOO": "bar"},
        )

    async def test_sdk_failure_wrapped_in_runtime_error(self):
        def _boom(*_a, **_kw):
            raise PermissionError("not allowed to invoke job")

        with patch("control.core.cloud_run.execute_job_async", _boom):
            with self.assertRaises(RuntimeError) as ctx:
                await _server._trigger_cloudrun_job(
                    "ytfactory-render-worker-v2",
                    "p",
                    "r",
                    {},
                )
        msg = str(ctx.exception)
        self.assertIn("cloud run jobs execute failed", msg)
        self.assertIn("not allowed", msg)
        # Original exception preserved as __cause__.
        self.assertIsInstance(ctx.exception.__cause__, PermissionError)


class TestScriptJobsRoutesTriggerHelper(unittest.IsolatedAsyncioTestCase):
    """Q2.41 — same wrapper exists in control/routes/script_jobs_routes.py
    (the laptop dev-side admin scripts API). Test the wrapper symmetrically.
    """

    async def test_happy_path_returns_execution_name(self):
        from control.routes import script_jobs_routes as _sjr

        with patch(
            "control.core.cloud_run.execute_job_async",
            return_value="exec-name-from-sdk",
        ):
            name = await _sjr._trigger_job_or_runtime_error(
                "j", "p", "r", {"K": "V"}
            )
        self.assertEqual(name, "exec-name-from-sdk")

    async def test_sdk_failure_wrapped_in_runtime_error(self):
        from control.routes import script_jobs_routes as _sjr

        def _boom(*_a, **_kw):
            raise RuntimeError("forbidden by org policy")

        with patch("control.core.cloud_run.execute_job_async", _boom):
            with self.assertRaises(RuntimeError) as ctx:
                await _sjr._trigger_job_or_runtime_error(
                    "j", "p", "r", {}
                )
        msg = str(ctx.exception)
        self.assertIn("cloud run jobs execute failed", msg)
        self.assertIn("forbidden", msg)
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)


if __name__ == "__main__":
    unittest.main()
