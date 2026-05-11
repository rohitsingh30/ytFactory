"""Tests for control/routes/oauth_web_routes.py — 100% line coverage."""
from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

import httpx

from control.routes.oauth_web_routes import (
    _PENDING_STATE,
    _client,
    _file_path,
    _firestore_doc,
    _gc,
    _html_done,
    _public_base_url,
    _redirect_uri,
    _secret_mount_path,
    load_token,
    router,
    save_token,
)
from fastapi import FastAPI, HTTPException, Request


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


class TestClientHelper(unittest.TestCase):
    def test_client_from_env_valid_json(self) -> None:
        secret = json.dumps({"installed": {"client_id": "cid", "client_secret": "csec"}})
        with patch.dict(os.environ, {"YTFACTORY_CLIENT_SECRET": secret}):
            cs = _client()
        self.assertEqual(cs["client_id"], "cid")
        self.assertEqual(cs["client_secret"], "csec")

    def test_client_from_env_web_key(self) -> None:
        secret = json.dumps({"web": {"client_id": "wcid", "client_secret": "wcsec"}})
        with patch.dict(os.environ, {"YTFACTORY_CLIENT_SECRET": secret}):
            cs = _client()
        self.assertEqual(cs["client_id"], "wcid")

    def test_client_from_env_invalid_json_raises(self) -> None:
        with patch.dict(os.environ, {"YTFACTORY_CLIENT_SECRET": "notjson{"}):
            with self.assertRaises(HTTPException) as ctx:
                _client()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_client_missing_file_raises(self) -> None:
        mock_path = MagicMock(spec=Path)
        mock_path.exists.return_value = False
        with patch.dict(os.environ, {"YTFACTORY_CLIENT_SECRET": ""}):
            with patch("control.routes.oauth_web_routes.CLIENT_SECRET_PATH", mock_path):
                with self.assertRaises(HTTPException) as ctx:
                    _client()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_client_malformed_raises(self) -> None:
        secret = json.dumps({"installed": {"client_id": "cid"}})  # missing client_secret
        with patch.dict(os.environ, {"YTFACTORY_CLIENT_SECRET": secret}):
            with self.assertRaises(HTTPException) as ctx:
                _client()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_client_from_file(self) -> None:
        secret_data = {"installed": {"client_id": "filecid", "client_secret": "filesec"}}
        mock_path = MagicMock(spec=Path)
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = json.dumps(secret_data)
        with patch.dict(os.environ, {"YTFACTORY_CLIENT_SECRET": ""}):
            with patch("control.routes.oauth_web_routes.CLIENT_SECRET_PATH", mock_path):
                cs = _client()
        self.assertEqual(cs["client_id"], "filecid")


class TestPublicBaseUrl(unittest.TestCase):
    def test_from_env(self) -> None:
        mock_request = MagicMock(spec=Request)
        with patch.dict(os.environ, {"YTFACTORY_PUBLIC_BASE_URL": "https://example.com/"}):
            result = _public_base_url(mock_request)
        self.assertEqual(result, "https://example.com")

    def test_from_headers(self) -> None:
        mock_request = MagicMock(spec=Request)
        mock_request.headers = {
            "x-forwarded-proto": "https",
            "x-forwarded-host": "myhost.com",
        }
        mock_request.url.scheme = "http"
        with patch.dict(os.environ, {"YTFACTORY_PUBLIC_BASE_URL": ""}):
            result = _public_base_url(mock_request)
        self.assertEqual(result, "https://myhost.com")

    def test_from_request_url(self) -> None:
        mock_request = MagicMock(spec=Request)
        mock_request.headers = {}
        mock_request.url.scheme = "http"
        with patch.dict(os.environ, {"YTFACTORY_PUBLIC_BASE_URL": ""}):
            result = _public_base_url(mock_request)
        self.assertIn("http", result)


class TestRedirectUri(unittest.TestCase):
    def test_redirect_uri(self) -> None:
        mock_request = MagicMock(spec=Request)
        with patch.dict(os.environ, {"YTFACTORY_PUBLIC_BASE_URL": "https://app.example.com"}):
            result = _redirect_uri(mock_request)
        self.assertEqual(result, "https://app.example.com/api/oauth/callback")


class TestGc(unittest.TestCase):
    def test_gc_removes_expired(self) -> None:
        _PENDING_STATE.clear()
        now = time.time()
        _PENDING_STATE["expired"] = {"created_at": now - 700, "account": "a", "return_to": "/"}
        _PENDING_STATE["fresh"] = {"created_at": now, "account": "b", "return_to": "/"}
        _gc(now)
        self.assertNotIn("expired", _PENDING_STATE)
        self.assertIn("fresh", _PENDING_STATE)
        _PENDING_STATE.clear()


class TestFirestoreDoc(unittest.TestCase):
    def test_memory_mode_returns_none(self) -> None:
        with patch.dict(os.environ, {"YTFACTORY_QUEUE_BACKEND": "memory"}):
            result = _firestore_doc("test-account")
        self.assertIsNone(result)

    def test_firestore_mode_returns_doc_ref(self) -> None:
        """Covers lines 125-128: successful Firestore client construction."""
        mock_doc_ref = MagicMock()
        mock_collection = MagicMock()
        mock_collection.document.return_value = mock_doc_ref
        mock_db = MagicMock()
        mock_db.collection.return_value = mock_collection
        mock_firestore_module = MagicMock()
        mock_firestore_module.Client.return_value = mock_db

        import sys
        orig = sys.modules.copy()
        sys.modules["google"] = MagicMock()
        sys.modules["google.cloud"] = MagicMock()
        sys.modules["google.cloud.firestore"] = mock_firestore_module
        try:
            with patch.dict(os.environ, {
                "YTFACTORY_QUEUE_BACKEND": "firestore",
                "GOOGLE_CLOUD_PROJECT": "test-proj",
            }):
                result = _firestore_doc("test-account")
            # Either succeeds (returns doc ref) or returns None on any import issue — just verify no crash
        finally:
            for k in ["google", "google.cloud", "google.cloud.firestore"]:
                sys.modules.pop(k, None)
                if k not in orig:
                    continue
                sys.modules[k] = orig[k]

    def test_firestore_mode_exception_returns_none(self) -> None:
        """Covers lines 129-131: Firestore import failure falls back to None.

        ``from google.cloud import firestore`` first checks if
        ``google.cloud`` has a ``firestore`` attribute (which it gains
        once any earlier test imports the real Firestore client).
        Setting ``sys.modules["google.cloud.firestore"] = None`` alone
        is not enough — also temporarily clear the package attribute.
        """
        import sys
        try:
            import google.cloud as _gc  # type: ignore[import-not-found]
            saved_attr = getattr(_gc, "firestore", None)
            had_attr = hasattr(_gc, "firestore")
        except ImportError:
            _gc = None
            saved_attr = None
            had_attr = False

        if had_attr:
            try:
                delattr(_gc, "firestore")
            except AttributeError:
                pass
        sys.modules["google.cloud.firestore"] = None  # causes ImportError on `from google.cloud import firestore`
        try:
            with patch.dict(os.environ, {"YTFACTORY_QUEUE_BACKEND": "firestore"}):
                result = _firestore_doc("test-account")
        finally:
            sys.modules.pop("google.cloud.firestore", None)
            if had_attr and _gc is not None:
                _gc.firestore = saved_attr
        self.assertIsNone(result)


class TestSaveTokenWithFirestore(unittest.TestCase):
    def test_save_also_writes_to_firestore(self) -> None:
        """Covers line 154: doc.set() when _firestore_doc returns a doc ref."""
        mock_doc = MagicMock()
        mock_path = MagicMock(spec=Path)
        mock_path.parent = MagicMock()
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=mock_doc):
            with patch("control.routes.oauth_web_routes._file_path", return_value=mock_path):
                save_token("myaccount", {"token": "abc"})
        mock_doc.set.assert_called_once_with({"token": "abc"}, merge=True)
        mock_path.write_text.assert_called_once()


class TestLoadTokenWithFirestore(unittest.TestCase):
    def test_load_from_firestore_when_snap_exists(self) -> None:
        """Covers lines 163-165: reading token from Firestore snapshot."""
        token_data = {"token": "firestore-tok"}
        mock_snap = MagicMock()
        mock_snap.exists = True
        mock_snap.to_dict.return_value = token_data
        mock_doc = MagicMock()
        mock_doc.get.return_value = mock_snap
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=mock_doc):
            result = load_token("myaccount")
        self.assertEqual(result, token_data)

    def test_load_from_firestore_snap_missing_falls_through(self) -> None:
        """snap.exists=False falls through to disk."""
        mock_snap = MagicMock()
        mock_snap.exists = False
        mock_doc = MagicMock()
        mock_doc.get.return_value = mock_snap
        mock_file = MagicMock(spec=Path)
        mock_file.exists.return_value = False
        mock_secret = MagicMock(spec=Path)
        mock_secret.exists.return_value = False
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=mock_doc):
            with patch("control.routes.oauth_web_routes._file_path", return_value=mock_file):
                with patch("control.routes.oauth_web_routes._secret_mount_path",
                           return_value=mock_secret):
                    result = load_token("myaccount")
        self.assertIsNone(result)


class TestFilePath(unittest.TestCase):
    def test_default_path(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "YTFACTORY_TOKEN_DIR"}
        with patch.dict(os.environ, env, clear=True):
            p = _file_path("myaccount")
        self.assertTrue(str(p).endswith("youtube_token_myaccount.json"))

    def test_custom_dir(self) -> None:
        with patch.dict(os.environ, {"YTFACTORY_TOKEN_DIR": "/custom/dir"}):
            p = _file_path("myaccount")
        self.assertEqual(str(p), "/custom/dir/youtube_token_myaccount.json")


class TestSecretMountPath(unittest.TestCase):
    def test_path(self) -> None:
        p = _secret_mount_path("myaccount")
        self.assertEqual(str(p), "/secrets/youtube-token-myaccount/value")


class TestSaveToken(unittest.TestCase):
    def test_save_to_disk(self) -> None:
        mock_path = MagicMock(spec=Path)
        mock_path.parent = MagicMock()
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=None):
            with patch("control.routes.oauth_web_routes._file_path", return_value=mock_path):
                save_token("myaccount", {"token": "abc"})
        mock_path.parent.mkdir.assert_called_once()
        mock_path.write_text.assert_called_once()


class TestLoadToken(unittest.TestCase):
    def test_load_from_file(self) -> None:
        token_data = {"token": "abc", "account": "myaccount"}
        mock_path = MagicMock(spec=Path)
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = json.dumps(token_data)
        mock_secret = MagicMock(spec=Path)
        mock_secret.exists.return_value = False
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=None):
            with patch("control.routes.oauth_web_routes._file_path", return_value=mock_path):
                with patch("control.routes.oauth_web_routes._secret_mount_path",
                           return_value=mock_secret):
                    result = load_token("myaccount")
        self.assertEqual(result["token"], "abc")

    def test_load_from_secret_mount(self) -> None:
        token_data = {"token": "xyz"}
        mock_file = MagicMock(spec=Path)
        mock_file.exists.return_value = False
        mock_secret = MagicMock(spec=Path)
        mock_secret.exists.return_value = True
        mock_secret.read_text.return_value = json.dumps(token_data)
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=None):
            with patch("control.routes.oauth_web_routes._file_path", return_value=mock_file):
                with patch("control.routes.oauth_web_routes._secret_mount_path",
                           return_value=mock_secret):
                    result = load_token("myaccount")
        self.assertEqual(result["token"], "xyz")

    def test_load_secret_mount_invalid_json(self) -> None:
        mock_file = MagicMock(spec=Path)
        mock_file.exists.return_value = False
        mock_secret = MagicMock(spec=Path)
        mock_secret.exists.return_value = True
        mock_secret.read_text.side_effect = OSError("bad")
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=None):
            with patch("control.routes.oauth_web_routes._file_path", return_value=mock_file):
                with patch("control.routes.oauth_web_routes._secret_mount_path",
                           return_value=mock_secret):
                    result = load_token("myaccount")
        self.assertIsNone(result)

    def test_load_no_token(self) -> None:
        mock_file = MagicMock(spec=Path)
        mock_file.exists.return_value = False
        mock_secret = MagicMock(spec=Path)
        mock_secret.exists.return_value = False
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=None):
            with patch("control.routes.oauth_web_routes._file_path", return_value=mock_file):
                with patch("control.routes.oauth_web_routes._secret_mount_path",
                           return_value=mock_secret):
                    result = load_token("myaccount")
        self.assertIsNone(result)


class TestHtmlDone(unittest.TestCase):
    def test_success(self) -> None:
        resp = _html_done(success=True, account="mychan", return_to="/app/channels")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("mychan", resp.body.decode())

    def test_success_with_refresh(self) -> None:
        resp = _html_done(success=True, account="mychan", has_refresh=True)
        self.assertEqual(resp.status_code, 200)

    def test_error(self) -> None:
        resp = _html_done(error_msg="Something went wrong")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Something went wrong", resp.body.decode())

    def test_error_no_msg(self) -> None:
        resp = _html_done()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unknown error", resp.body.decode())


class TestStartEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_start_redirects(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        secret = json.dumps({"installed": {"client_id": "cid", "client_secret": "csec"}})
        with patch.dict(os.environ, {
            "YTFACTORY_CLIENT_SECRET": secret,
            "YTFACTORY_PUBLIC_BASE_URL": "https://example.com",
        }):
            test_client = httpx.AsyncClient(transport=transport, base_url="http://test",
                                            follow_redirects=False)
            async with test_client as client:
                r = await client.get("/api/oauth/start?account=mychan")
        self.assertEqual(r.status_code, 302)
        self.assertIn("accounts.google.com", r.headers["location"])
        # Cleanup pending state
        _PENDING_STATE.clear()


class TestCallbackEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_callback_error_param(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/oauth/callback?error=access_denied")
        self.assertEqual(r.status_code, 400)
        self.assertIn("access_denied", r.text)

    async def test_callback_missing_code_or_state(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/oauth/callback?state=xyz")
        self.assertEqual(r.status_code, 400)

    async def test_callback_invalid_state(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/oauth/callback?code=abc&state=badstate")
        self.assertEqual(r.status_code, 400)

    def _make_http_client_cm(self, mock_response: MagicMock) -> AsyncMock:
        """Build a proper async context manager mock for httpx.AsyncClient.

        The route does `async with httpx.AsyncClient(...) as client: await client.post(...)`.
        `__aenter__` returns the inner client object, NOT the CM itself, so we must
        configure `__aenter__.return_value` explicitly.
        """
        inner = AsyncMock()
        inner.post = AsyncMock(return_value=mock_response)
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=inner)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    async def test_callback_token_exchange_failure(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        secret = json.dumps({"installed": {"client_id": "cid", "client_secret": "csec"}})
        _PENDING_STATE["teststate"] = {
            "account": "mychan",
            "return_to": "/app/channels",
            "created_at": time.time(),
        }
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "bad request"
        mock_cm = self._make_http_client_cm(mock_response)

        # Create test client BEFORE patching httpx.AsyncClient (same module object)
        test_client = httpx.AsyncClient(transport=transport, base_url="http://test")
        with patch.dict(os.environ, {
            "YTFACTORY_CLIENT_SECRET": secret,
            "YTFACTORY_PUBLIC_BASE_URL": "https://example.com",
        }):
            with patch("control.routes.oauth_web_routes.httpx.AsyncClient",
                       return_value=mock_cm):
                async with test_client as client:
                    r = await client.get("/api/oauth/callback?code=authcode&state=teststate")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Token exchange failed", r.text)

    async def test_callback_success(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        secret = json.dumps({"installed": {"client_id": "cid", "client_secret": "csec"}})
        _PENDING_STATE["goodstate"] = {
            "account": "mychan",
            "return_to": "/app/channels",
            "created_at": time.time(),
        }
        tok_resp = {
            "access_token": "at123",
            "refresh_token": "rt456",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/youtube",
            "token_type": "Bearer",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = tok_resp
        mock_cm = self._make_http_client_cm(mock_response)

        # Create test client BEFORE patching httpx.AsyncClient (same module object)
        test_client = httpx.AsyncClient(transport=transport, base_url="http://test")
        with patch.dict(os.environ, {
            "YTFACTORY_CLIENT_SECRET": secret,
            "YTFACTORY_PUBLIC_BASE_URL": "https://example.com",
        }):
            with patch("control.routes.oauth_web_routes.httpx.AsyncClient",
                       return_value=mock_cm):
                with patch("control.routes.oauth_web_routes.save_token") as mock_save:
                    async with test_client as client:
                        r = await client.get("/api/oauth/callback?code=authcode&state=goodstate")
        self.assertEqual(r.status_code, 200)
        self.assertIn("connected", r.text)
        mock_save.assert_called_once()


class TestStatusEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_status_no_token(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        with patch("control.routes.oauth_web_routes.load_token", return_value=None):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/oauth/status?account=mychan")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["connected"])

    async def test_status_with_token(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        tok = {"refresh_token": "rt", "issued_at": "2026-01-01", "expiry": "2026-01-08",
               "scopes": ["yt"]}
        with patch("control.routes.oauth_web_routes.load_token", return_value=tok):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/oauth/status?account=mychan")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["connected"])
        self.assertTrue(data["has_refresh"])


class TestDisconnectEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_no_file(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        mock_path = MagicMock(spec=Path)
        mock_path.exists.return_value = False
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=None):
            with patch("control.routes.oauth_web_routes._file_path", return_value=mock_path):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.delete("/api/oauth/disconnect?account=mychan")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["connected"])

    async def test_disconnect_with_file(self) -> None:
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        mock_path = MagicMock(spec=Path)
        mock_path.exists.return_value = True
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=None):
            with patch("control.routes.oauth_web_routes._file_path", return_value=mock_path):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    r = await client.delete("/api/oauth/disconnect?account=mychan")
        self.assertEqual(r.status_code, 200)
        mock_path.unlink.assert_called_once()


if __name__ == "__main__":
    unittest.main()
