"""Audit S1.23 + Q2.39 — security headers + cache-on-401 fix.

Pre-fix:
- web/server.py shipped no Content-Security-Policy /
  X-Frame-Options / Strict-Transport-Security / Referrer-Policy.
  With the XSS surfaces present (oauth_web_routes _html_done
  before audit S1.2), absence of CSP turned every XSS into full
  credential takeover.
- _perf_headers_middleware set Cache-Control on EVERY 200-or-not
  response. A transient auth blip would leave the browser staring
  at a stuck 401 for max-age=10 + stale-while-revalidate=60.
"""
from __future__ import annotations

import os

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

import unittest

import httpx

from web import server as _server


def _make_client():
    transport = httpx.ASGITransport(app=_server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestSecurityHeaders(unittest.IsolatedAsyncioTestCase):
    HEADERS = {"Authorization": "Bearer test-token"}

    async def test_csp_set_on_response(self) -> None:
        async with _make_client() as c:
            r = await c.get("/healthz", headers=self.HEADERS)
        csp = r.headers.get("Content-Security-Policy", "")
        # Audit S1.23 — CSP must be present and turn off the
        # default-src to 'self', script-src to 'self' (no inline JS),
        # frame-ancestors to 'none' (no clickjacking), object-src
        # to 'none' (no Flash/Java).
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)

    async def test_x_frame_options_deny(self) -> None:
        async with _make_client() as c:
            r = await c.get("/healthz", headers=self.HEADERS)
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")

    async def test_x_content_type_options_nosniff(self) -> None:
        async with _make_client() as c:
            r = await c.get("/healthz", headers=self.HEADERS)
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")

    async def test_referrer_policy_set(self) -> None:
        async with _make_client() as c:
            r = await c.get("/healthz", headers=self.HEADERS)
        self.assertEqual(
            r.headers.get("Referrer-Policy"),
            "strict-origin-when-cross-origin",
        )

    async def test_hsts_set(self) -> None:
        async with _make_client() as c:
            r = await c.get("/healthz", headers=self.HEADERS)
        hsts = r.headers.get("Strict-Transport-Security", "")
        self.assertIn("max-age=", hsts)
        self.assertIn("includeSubDomains", hsts)

    async def test_permissions_policy_set(self) -> None:
        async with _make_client() as c:
            r = await c.get("/healthz", headers=self.HEADERS)
        pp = r.headers.get("Permissions-Policy", "")
        self.assertIn("geolocation=()", pp)
        self.assertIn("camera=()", pp)


class TestCacheControlNotSetOnAuthFailures(unittest.IsolatedAsyncioTestCase):
    """Audit Q2.39 — pre-fix _perf_headers_middleware set
    Cache-Control: private, max-age=10, stale-while-revalidate=60
    on EVERY GET to a cacheable prefix, including 401/403. A
    transient auth blip then left users staring at a stuck 401 for
    up to 70s. The fix gates the cache header on
    response.status_code < 400."""

    async def test_4xx_response_on_cacheable_prefix_has_no_cache_control(self) -> None:
        # /api/cloud/health is in _CACHEABLE_GET_PREFIXES; without
        # a bearer token the auth middleware should refuse → 4xx.
        async with _make_client() as c:
            r = await c.get("/api/cloud/health")  # no Authorization header
        # 4xx (any of 401/403/422 — depends on the route's auth shape).
        # The fix gates on response.status_code < 400, so any 4xx must
        # NOT carry the SWR cache header.
        if r.status_code >= 400 and r.status_code < 500:
            cache = r.headers.get("Cache-Control", "")
            self.assertNotIn("max-age=10", cache)
            self.assertNotIn("stale-while-revalidate=60", cache)
        else:
            # If the endpoint is happily 200 in this env (auth disabled),
            # we still want to verify cache IS set (sanity check). The
            # negative test isn't reachable here — rely on the unit test
            # below that drives the middleware directly.
            pass

    async def test_middleware_skips_cache_on_synthetic_401(self) -> None:
        # Drive the middleware directly with a synthetic 401 response
        # so we don't depend on which routes 401 in this env.
        from starlette.responses import JSONResponse
        from starlette.requests import Request as StarletteRequest

        async def fake_call_next(request):
            return JSONResponse({"detail": "unauth"}, status_code=401)

        scope = {
            "type": "http", "method": "GET", "headers": [],
            "path": "/api/cloud/health",
            "query_string": b"", "scheme": "http", "server": ("test", 80),
            "client": ("127.0.0.1", 0), "root_path": "", "app": _server.app,
        }
        request = StarletteRequest(scope)
        response = await _server._perf_headers_middleware(request, fake_call_next)
        self.assertEqual(response.status_code, 401)
        cache = response.headers.get("Cache-Control", "")
        self.assertNotIn("max-age=10", cache)
        self.assertNotIn("stale-while-revalidate=60", cache)

    async def test_middleware_does_set_cache_on_200(self) -> None:
        # Sanity: the cache header SHOULD apply to a 200 on a
        # cacheable prefix. Pins the regression direction.
        from starlette.responses import JSONResponse
        from starlette.requests import Request as StarletteRequest

        async def fake_call_next(request):
            return JSONResponse({"ok": True}, status_code=200)

        scope = {
            "type": "http", "method": "GET", "headers": [],
            "path": "/api/cloud/health",
            "query_string": b"", "scheme": "http", "server": ("test", 80),
            "client": ("127.0.0.1", 0), "root_path": "", "app": _server.app,
        }
        request = StarletteRequest(scope)
        response = await _server._perf_headers_middleware(request, fake_call_next)
        self.assertEqual(response.status_code, 200)
        cache = response.headers.get("Cache-Control", "")
        self.assertIn("max-age=10", cache)


if __name__ == "__main__":
    unittest.main()
