"""Audit T1.20 — /api/youtube/auth/start/{account} hard-stop on Cloud Run.

Pre-fix the endpoint shelled to a localhost-OAuth subprocess that
needs a display + a localhost callback the operator's browser can
reach. On Cloud Run none of that works (no display, no callback,
plus per-request revisions kill the subprocess between start +
poll). Now the endpoint refuses with HTTP 501 + a pointer to the
proper /api/oauth/start path.
"""
from __future__ import annotations

import os

# Set env BEFORE importing web.server.
os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

import unittest
from unittest.mock import patch

import httpx

from web import server as _server


def _make_client():
    transport = httpx.ASGITransport(app=_server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestYoutubeAuthStartCloudRunHardStop(unittest.IsolatedAsyncioTestCase):
    HEADERS = {"Authorization": "Bearer test-token"}

    async def test_cloud_run_returns_501_with_pointer_to_web_oauth(self) -> None:
        # When K_SERVICE is set (Cloud Run runtime), the endpoint must
        # refuse with 501 and tell the operator about /api/oauth/start.
        # Patch web.server.AGENT_TOKEN directly because the module-level
        # cache may be None if another test imported web.server first
        # without YTFACTORY_AGENT_TOKEN set.
        with patch.dict(os.environ, {"K_SERVICE": "ytfactory-web"}), \
             patch.object(_server, "AGENT_TOKEN", "test-token"):
            async with _make_client() as c:
                r = await c.post(
                    "/api/youtube/auth/start/historyrecapped",
                    headers=self.HEADERS,
                )
        self.assertEqual(r.status_code, 501)
        body = r.json()
        self.assertIn("/api/oauth/start", body["detail"])
        self.assertIn("historyrecapped", body["detail"])

    async def test_laptop_path_still_validates_account(self) -> None:
        # On laptop (no K_SERVICE) the legacy localhost-OAuth subprocess
        # path runs; an unknown account still 404s before we spawn.
        os.environ.pop("K_SERVICE", None)
        with patch.object(_server, "AGENT_TOKEN", "test-token"):
            async with _make_client() as c:
                r = await c.post(
                    "/api/youtube/auth/start/no-such-account",
                    headers=self.HEADERS,
                )
        self.assertEqual(r.status_code, 404)


class TestYoutubeAuthStatusUsesPublicLoadRegistry(unittest.IsolatedAsyncioTestCase):
    """Audit S1.9 — /api/youtube/auth/status must reach the public
    ``cross_engage.load_registry`` alias, not the underscore-prefixed
    private helper. Pin the live wiring by exercising the endpoint
    end-to-end with patched dependencies and asserting that the public
    alias was the one called.
    """

    async def test_status_route_calls_public_load_registry(self) -> None:
        os.environ.pop("K_SERVICE", None)
        from pipeline.research import cross_engage as _ce
        from pipeline.upload import upload as _upload

        accounts = ["historyrecapped", "mystoriesanimated"]
        registry = {
            "historyrecapped": {"channel_id": "UC_history", "title": "History Recapped"},
        }

        with patch.object(_ce, "list_sibling_accounts", return_value=accounts), \
             patch.object(_ce, "load_registry", return_value=registry) as m_load, \
             patch.object(_upload, "inspect_token_status",
                          side_effect=lambda a: {"account": a, "ok": True}), \
             patch.object(_server, "AGENT_TOKEN", "test-token"):
            async with _make_client() as c:
                r = await c.get(
                    "/api/youtube/auth/status",
                    headers={"Authorization": "Bearer test-token"},
                )

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["accounts"]), 2)
        # The public alias must have been called.
        m_load.assert_called_once_with()
        # And the registry data must have wired through.
        history = next(a for a in body["accounts"] if a["account"] == "historyrecapped")
        self.assertEqual(history["channel_id"], "UC_history")
        self.assertEqual(history["channel_title"], "History Recapped")


if __name__ == "__main__":
    unittest.main()
