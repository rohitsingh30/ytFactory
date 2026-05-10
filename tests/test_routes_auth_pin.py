"""Tests for control/routes/auth_pin.py — 100% line coverage."""
from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

import httpx

from control.routes.auth_pin import (
    COOKIE_NAME,
    PIN_ENV,
    SECRET_ENV,
    TTL_ENV,
    _make_token,
    _secret,
    _sign,
    _ttl_s,
    _verify_token,
    auth_enabled,
    require_pin,
    router,
)
from fastapi import FastAPI, HTTPException


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


class TestHelpers(unittest.TestCase):
    def test_auth_enabled_false(self) -> None:
        with patch.dict(os.environ, {PIN_ENV: ""}):
            self.assertFalse(auth_enabled())

    def test_auth_enabled_true(self) -> None:
        with patch.dict(os.environ, {PIN_ENV: "1234"}):
            self.assertTrue(auth_enabled())

    def test_secret_from_env(self) -> None:
        with patch.dict(os.environ, {SECRET_ENV: "mysecret"}):
            self.assertEqual(_secret(), b"mysecret")

    def test_secret_fallback(self) -> None:
        env = os.environ.copy()
        env.pop(SECRET_ENV, None)
        with patch.dict(os.environ, env, clear=True):
            s = _secret()
            self.assertIsInstance(s, bytes)
            self.assertTrue(len(s) > 0)

    def test_ttl_valid(self) -> None:
        with patch.dict(os.environ, {TTL_ENV: "3600"}):
            self.assertEqual(_ttl_s(), 3600)

    def test_ttl_default(self) -> None:
        env = os.environ.copy()
        env.pop(TTL_ENV, None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_ttl_s(), 604800)

    def test_ttl_invalid(self) -> None:
        with patch.dict(os.environ, {TTL_ENV: "notanint"}):
            self.assertEqual(_ttl_s(), 604800)

    def test_sign_deterministic(self) -> None:
        with patch.dict(os.environ, {SECRET_ENV: "s3cr3t"}):
            s1 = _sign("payload=hello")
            s2 = _sign("payload=hello")
            self.assertEqual(s1, s2)

    def test_make_token_contains_issued(self) -> None:
        tok = _make_token(now_s=1000000)
        self.assertIn("issued=1000000", tok)
        self.assertIn(".", tok)

    def test_make_token_default_time(self) -> None:
        before = int(time.time())
        tok = _make_token()
        after = int(time.time())
        issued = int(tok.split("=", 1)[1].split(".")[0])
        self.assertGreaterEqual(issued, before)
        self.assertLessEqual(issued, after)

    def test_verify_token_none(self) -> None:
        self.assertFalse(_verify_token(None))

    def test_verify_token_no_dot(self) -> None:
        self.assertFalse(_verify_token("nodot"))

    def test_verify_token_wrong_sig(self) -> None:
        tok = _make_token(now_s=1000000)
        payload = tok.rsplit(".", 1)[0]
        self.assertFalse(_verify_token(f"{payload}.badsig"))

    def test_verify_token_wrong_payload_prefix(self) -> None:
        # Craft a token with a valid sig but wrong payload prefix
        payload = "notissued=1000000"
        sig = _sign(payload)
        self.assertFalse(_verify_token(f"{payload}.{sig}"))

    def test_verify_token_non_int_issued(self) -> None:
        payload = "issued=notanint"
        sig = _sign(payload)
        self.assertFalse(_verify_token(f"{payload}.{sig}"))

    def test_verify_token_expired(self) -> None:
        old_ts = int(time.time()) - 700000  # > 7 days
        tok = _make_token(now_s=old_ts)
        self.assertFalse(_verify_token(tok))

    def test_verify_token_valid(self) -> None:
        tok = _make_token()
        self.assertTrue(_verify_token(tok))


class TestRequirePin(unittest.TestCase):
    def test_no_auth_noop(self) -> None:
        with patch.dict(os.environ, {PIN_ENV: ""}):
            require_pin(None)  # should not raise

    def test_auth_enabled_valid_token(self) -> None:
        tok = _make_token()
        with patch.dict(os.environ, {PIN_ENV: "1234"}):
            require_pin(tok)  # should not raise

    def test_auth_enabled_invalid_token(self) -> None:
        with patch.dict(os.environ, {PIN_ENV: "1234"}):
            with self.assertRaises(HTTPException) as ctx:
                require_pin("badtoken")
            self.assertEqual(ctx.exception.status_code, 401)


class TestLoginEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_login_auth_disabled(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch.dict(os.environ, {PIN_ENV: ""}):
                r = await client.post("/api/auth/login", json={"pin": "anything"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["auth_required"])

    async def test_login_wrong_pin(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch.dict(os.environ, {PIN_ENV: "correct"}):
                r = await client.post("/api/auth/login", json={"pin": "wrong"})
        self.assertEqual(r.status_code, 401)

    async def test_login_correct_pin(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch.dict(os.environ, {PIN_ENV: "correct", "YTFACTORY_COOKIE_SECURE": "1"}):
                r = await client.post("/api/auth/login", json={"pin": "correct"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["logged_in"])
        self.assertIn(COOKIE_NAME, r.cookies)

    async def test_login_correct_pin_insecure(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch.dict(os.environ, {PIN_ENV: "mypin", "YTFACTORY_COOKIE_SECURE": "0"}):
                r = await client.post("/api/auth/login", json={"pin": "mypin"})
        self.assertEqual(r.status_code, 200)


class TestLogoutEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_logout(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/auth/logout")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["logged_in"])

    async def test_logout_auth_required_flag(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch.dict(os.environ, {PIN_ENV: "pin123"}):
                r = await client.post("/api/auth/logout")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["auth_required"])


class TestWhoamiEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_whoami_no_auth(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch.dict(os.environ, {PIN_ENV: ""}):
                r = await client.get("/api/auth/whoami")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["logged_in"])
        self.assertFalse(r.json()["auth_required"])

    async def test_whoami_auth_enabled_no_cookie(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch.dict(os.environ, {PIN_ENV: "pin123"}):
                r = await client.get("/api/auth/whoami")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["logged_in"])
        self.assertTrue(r.json()["auth_required"])

    async def test_whoami_auth_enabled_valid_cookie(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        tok = _make_token()
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.cookies.set(COOKIE_NAME, tok)
            with patch.dict(os.environ, {PIN_ENV: "pin123"}):
                r = await client.get("/api/auth/whoami")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["logged_in"])


if __name__ == "__main__":
    unittest.main()
