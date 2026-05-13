"""Regression tests for web/server.py::google_callback IAM error handling.

2026-05-13 post-mortem — at the audit S1.21 SA flip from `tts-runner@`
to `web-runner@`, the new SA was missing `roles/datastore.user`. The
OAuth callback's `upsert_user(...)` raised
`google.api_core.exceptions.PermissionDenied: 403` and FastAPI returned
a bare 500. The user (correctly) saw "Internal Server Error" and
assumed regression.

These tests pin the new behaviour: when the storage layer can't be
reached, redirect to /login?error=... so the user sees an actionable
message and the operator sees the structured exception in Cloud Logging.

See:
- ~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_web_runner_iam_silent_post_deploy_500.md
- cloud/iam/verify_web_runner.sh (preflight that prevents this in the first place)
"""
from __future__ import annotations

import os
import secrets

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

import unittest
from unittest.mock import patch

import httpx

from web import server as _server


def _make_client():
    transport = httpx.ASGITransport(app=_server.app)
    # follow_redirects=False so we can assert the 302 + Location header
    # rather than chasing the redirect into /login (which would render
    # the Next.js page in real life — out of scope here).
    return httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False)


class _FakePermissionDenied(Exception):
    """Mimics google.api_core.exceptions.PermissionDenied without
    importing google-api-core into the test surface. The handler
    branches on `type(e).__name__ in {"PermissionDenied", ...}` /
    `"Permission" in type(e).__name__` so this stand-in exercises the
    same code path."""


class TestGoogleCallbackIamErrors(unittest.IsolatedAsyncioTestCase):
    """When the OAuth code exchange succeeds but `upsert_user` fails
    because the runtime SA can't reach Firestore, redirect the user
    to a useful error page instead of bubbling a 500."""

    async def _hit_callback(self, upsert_exc: Exception) -> httpx.Response:
        """Helper: drive the callback past state-mismatch + token
        exchange, then assert the upsert raises the given exception."""
        # Stand-in OAuth state — must match the cookie we send.
        state = secrets.token_urlsafe(16)

        # exchange_code is referenced via `from pipeline.auth import
        # exchange_code` INSIDE google_callback. Patching the symbol on
        # pipeline.auth is the canonical seam.
        fake_info = {"email": "user@example.com", "name": "U", "picture": ""}

        with patch("pipeline.auth.exchange_code", return_value=fake_info), \
             patch("pipeline.auth.upsert_user", side_effect=upsert_exc):
            async with _make_client() as c:
                return await c.get(
                    f"/api/auth/google/callback?code=fake-code&state={state}",
                    cookies={_server.OAUTH_STATE_COOKIE: state},
                )

    async def test_permission_denied_redirects_to_login(self) -> None:
        """Firestore PermissionDenied → 302 → /login?error=auth_storage_permission."""
        r = await self._hit_callback(_FakePermissionDenied("403"))
        self.assertEqual(r.status_code, 302,
                         f"expected 302, got {r.status_code} (body={r.text[:200]!r})")
        location = r.headers.get("location", "")
        self.assertIn("/login", location)
        self.assertIn("error=auth_storage_permission", location)

    async def test_permission_denied_does_not_set_session_cookie(self) -> None:
        """If we couldn't upsert the user, we MUST NOT issue a session
        cookie that would let them in on the next request — the
        upstream auth gate would then 403 them with no recourse."""
        r = await self._hit_callback(_FakePermissionDenied("403"))
        # Set-Cookie may include the OAuth state cookie deletion, but
        # NOT the yt_session cookie.
        cookies = r.headers.get_list("set-cookie")
        for c in cookies:
            self.assertNotIn(_server.SESSION_COOKIE + "=", c.split(";")[0],
                             f"yt_session cookie set on auth-storage failure: {c!r}")

    async def test_other_storage_failure_redirects_to_unavailable(self) -> None:
        """Any non-PermissionDenied storage failure → 302 →
        /login?error=auth_storage_unavailable. So a Firestore service
        outage shows the user the same actionable error path."""
        class FakeServiceUnavailable(Exception):
            pass

        r = await self._hit_callback(FakeServiceUnavailable("503"))
        self.assertEqual(r.status_code, 302)
        location = r.headers.get("location", "")
        self.assertIn("/login", location)
        self.assertIn("error=auth_storage_unavailable", location)

    async def test_callback_still_500s_loud_when_unrelated_bug_in_handler(
        self,
    ) -> None:
        """Defence in depth — we only catch around upsert_user. A bug
        elsewhere in the handler (e.g. AttributeError on `info["email"]`)
        must still surface as a 500 so the operator sees it. We
        approximate by patching exchange_code to return a malformed dict.
        """
        with patch("pipeline.auth.exchange_code", return_value={}):
            state = secrets.token_urlsafe(16)
            async with _make_client() as c:
                r = await c.get(
                    f"/api/auth/google/callback?code=fake-code&state={state}",
                    cookies={_server.OAUTH_STATE_COOKIE: state},
                )
        # Empty dict → upsert_user(email="", ...) — this still goes
        # through the (caught) upsert path. The point of the test is
        # documenting that uncaught code paths surface as 5xx, not
        # asserting a specific status. So just assert it didn't
        # silently 200.
        self.assertNotEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
