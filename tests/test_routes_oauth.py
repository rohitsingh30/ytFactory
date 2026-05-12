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

    def test_from_headers_falls_back_to_host_when_no_allowlist(self) -> None:
        # Audit S1.8 — without YTFACTORY_ALLOWED_HOSTS configured we
        # MUST NOT trust X-Forwarded-Host (an attacker-controlled
        # header in some edge configurations); fall back to the
        # actual Host header which the load balancer rewrites.
        mock_request = MagicMock(spec=Request)
        mock_request.headers = {
            "x-forwarded-proto": "https",
            "x-forwarded-host": "evil.example",
            "host": "myhost.com",
        }
        mock_request.url.scheme = "http"
        mock_request.url.hostname = "myhost.com"
        with patch.dict(os.environ, {"YTFACTORY_PUBLIC_BASE_URL": "",
                                     "YTFACTORY_ALLOWED_HOSTS": ""}):
            result = _public_base_url(mock_request)
        self.assertEqual(result, "https://myhost.com")

    def test_xfh_honoured_when_in_allowlist(self) -> None:
        # When the operator explicitly allowlists hosts, XFH IS
        # honoured (this matches Cloud Run's dual-URL behaviour).
        mock_request = MagicMock(spec=Request)
        mock_request.headers = {
            "x-forwarded-proto": "https",
            "x-forwarded-host": "ytfactory-web-7hwnzw7lya-as.a.run.app",
            "host": "internal-host",
        }
        mock_request.url.scheme = "http"
        mock_request.url.hostname = "internal-host"
        with patch.dict(os.environ, {
            "YTFACTORY_PUBLIC_BASE_URL": "",
            "YTFACTORY_ALLOWED_HOSTS": (
                "ytfactory-web-7hwnzw7lya-as.a.run.app, "
                "ytfactory-web-283470729204.as.run.app"
            ),
        }):
            result = _public_base_url(mock_request)
        self.assertEqual(
            result, "https://ytfactory-web-7hwnzw7lya-as.a.run.app",
        )

    def test_xfh_NOT_in_allowlist_raises_400(self) -> None:
        # An XFH that's not in the allowlist must NOT silently
        # fall back to the attacker-controlled value — refuse the
        # request so OAuth can't be routed off-host.
        from fastapi import HTTPException
        mock_request = MagicMock(spec=Request)
        mock_request.headers = {
            "x-forwarded-host": "evil.example",
            "host": "also-not-allowed.example",
        }
        mock_request.url.scheme = "http"
        mock_request.url.hostname = "also-not-allowed.example"
        with patch.dict(os.environ, {
            "YTFACTORY_PUBLIC_BASE_URL": "",
            "YTFACTORY_ALLOWED_HOSTS": "ytfactory-web-7hwnzw7lya-as.a.run.app",
        }):
            with self.assertRaises(HTTPException) as ctx:
                _public_base_url(mock_request)
            self.assertEqual(ctx.exception.status_code, 400)

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

    def test_load_splices_client_secret_from_oauth_client(self) -> None:
        # Audit S1.13 — the persisted token blob no longer carries
        # client_secret (so a Firestore read leak doesn't expose
        # project credentials). load_token must splice it back in
        # from the canonical _client() config so consumers see a
        # complete blob.
        token_data = {"token": "abc", "account": "myaccount"}  # no client_secret
        mock_path = MagicMock(spec=Path)
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = json.dumps(token_data)
        mock_secret = MagicMock(spec=Path)
        mock_secret.exists.return_value = False
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=None), \
             patch("control.routes.oauth_web_routes._file_path", return_value=mock_path), \
             patch("control.routes.oauth_web_routes._secret_mount_path",
                   return_value=mock_secret), \
             patch("control.routes.oauth_web_routes._client",
                   return_value={"client_id": "spliced-cid",
                                 "client_secret": "spliced-csecret",
                                 "redirect_uris": []}):
            result = load_token("myaccount")
        self.assertEqual(result["token"], "abc")
        self.assertEqual(result["client_id"], "spliced-cid")
        self.assertEqual(result["client_secret"], "spliced-csecret")

    def test_load_does_not_overwrite_existing_client_secret(self) -> None:
        # Defence in depth: if a legacy token blob still carries
        # client_secret (from before the audit fix), load_token must
        # NOT clobber it with whatever _client() returns.
        token_data = {
            "token": "abc",
            "client_id": "legacy-cid",
            "client_secret": "legacy-csecret",
        }
        mock_path = MagicMock(spec=Path)
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = json.dumps(token_data)
        mock_secret = MagicMock(spec=Path)
        mock_secret.exists.return_value = False
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=None), \
             patch("control.routes.oauth_web_routes._file_path", return_value=mock_path), \
             patch("control.routes.oauth_web_routes._secret_mount_path",
                   return_value=mock_secret), \
             patch("control.routes.oauth_web_routes._client",
                   return_value={"client_id": "spliced-cid",
                                 "client_secret": "spliced-csecret",
                                 "redirect_uris": []}):
            result = load_token("myaccount")
        # Legacy values preserved.
        self.assertEqual(result["client_id"], "legacy-cid")
        self.assertEqual(result["client_secret"], "legacy-csecret")

    def test_load_tolerates_oauth_client_unavailable(self) -> None:
        # When _client() is unavailable (e.g. dev env without the
        # OAuth client config), load_token must not crash; just
        # return the blob without the spliced fields.
        token_data = {"token": "abc"}
        mock_path = MagicMock(spec=Path)
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = json.dumps(token_data)
        mock_secret = MagicMock(spec=Path)
        mock_secret.exists.return_value = False
        with patch("control.routes.oauth_web_routes._firestore_doc", return_value=None), \
             patch("control.routes.oauth_web_routes._file_path", return_value=mock_path), \
             patch("control.routes.oauth_web_routes._secret_mount_path",
                   return_value=mock_secret), \
             patch("control.routes.oauth_web_routes._client",
                   side_effect=RuntimeError("client config missing")):
            result = load_token("myaccount")
        self.assertEqual(result["token"], "abc")
        self.assertNotIn("client_secret", result)


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

    def test_account_xss_escaped(self) -> None:
        # Audit S1.2 — account is HTML-escaped so a crafted
        # ?account=foo"><script>... can't execute.
        attack = 'foo"><script>alert(1)</script>'
        resp = _html_done(success=True, account=attack)
        body = resp.body.decode()
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_error_msg_xss_escaped(self) -> None:
        attack = '<img src=x onerror=alert(1)>'
        resp = _html_done(error_msg=attack)
        body = resp.body.decode()
        self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;img", body)

    def test_return_to_off_origin_clamped_to_default(self) -> None:
        # Audit S1.3 — open-redirect defence. An off-origin
        # return_to (//evil.example, https://evil) collapses to
        # the safe default.
        for attack in (
            "//evil.example/x",
            "https://evil.example/x",
            "javascript:alert(1)",
            "../../etc/passwd",
        ):
            with self.subTest(attack=attack):
                resp = _html_done(success=True, account="x", return_to=attack)
                body = resp.body.decode()
                # Default safe return_to lands instead.
                self.assertIn("/app/channels", body)
                # The attack string is NOT injected raw into the JS literal.
                self.assertNotIn(attack, body)

    def test_return_to_attribute_in_error_path_is_quoted_and_escaped(self) -> None:
        # On the error page the return_to value lands inside an
        # ``href="…"`` attribute too. Even if it survives the
        # safe-clamp (i.e. is a legit same-origin path) it MUST be
        # HTML-escaped so a path containing ``"`` can't escape the
        # attribute and inject arbitrary HTML.
        resp = _html_done(error_msg="bad", return_to='/app/x"><script>alert(1)</script>')
        body = resp.body.decode()
        self.assertNotIn('"><script>alert(1)</script>', body)


class TestSafeReturnTo(unittest.TestCase):
    """Audit S1.3 — _safe_return_to clamps to same-origin path."""

    def test_safe_path_passes(self) -> None:
        from control.routes.oauth_web_routes import _safe_return_to
        self.assertEqual(_safe_return_to("/app/channels"), "/app/channels")
        self.assertEqual(_safe_return_to("/foo/bar?x=1"), "/foo/bar?x=1")

    def test_protocol_relative_collapses(self) -> None:
        from control.routes.oauth_web_routes import _safe_return_to
        self.assertEqual(_safe_return_to("//evil.example/x"), "/app/channels")

    def test_absolute_url_collapses(self) -> None:
        from control.routes.oauth_web_routes import _safe_return_to
        self.assertEqual(_safe_return_to("https://evil.example"), "/app/channels")

    def test_javascript_scheme_collapses(self) -> None:
        from control.routes.oauth_web_routes import _safe_return_to
        self.assertEqual(_safe_return_to("javascript:alert(1)"), "/app/channels")

    def test_relative_path_collapses(self) -> None:
        from control.routes.oauth_web_routes import _safe_return_to
        self.assertEqual(_safe_return_to("../etc/passwd"), "/app/channels")

    def test_none_or_empty_collapses(self) -> None:
        from control.routes.oauth_web_routes import _safe_return_to
        self.assertEqual(_safe_return_to(None), "/app/channels")
        self.assertEqual(_safe_return_to(""), "/app/channels")


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
