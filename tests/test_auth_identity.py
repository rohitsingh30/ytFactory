"""Tests for pipeline.auth.identity and pipeline.auth.__init__.

100% line + branch coverage target.

Mock strategy:
  - Firestore: patch `pipeline.auth.identity._firestore` directly so the
    lazy-import inside the function is bypassed cleanly.
  - httpx: patch `httpx.post` for OAuth token exchange.
  - env vars: patch.dict(os.environ, ...) for all config getters.
  - HMAC / sign_session / verify_session: use real implementation (pure Python).
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import json
import os
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jwt(claims: dict) -> str:
    """Build a fake 3-segment JWT with the given claims payload."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


_FAKE_CLIENT_CFG = {
    "web": {
        "client_id": "test-client-id",
        "client_secret": "test-secret",
        "redirect_uris": ["https://example.com/callback"],
    }
}
_FAKE_CLIENT_JSON = json.dumps(_FAKE_CLIENT_CFG)
_TEST_SECRET = "test-session-secret-32-bytes-xyz"


def _env_with_client(**extra):
    return {"YTFACTORY_WEB_OAUTH_CLIENT": _FAKE_CLIENT_JSON, **extra}


def _env_with_secret(**extra):
    return {"YTFACTORY_SESSION_SECRET": _TEST_SECRET, **extra}


# ---------------------------------------------------------------------------
# pipeline.auth.__init__ re-export
# ---------------------------------------------------------------------------

class TestAuthPackageReexport(unittest.TestCase):
    def test_all_public_symbols_importable(self):
        import pipeline.auth as auth
        for sym in (
            "USER_STATUS_APPROVED", "USER_STATUS_DENIED", "USER_STATUS_PENDING",
            "approve", "deny", "exchange_code", "get_user", "is_admin_email",
            "list_pending", "list_users", "oauth_url", "sign_session",
            "upsert_user", "verify_session",
        ):
            with self.subTest(sym=sym):
                self.assertTrue(hasattr(auth, sym))


# ---------------------------------------------------------------------------
# _oauth_client
# ---------------------------------------------------------------------------

class TestOauthClient(unittest.TestCase):
    def test_from_env_json_web_key(self):
        from pipeline.auth.identity import _oauth_client
        with patch.dict(os.environ, {"YTFACTORY_WEB_OAUTH_CLIENT": _FAKE_CLIENT_JSON}):
            c = _oauth_client()
        self.assertEqual(c["client_id"], "test-client-id")
        self.assertEqual(c["redirect_uris"], ["https://example.com/callback"])

    def test_from_env_json_installed_key(self):
        from pipeline.auth.identity import _oauth_client
        cfg = json.dumps({"installed": _FAKE_CLIENT_CFG["web"]})
        with patch.dict(os.environ, {"YTFACTORY_WEB_OAUTH_CLIENT": cfg}):
            c = _oauth_client()
        self.assertEqual(c["client_id"], "test-client-id")

    def test_from_env_json_flat(self):
        from pipeline.auth.identity import _oauth_client
        cfg = json.dumps(_FAKE_CLIENT_CFG["web"])
        with patch.dict(os.environ, {"YTFACTORY_WEB_OAUTH_CLIENT": cfg}):
            c = _oauth_client()
        self.assertEqual(c["client_id"], "test-client-id")

    def test_from_path_env(self):
        from pipeline.auth.identity import _oauth_client
        env = {k: v for k, v in os.environ.items()
               if k != "YTFACTORY_WEB_OAUTH_CLIENT"}
        env["YTFACTORY_WEB_OAUTH_CLIENT_PATH"] = "/fake/path.json"
        with patch.dict(os.environ, env, clear=True), \
             patch("builtins.open", unittest.mock.mock_open()), \
             patch("json.load", return_value=_FAKE_CLIENT_CFG):
            c = _oauth_client()
        self.assertEqual(c["client_id"], "test-client-id")

    def test_from_default_path(self):
        from pipeline.auth.identity import _oauth_client
        env = {k: v for k, v in os.environ.items()
               if k not in ("YTFACTORY_WEB_OAUTH_CLIENT",
                             "YTFACTORY_WEB_OAUTH_CLIENT_PATH")}
        with patch.dict(os.environ, env, clear=True), \
             patch("builtins.open", unittest.mock.mock_open()), \
             patch("json.load", return_value=_FAKE_CLIENT_CFG):
            c = _oauth_client()
        self.assertEqual(c["client_id"], "test-client-id")

    def test_missing_client_id_raises(self):
        from pipeline.auth.identity import _oauth_client
        bad = json.dumps({"web": {"client_secret": "s"}})
        with patch.dict(os.environ, {"YTFACTORY_WEB_OAUTH_CLIENT": bad}):
            with self.assertRaises(RuntimeError):
                _oauth_client()

    def test_missing_client_secret_raises(self):
        from pipeline.auth.identity import _oauth_client
        bad = json.dumps({"web": {"client_id": "id"}})
        with patch.dict(os.environ, {"YTFACTORY_WEB_OAUTH_CLIENT": bad}):
            with self.assertRaises(RuntimeError):
                _oauth_client()


# ---------------------------------------------------------------------------
# Config getters
# ---------------------------------------------------------------------------

class TestConfigGetters(unittest.TestCase):
    def test_redirect_uri_from_env(self):
        from pipeline.auth.identity import _redirect_uri
        with patch.dict(os.environ, {"YTFACTORY_AUTH_REDIRECT_URI": "https://x.com/cb"}):
            self.assertEqual(_redirect_uri(), "https://x.com/cb")

    def test_redirect_uri_default(self):
        from pipeline.auth.identity import _redirect_uri, _DEFAULT_REDIRECT_URI
        env = {k: v for k, v in os.environ.items()
               if k != "YTFACTORY_AUTH_REDIRECT_URI"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_redirect_uri(), _DEFAULT_REDIRECT_URI)

    def test_session_secret_from_env(self):
        from pipeline.auth.identity import _session_secret
        with patch.dict(os.environ, {"YTFACTORY_SESSION_SECRET": "abc"}):
            self.assertEqual(_session_secret(), b"abc")

    def test_session_secret_fallback(self):
        from pipeline.auth.identity import _session_secret, _RUNTIME_SECRET
        env = {k: v for k, v in os.environ.items()
               if k != "YTFACTORY_SESSION_SECRET"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_session_secret(), _RUNTIME_SECRET.encode())

    def test_ttl_s_from_env(self):
        from pipeline.auth.identity import _ttl_s
        with patch.dict(os.environ, {"YTFACTORY_SESSION_TTL_S": "3600"}):
            self.assertEqual(_ttl_s(), 3600)

    def test_ttl_s_default(self):
        from pipeline.auth.identity import _ttl_s, _DEFAULT_TTL_S
        env = {k: v for k, v in os.environ.items()
               if k != "YTFACTORY_SESSION_TTL_S"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_ttl_s(), _DEFAULT_TTL_S)

    def test_ttl_s_invalid_returns_default(self):
        from pipeline.auth.identity import _ttl_s, _DEFAULT_TTL_S
        with patch.dict(os.environ, {"YTFACTORY_SESSION_TTL_S": "not-a-number"}):
            self.assertEqual(_ttl_s(), _DEFAULT_TTL_S)

    def test_admin_domains_from_env(self):
        from pipeline.auth.identity import _admin_domains
        with patch.dict(os.environ, {"YTFACTORY_ADMIN_DOMAINS": "foo.com, Bar.com "}):
            self.assertIn("foo.com", _admin_domains())
            self.assertIn("bar.com", _admin_domains())

    def test_admin_domains_default(self):
        from pipeline.auth.identity import _admin_domains, _DEFAULT_ADMIN_DOMAINS
        env = {k: v for k, v in os.environ.items()
               if k != "YTFACTORY_ADMIN_DOMAINS"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_admin_domains(), _DEFAULT_ADMIN_DOMAINS)

    def test_admin_domains_blank_returns_default(self):
        from pipeline.auth.identity import _admin_domains, _DEFAULT_ADMIN_DOMAINS
        with patch.dict(os.environ, {"YTFACTORY_ADMIN_DOMAINS": "   "}):
            self.assertEqual(_admin_domains(), _DEFAULT_ADMIN_DOMAINS)


# ---------------------------------------------------------------------------
# is_admin_email
# ---------------------------------------------------------------------------

class TestIsAdminEmail(unittest.TestCase):
    def test_admin_domain(self):
        from pipeline.auth.identity import is_admin_email
        with patch.dict(os.environ, {"YTFACTORY_ADMIN_DOMAINS": "docx.co.in"}):
            self.assertTrue(is_admin_email("user@docx.co.in"))

    def test_non_admin_domain(self):
        from pipeline.auth.identity import is_admin_email
        with patch.dict(os.environ, {"YTFACTORY_ADMIN_DOMAINS": "docx.co.in"}):
            self.assertFalse(is_admin_email("user@gmail.com"))

    def test_no_at_returns_false(self):
        from pipeline.auth.identity import is_admin_email
        self.assertFalse(is_admin_email("notanemail"))

    def test_explicit_email_allowlist(self):
        from pipeline.auth.identity import is_admin_email
        with patch.dict(os.environ, {
            "YTFACTORY_ADMIN_DOMAINS": "docx.co.in",
            "YTFACTORY_ADMIN_EMAILS": "sanimated219@gmail.com, OTHER@gmail.com",
        }):
            self.assertTrue(is_admin_email("sanimated219@gmail.com"))
            self.assertTrue(is_admin_email("other@gmail.com"))
            self.assertFalse(is_admin_email("randomuser@gmail.com"))
            self.assertTrue(is_admin_email("user@docx.co.in"))

    def test_explicit_email_case_insensitive(self):
        from pipeline.auth.identity import is_admin_email
        with patch.dict(os.environ, {
            "YTFACTORY_ADMIN_DOMAINS": "docx.co.in",
            "YTFACTORY_ADMIN_EMAILS": "Sanimated219@Gmail.com",
        }):
            self.assertTrue(is_admin_email("sanimated219@gmail.com"))
            self.assertTrue(is_admin_email("SANIMATED219@GMAIL.COM"))

    def test_explicit_email_empty_does_not_widen_domains(self):
        """Empty YTFACTORY_ADMIN_EMAILS must not auto-admin all gmail users."""
        from pipeline.auth.identity import is_admin_email
        with patch.dict(os.environ, {
            "YTFACTORY_ADMIN_DOMAINS": "docx.co.in",
            "YTFACTORY_ADMIN_EMAILS": "",
        }):
            self.assertFalse(is_admin_email("anyone@gmail.com"))


# ---------------------------------------------------------------------------
# oauth_url
# ---------------------------------------------------------------------------

class TestOauthUrl(unittest.TestCase):
    def test_url_contains_expected_params(self):
        from pipeline.auth.identity import oauth_url
        with patch.dict(os.environ, _env_with_client()):
            url = oauth_url("csrf-nonce-abc")
        self.assertIn("accounts.google.com", url)
        self.assertIn("test-client-id", url)
        self.assertIn("csrf-nonce-abc", url)
        self.assertIn("openid", url)
        self.assertIn("email", url)


# ---------------------------------------------------------------------------
# _decode_id_token
# ---------------------------------------------------------------------------

class TestDecodeIdToken(unittest.TestCase):
    def test_valid_token(self):
        from pipeline.auth.identity import _decode_id_token
        claims = {"email": "user@example.com", "email_verified": True}
        result = _decode_id_token(_jwt(claims))
        self.assertEqual(result["email"], "user@example.com")

    def test_malformed_token_raises(self):
        from pipeline.auth.identity import _decode_id_token
        with self.assertRaises(RuntimeError) as ctx:
            _decode_id_token("only.two")
        self.assertIn("Malformed", str(ctx.exception))


# ---------------------------------------------------------------------------
# exchange_code
# ---------------------------------------------------------------------------

class TestExchangeCode(unittest.TestCase):
    """Audit S1.1 — _decode_id_token now uses
    google.oauth2.id_token.verify_oauth2_token to crypto-verify the
    JWT signature against Google's JWKS. Tests mock the verify call
    so they don't need a live Google-signed token; the verify mock
    returns the claims dict directly (matching the real lib's
    contract on success)."""

    def _mock_verify(self, claims: dict) -> Any:
        """Return a context manager that mocks the
        google.oauth2.id_token.verify_oauth2_token call to return
        ``claims``. Use as ``with self._mock_verify({...}) as m:``."""
        return patch(
            "google.oauth2.id_token.verify_oauth2_token",
            return_value=claims,
        )

    def test_success(self):
        from pipeline.auth.identity import exchange_code
        claims = {
            "email": "  User@Example.COM  ",
            "email_verified": True,
            "name": "Test User",
            "picture": "https://example.com/pic.jpg",
        }
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id_token": _jwt(claims)}
        with patch.dict(os.environ, _env_with_client()), \
             patch("httpx.post", return_value=resp), \
             self._mock_verify(claims):
            result = exchange_code("fake-code")
        self.assertEqual(result["email"], "user@example.com")
        self.assertEqual(result["name"], "Test User")

    def test_http_error_raises(self):
        from pipeline.auth.identity import exchange_code
        resp = MagicMock()
        resp.status_code = 400
        resp.text = "Bad Request"
        with patch.dict(os.environ, _env_with_client()), \
             patch("httpx.post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                exchange_code("bad-code")
        self.assertIn("400", str(ctx.exception))

    def test_missing_id_token_raises(self):
        from pipeline.auth.identity import exchange_code
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {}
        with patch.dict(os.environ, _env_with_client()), \
             patch("httpx.post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                exchange_code("code")
        self.assertIn("id_token", str(ctx.exception))

    def test_missing_email_claim_raises(self):
        from pipeline.auth.identity import exchange_code
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id_token": _jwt({"email_verified": True})}
        with patch.dict(os.environ, _env_with_client()), \
             patch("httpx.post", return_value=resp), \
             self._mock_verify({"email_verified": True}):
            with self.assertRaises(RuntimeError) as ctx:
                exchange_code("code")
        self.assertIn("email", str(ctx.exception))

    def test_unverified_email_raises(self):
        from pipeline.auth.identity import exchange_code
        claims = {"email": "u@x.com", "email_verified": False}
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id_token": _jwt(claims)}
        with patch.dict(os.environ, _env_with_client()), \
             patch("httpx.post", return_value=resp), \
             self._mock_verify(claims):
            with self.assertRaises(RuntimeError) as ctx:
                exchange_code("code")
        self.assertIn("NOT verified", str(ctx.exception))

    def test_invalid_signature_raises_runtime_error(self):
        # Audit S1.1 — pre-fix, a tampered id_token would silently
        # decode and the unverified claims would be trusted. Now the
        # google-auth lib's signature failure must surface as a
        # RuntimeError so the OAuth flow rejects it.
        from pipeline.auth.identity import exchange_code
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id_token": _jwt({"email": "u@x.com"})}
        with patch.dict(os.environ, _env_with_client()), \
             patch("httpx.post", return_value=resp), \
             patch(
                "google.oauth2.id_token.verify_oauth2_token",
                side_effect=ValueError("Could not verify token signature."),
             ):
            with self.assertRaises(RuntimeError) as ctx:
                exchange_code("code")
        self.assertIn("verification failed", str(ctx.exception))

    def test_wrong_audience_raises_runtime_error(self):
        # Audit S1.1 — token minted for a different OAuth client must
        # be rejected. google-auth raises ValueError("Token has wrong
        # audience ...").
        from pipeline.auth.identity import exchange_code
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id_token": _jwt({"email": "u@x.com"})}
        with patch.dict(os.environ, _env_with_client()), \
             patch("httpx.post", return_value=resp), \
             patch(
                "google.oauth2.id_token.verify_oauth2_token",
                side_effect=ValueError(
                    "Token has wrong audience attacker-client",
                ),
             ):
            with self.assertRaises(RuntimeError) as ctx:
                exchange_code("code")
        self.assertIn("verification failed", str(ctx.exception))


# ---------------------------------------------------------------------------
# sign_session / verify_session
# ---------------------------------------------------------------------------

class TestSessionCookie(unittest.TestCase):
    def test_roundtrip(self):
        from pipeline.auth.identity import sign_session, verify_session
        with patch.dict(os.environ, _env_with_secret()):
            cookie = sign_session("user@example.com", now_s=int(time.time()))
            result = verify_session(cookie)
        self.assertEqual(result, "user@example.com")

    def test_sign_without_explicit_now(self):
        from pipeline.auth.identity import sign_session, verify_session
        with patch.dict(os.environ, _env_with_secret()):
            cookie = sign_session("user@example.com")
            result = verify_session(cookie)
        self.assertEqual(result, "user@example.com")

    def test_expired_returns_none(self):
        from pipeline.auth.identity import sign_session, verify_session
        past = int(time.time()) - 999 * 24 * 3600
        with patch.dict(os.environ, _env_with_secret()):
            cookie = sign_session("user@example.com", now_s=past)
            result = verify_session(cookie)
        self.assertIsNone(result)

    def test_tampered_sig_returns_none(self):
        from pipeline.auth.identity import sign_session, verify_session
        with patch.dict(os.environ, _env_with_secret()):
            cookie = sign_session("user@example.com", now_s=int(time.time()))
        tampered = cookie[:-3] + "XXX"
        with patch.dict(os.environ, _env_with_secret()):
            self.assertIsNone(verify_session(tampered))

    def test_none_returns_none(self):
        from pipeline.auth.identity import verify_session
        self.assertIsNone(verify_session(None))

    def test_empty_string_returns_none(self):
        from pipeline.auth.identity import verify_session
        self.assertIsNone(verify_session(""))

    def test_no_pipe_returns_none(self):
        from pipeline.auth.identity import verify_session
        self.assertIsNone(verify_session("nopipes"))

    def test_only_one_pipe_returns_none(self):
        from pipeline.auth.identity import verify_session
        # rsplit("|", 2) can't unpack 3 parts from "a|b" → ValueError → None
        self.assertIsNone(verify_session("user|1234567890"))

    def test_non_integer_issued_returns_none(self):
        # Build a cookie with a valid HMAC but non-integer issued_s so the
        # except ValueError branch at identity.py:253-254 is reached.
        import hashlib as _hl
        import hmac as _hmac
        import base64 as _b64
        from pipeline.auth import identity
        env = _env_with_secret()
        secret = env["YTFACTORY_SESSION_SECRET"].encode()
        payload = "user@example.com|notanint"
        sig = _hmac.new(secret, payload.encode(), _hl.sha256).digest()
        sig_b = _b64.urlsafe_b64encode(sig).decode().rstrip("=")
        bad_cookie = f"{payload}|{sig_b}"
        with patch.dict(os.environ, env):
            self.assertIsNone(identity.verify_session(bad_cookie))

    def test_empty_email_returns_none(self):
        from pipeline.auth.identity import sign_session, verify_session
        with patch.dict(os.environ, _env_with_secret()):
            cookie = sign_session("", now_s=int(time.time()))
            result = verify_session(cookie)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Firestore helpers — patch _firestore directly
# ---------------------------------------------------------------------------

def _make_db():
    """Return a (db_mock, col_mock, doc_ref_mock, snap_mock) quadruple."""
    snap = MagicMock()
    ref = MagicMock()
    ref.get.return_value = snap
    col = MagicMock()
    col.document.return_value = ref
    db = MagicMock()
    db.collection.return_value = col
    return db, col, ref, snap


class TestGetUser(unittest.TestCase):
    def setUp(self):
        # Audit Q2.34 — bust the get_user cache between tests so each
        # test sees fresh Firestore reads.
        from pipeline.auth import identity
        identity.invalidate_user_cache()

    def test_user_found(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = True
        snap.to_dict.return_value = {"status": "approved", "name": "Alice"}

        with patch.object(identity, "_firestore", return_value=db):
            result = identity.get_user("Alice@Example.COM")

        self.assertEqual(result["email"], "alice@example.com")
        self.assertEqual(result["status"], "approved")

    def test_user_not_found(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = False

        with patch.object(identity, "_firestore", return_value=db):
            result = identity.get_user("nobody@example.com")

        self.assertIsNone(result)


class TestGetUserTtlCache(unittest.TestCase):
    """Audit Q2.34 — pre-fix every authenticated request hit Firestore.
    Now ``get_user`` is TTL-cached per email; mutators call
    ``invalidate_user_cache(email)`` to bust the entry on demand.
    """

    def setUp(self):
        from pipeline.auth import identity
        identity.invalidate_user_cache()

    def test_second_lookup_within_ttl_uses_cache(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = True
        snap.to_dict.return_value = {"status": "approved"}

        with patch.object(identity, "_firestore", return_value=db) as m_fs:
            identity.get_user("a@x.com")
            identity.get_user("a@x.com")
            identity.get_user("a@x.com")
        # Three calls, but only one Firestore client construction.
        self.assertEqual(m_fs.call_count, 1)

    def test_invalidate_user_cache_busts_one_entry(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = True
        snap.to_dict.return_value = {"status": "approved"}

        with patch.object(identity, "_firestore", return_value=db) as m_fs:
            identity.get_user("a@x.com")
            identity.invalidate_user_cache("a@x.com")
            identity.get_user("a@x.com")
        # Two Firestore client constructions because the cache was busted.
        self.assertEqual(m_fs.call_count, 2)

    def test_invalidate_user_cache_clear_all_busts_every_entry(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = True
        snap.to_dict.return_value = {"status": "approved"}

        with patch.object(identity, "_firestore", return_value=db) as m_fs:
            identity.get_user("a@x.com")
            identity.get_user("b@x.com")
            identity.invalidate_user_cache()  # no email → clear all
            identity.get_user("a@x.com")
        # 2 from initial fills + 1 after clear = 3 Firestore reads.
        self.assertEqual(m_fs.call_count, 3)

    def test_ttl_zero_disables_cache(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = True
        snap.to_dict.return_value = {"status": "approved"}

        with patch.object(identity, "_firestore", return_value=db) as m_fs, \
             patch.dict(os.environ, {"YTFACTORY_USER_CACHE_TTL_S": "0"}):
            identity.get_user("a@x.com")
            identity.get_user("a@x.com")
        # TTL 0 disables caching → both lookups hit Firestore.
        self.assertEqual(m_fs.call_count, 2)

    def test_invalid_ttl_env_falls_back_to_default(self):
        from pipeline.auth import identity
        with patch.dict(os.environ, {"YTFACTORY_USER_CACHE_TTL_S": "not-a-number"}):
            self.assertEqual(identity._user_cache_ttl_s(),
                             identity._USER_CACHE_TTL_DEFAULT_S)

    def test_set_status_invalidates_cache(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = True
        snap.to_dict.return_value = {"status": "pending"}

        with patch.object(identity, "_firestore", return_value=db):
            identity.get_user("a@x.com")
            # _set_status must bust the cache; verify by calling get_user
            # again and confirming a fresh Firestore read happened.
            identity._set_status("a@x.com", identity.USER_STATUS_APPROVED, by="op")
        # The cache should now be empty for this email.
        with identity._USER_CACHE_LOCK:
            self.assertNotIn("a@x.com", identity._USER_CACHE)


class TestUpsertUser(unittest.TestCase):
    def test_new_admin_user(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = False

        with patch.object(identity, "_firestore", return_value=db), \
             patch.dict(os.environ, {"YTFACTORY_ADMIN_DOMAINS": "docx.co.in"}):
            result = identity.upsert_user("Admin@docx.co.in", name="Admin")

        self.assertEqual(result["status"], identity.USER_STATUS_APPROVED)
        self.assertTrue(result["is_admin"])
        self.assertEqual(result["approved_by"], "system:admin_domain")
        ref.set.assert_called_once()

    def test_new_non_admin_user(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = False

        with patch.object(identity, "_firestore", return_value=db), \
             patch.dict(os.environ, {"YTFACTORY_ADMIN_DOMAINS": "docx.co.in"}):
            result = identity.upsert_user("user@gmail.com", name="User")

        self.assertEqual(result["status"], identity.USER_STATUS_PENDING)
        self.assertFalse(result["is_admin"])
        self.assertIsNone(result["approved_at"])

    def test_existing_user_backfills_name_and_picture(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = True
        snap.to_dict.return_value = {"status": "approved", "name": "", "picture": ""}

        with patch.object(identity, "_firestore", return_value=db):
            result = identity.upsert_user("user@example.com", name="Bob",
                                          picture="http://pic")

        ref.update.assert_called_once()
        update_arg = ref.update.call_args[0][0]
        self.assertIn("name", update_arg)
        self.assertIn("picture", update_arg)
        self.assertEqual(result["email"], "user@example.com")

    def test_existing_user_does_not_overwrite_existing_name(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = True
        snap.to_dict.return_value = {
            "status": "approved",
            "name": "Existing Name",
            "picture": "http://existing-pic",
        }

        with patch.object(identity, "_firestore", return_value=db):
            identity.upsert_user("user@example.com", name="New Name",
                                 picture="http://new-pic")

        update_arg = ref.update.call_args[0][0]
        self.assertNotIn("name", update_arg)
        self.assertNotIn("picture", update_arg)


class TestSetStatus(unittest.TestCase):
    def test_invalid_status_raises(self):
        from pipeline.auth.identity import _set_status
        db, col, ref, snap = _make_db()
        with patch("pipeline.auth.identity._firestore", return_value=db):
            with self.assertRaises(ValueError):
                _set_status("user@example.com", "invalid", by="admin")

    def test_user_not_found_raises(self):
        from pipeline.auth.identity import _set_status, USER_STATUS_APPROVED
        db, col, ref, snap = _make_db()
        snap.exists = False

        with patch("pipeline.auth.identity._firestore", return_value=db):
            with self.assertRaises(LookupError):
                _set_status("user@example.com", USER_STATUS_APPROVED, by="admin")

    def test_approve(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = True
        snap.to_dict.return_value = {"status": "pending"}

        with patch.object(identity, "_firestore", return_value=db):
            result = identity.approve("user@example.com", by="admin@x.com")

        update_arg = ref.update.call_args[0][0]
        self.assertEqual(update_arg["status"], identity.USER_STATUS_APPROVED)
        self.assertEqual(update_arg["approved_by"], "admin@x.com")
        self.assertIsNotNone(update_arg["approved_at"])

    def test_deny(self):
        from pipeline.auth import identity
        db, col, ref, snap = _make_db()
        snap.exists = True
        snap.to_dict.return_value = {"status": "pending"}

        with patch.object(identity, "_firestore", return_value=db):
            result = identity.deny("user@example.com", by="admin@x.com")

        update_arg = ref.update.call_args[0][0]
        self.assertEqual(update_arg["status"], identity.USER_STATUS_DENIED)
        self.assertIsNone(update_arg["approved_at"])
        self.assertIsNone(update_arg["approved_by"])


class TestListUsers(unittest.TestCase):
    def _snap(self, email, data):
        s = MagicMock()
        s.id = email
        s.to_dict.return_value = data
        return s

    def test_list_all_sorted_newest_first(self):
        from pipeline.auth import identity
        db, col, ref, _ = _make_db()
        snaps = [
            self._snap("a@x.com", {"status": "approved",
                                    "requested_at": "2024-01-02"}),
            self._snap("b@x.com", {"status": "pending",
                                    "requested_at": "2024-01-01"}),
        ]
        col.stream.return_value = iter(snaps)

        with patch.object(identity, "_firestore", return_value=db):
            result = identity.list_users()

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["email"], "a@x.com")  # newest first

    def test_list_users_filtered_by_status(self):
        from pipeline.auth import identity, USER_STATUS_PENDING

        db, col, ref, _ = _make_db()
        filtered_col = MagicMock()
        col.where.return_value = filtered_col
        s = self._snap("b@x.com", {"status": "pending", "requested_at": "2024-01-01"})
        filtered_col.stream.return_value = iter([s])

        fake_ff = MagicMock()
        fake_ff.FieldFilter.return_value = MagicMock()

        with patch.object(identity, "_firestore", return_value=db), \
             patch.dict("sys.modules",
                        {"google.cloud.firestore_v1.base_query": fake_ff}):
            result = identity.list_users(status=USER_STATUS_PENDING)

        self.assertEqual(len(result), 1)

    def test_list_users_invalid_status_raises(self):
        from pipeline.auth import identity
        db, col, ref, _ = _make_db()
        with patch.object(identity, "_firestore", return_value=db):
            with self.assertRaises(ValueError):
                identity.list_users(status="bogus")

    def test_list_pending_calls_list_users_with_pending(self):
        from pipeline.auth import identity, USER_STATUS_PENDING

        db, col, ref, _ = _make_db()
        filtered_col = MagicMock()
        col.where.return_value = filtered_col
        s = self._snap("p@x.com", {"status": "pending", "requested_at": "2024-01"})
        filtered_col.stream.return_value = iter([s])

        fake_ff = MagicMock()
        fake_ff.FieldFilter.return_value = MagicMock()

        with patch.object(identity, "_firestore", return_value=db), \
             patch.dict("sys.modules",
                        {"google.cloud.firestore_v1.base_query": fake_ff}):
            result = identity.list_pending()

        self.assertEqual(len(result), 1)

    def test_snap_to_dict_none_handled(self):
        """snap.to_dict() returning None must not crash."""
        from pipeline.auth import identity
        db, col, ref, _ = _make_db()
        s = MagicMock()
        s.id = "x@x.com"
        s.to_dict.return_value = None
        col.stream.return_value = iter([s])

        with patch.object(identity, "_firestore", return_value=db):
            result = identity.list_users()

        self.assertEqual(result[0]["email"], "x@x.com")


class TestFieldEq(unittest.TestCase):
    def test_returns_filter_object(self):
        from pipeline.auth.identity import _field_eq
        fake_ff = MagicMock()
        fake_ff.FieldFilter.return_value = "FILTER"
        with patch.dict("sys.modules",
                        {"google.cloud.firestore_v1.base_query": fake_ff}):
            result = _field_eq("status", "approved")
        self.assertEqual(result, "FILTER")


class TestNowIso(unittest.TestCase):
    def test_format(self):
        from pipeline.auth.identity import _now_iso
        import re
        self.assertRegex(_now_iso(), r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


if __name__ == "__main__":
    unittest.main()
