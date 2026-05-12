"""Audit Q2.43 — `/api/jobs/{id}/events` SSE falls through SCRIPT_JOBS
and control-plane jobs.

Pre-fix the SSE handler only checked `JOBS.get(job_id)` and 404'd for
any other tier. The companion `GET /api/jobs/{id}` correctly fell
through to SCRIPT_JOBS + control-plane, but the events stream didn't
— so a chat-confirmed render's web-next page would log a 404 every
time it tried to subscribe to events.

Tests exercise: web/server.py
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Required env BEFORE import.
os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

for name in ("google.cloud", "google.cloud.storage", "google.cloud.firestore"):
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

import httpx

from web import server as _server


def _make_client():
    transport = httpx.ASGITransport(app=_server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestJobEventsFallthrough(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_job_id_still_404s(self):
        # Audit Q2.43 — only after SCRIPT_JOBS + control-plane both
        # miss should we 404.
        with patch.object(_server, "AGENT_TOKEN", "test-token"):
            async with _make_client() as c:
                r = await c.get(
                    "/api/jobs/no-such-id-123/events",
                    headers={"Authorization": "Bearer test-token"},
                )
        self.assertEqual(r.status_code, 404)

    async def test_script_jobs_id_no_longer_404s(self):
        jid = "script-job-test-" + "x" * 8
        _server.SCRIPT_JOBS[jid] = {
            "job_id": jid,
            "state": "done",   # done → stream emits one snapshot then exits
            "started_at": 0.0,
            "completed_at": 1.0,
            "mp4_path": None,
            "log_path": None,
        }
        try:
            with patch.object(_server, "AGENT_TOKEN", "test-token"):
                async with _make_client() as c:
                    r = await c.get(
                        f"/api/jobs/{jid}/events",
                        headers={"Authorization": "Bearer test-token"},
                        timeout=5.0,
                    )
            # Should NOT be 404; status 200 (streaming) is expected.
            self.assertEqual(r.status_code, 200)
            self.assertIn("text/event-stream", r.headers.get("content-type", ""))
        finally:
            _server.SCRIPT_JOBS.pop(jid, None)


    async def test_control_jobs_lookup_raise_falls_through_to_404(self):
        """Audit Q2.43 — when control_jobs.get_job raises (Firestore
        outage), the SSE handler falls back to 404 without 500'ing.
        Pinning the except branch coverage."""
        from control.core import jobs as control_jobs  # noqa: PLC0415

        with patch.object(_server, "AGENT_TOKEN", "test-token"), \
             patch.object(control_jobs, "get_job",
                          side_effect=RuntimeError("firestore offline")):
            async with _make_client() as c:
                r = await c.get(
                    "/api/jobs/no-such-id-789/events",
                    headers={"Authorization": "Bearer test-token"},
                )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
