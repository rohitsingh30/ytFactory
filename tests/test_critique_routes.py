"""Tests for control/routes/critique_routes.py (2026-05-11).

Two endpoints:

- POST /api/jobs/<job_id>/critique/start
- POST /api/jobs/<job_id>/critique/token

Both rely on Firestore + firebase-admin in production. We mock both
so the test suite stays fully offline.
"""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from control.routes import critique_routes


def _build_app(*, fake_user_email: str | None = "rohit@example.com") -> FastAPI:
    """Build a minimal FastAPI app that mounts the critique router and
    populates ``request.state.user_email`` the way the production
    OAuth middleware would."""
    app = FastAPI()

    @app.middleware("http")
    async def _stub_auth(request: Request, call_next):
        if fake_user_email is not None:
            request.state.user_email = fake_user_email
        return await call_next(request)

    app.include_router(critique_routes.router)
    return app


# ---------------------------------------------------------------------------
# /critique/start
# ---------------------------------------------------------------------------


class CritiqueStartTests(unittest.TestCase):
    def setUp(self):
        self.app = _build_app()
        self.client = TestClient(self.app)
        self.fake_collection = mock.MagicMock()
        # `existing` query → empty result by default → fresh mint path.
        self.fake_collection.where.return_value.where.return_value.where.return_value.limit.return_value.get.return_value = []

    def _patch(self, *, job_doc=None):
        return [
            mock.patch.object(critique_routes.jobs_mod, "get_job",
                              return_value=(job_doc if job_doc is not None
                                            else {"channel": "mystoriesanimated",
                                                  "short_uri": "gs://b/jobs/j1/short.mp4"})),
            mock.patch.object(critique_routes, "_critiques_collection",
                              return_value=self.fake_collection),
            mock.patch.object(critique_routes, "_server_now",
                              return_value="<srv-ts>"),
        ]

    def test_creates_doc_when_no_existing_critique(self):
        with self._patch()[0], self._patch()[1], self._patch()[2]:
            r = self.client.post("/api/jobs/j1/critique/start", json={"agent": "claude"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["job_id"], "j1")
        self.assertEqual(body["agent"], "claude")
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["mp4_uri"], "gs://b/jobs/j1/short.mp4")
        self.assertGreater(len(body["critique_id"]), 16)

        # Verify the doc payload sent to Firestore.
        set_call = self.fake_collection.document.return_value.set
        set_call.assert_called_once()
        doc = set_call.call_args.args[0]
        self.assertEqual(doc["job_id"], "j1")
        self.assertEqual(doc["channel"], "mystoriesanimated")
        self.assertEqual(doc["agent"], "claude")
        self.assertEqual(doc["status"], "queued")
        self.assertEqual(doc["created_by"], "rohit@example.com")
        self.assertTrue(doc["created_by_uid"].startswith("yt_"))
        self.assertEqual(doc["mp4_uri"], "gs://b/jobs/j1/short.mp4")

    def test_returns_existing_when_live_critique_present(self):
        # Existing live (queued/in_progress) critique → endpoint must
        # NOT mint a new id; reuse the existing one so two browser tabs
        # share the same conversation.
        existing_snap = mock.MagicMock()
        existing_snap.id = "existing-id-123"
        existing_snap.to_dict.return_value = {
            "agent": "copilot",
            "status": "in_progress",
            "mp4_uri": "gs://b/jobs/j1/short.mp4",
        }
        self.fake_collection.where.return_value.where.return_value.where.return_value.limit.return_value.get.return_value = [existing_snap]

        with self._patch()[0], self._patch()[1], self._patch()[2]:
            r = self.client.post("/api/jobs/j1/critique/start", json={"agent": "claude"})

        body = r.json()
        self.assertFalse(body["created"])
        self.assertEqual(body["critique_id"], "existing-id-123")
        # Agent reflects the EXISTING doc, not what the user just asked for.
        self.assertEqual(body["agent"], "copilot")
        self.assertEqual(body["status"], "in_progress")
        # No fresh document.set() call.
        self.fake_collection.document.return_value.set.assert_not_called()

    def test_rejects_unknown_agent(self):
        with self._patch()[0], self._patch()[1], self._patch()[2]:
            r = self.client.post("/api/jobs/j1/critique/start", json={"agent": "rm-rf"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("agent", r.json()["detail"])

    def test_404_when_job_missing(self):
        with mock.patch.object(critique_routes.jobs_mod, "get_job", return_value=None):
            r = self.client.post("/api/jobs/missing/critique/start", json={"agent": "claude"})
        self.assertEqual(r.status_code, 404)

    def test_401_when_no_session_and_no_bearer(self):
        # Build a fresh app where the stub middleware does NOT set
        # request.state.user_email AND the request carries no Bearer
        # header — only true anonymous calls return 401. M2M bearer
        # callers and the cloud runtime (K_SERVICE) get a sentinel
        # actor instead so ops scripts can drive the endpoint without
        # a Google OAuth session.
        app = _build_app(fake_user_email=None)
        client = TestClient(app)
        with mock.patch.object(critique_routes.jobs_mod, "get_job",
                               return_value={"channel": "x"}):
            r = client.post("/api/jobs/j1/critique/start", json={"agent": "claude"})
        self.assertEqual(r.status_code, 401)

    def test_accepts_bearer_token_with_sentinel_owner(self):
        # M2M / smoke-test path: no session cookie, but the request
        # carries a Bearer token (which the production middleware
        # would have already validated). The endpoint accepts and
        # mints the doc with `agent@m2m.ytfactory` as created_by so
        # Firestore rules + ownership queries still see a real uid.
        app = _build_app(fake_user_email=None)
        client = TestClient(app)
        with self._patch()[0], self._patch()[1], self._patch()[2]:
            r = client.post(
                "/api/jobs/j1/critique/start",
                json={"agent": "claude"},
                headers={"Authorization": "Bearer ops-token-xyz"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["created"])
        # The committed doc carries the sentinel email + uid.
        set_call = self.fake_collection.document.return_value.set
        doc = set_call.call_args.args[0]
        self.assertEqual(doc["created_by"], "agent@m2m.ytfactory")


# ---------------------------------------------------------------------------
# /critique/token
# ---------------------------------------------------------------------------


class CritiqueTokenTests(unittest.TestCase):
    def setUp(self):
        self.app = _build_app()
        self.client = TestClient(self.app)

    def test_mints_custom_token(self):
        # Fake the firebase_admin module so we don't need it on the
        # PYTHONPATH. ``from firebase_admin import auth`` resolves
        # ``auth`` as an attribute on the parent module, so we have
        # to attach the sub-mocks AS attributes (not just inject
        # `firebase_admin.auth` into sys.modules).
        fake_auth = mock.MagicMock()
        fake_auth.create_custom_token.return_value = b"fake.custom.token"
        fake_credentials = mock.MagicMock()
        fake_admin = mock.MagicMock()
        fake_admin._apps = {}
        fake_admin.auth = fake_auth
        fake_admin.credentials = fake_credentials

        with mock.patch.dict("sys.modules", {
            "firebase_admin": fake_admin,
            "firebase_admin.auth": fake_auth,
            "firebase_admin.credentials": fake_credentials,
        }):
            r = self.client.post("/api/jobs/j1/critique/token")

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["token"], "fake.custom.token")
        self.assertTrue(body["uid"].startswith("yt_"))
        self.assertGreater(body["expires_in_s"], 0)

        # Verify the token call carried the email + job_id claims so
        # downstream Firestore rules can audit who minted what for which job.
        kwargs_or_args = fake_auth.create_custom_token.call_args
        # Signature: create_custom_token(uid, claims_dict)
        claims = kwargs_or_args.args[1]
        self.assertEqual(claims["email"], "rohit@example.com")
        self.assertEqual(claims["job_id"], "j1")
        self.assertEqual(claims["yt_actor"], "critique_chat")

    def test_503_when_firebase_admin_missing(self):
        # ImportError path — ensure the endpoint surfaces a clear ops msg
        # rather than crashing with a 500 traceback.
        with mock.patch.dict("sys.modules", {
            "firebase_admin": None,  # forces ImportError on `import firebase_admin`
        }):
            r = self.client.post("/api/jobs/j1/critique/token")
        self.assertEqual(r.status_code, 503)
        self.assertIn("firebase-admin", r.json()["detail"])

    def test_503_when_signblob_unavailable(self):
        # The signBlob delegation (Cloud Run path) needs
        # roles/iam.serviceAccountTokenCreator on the runtime SA.
        # When that grant is missing, surface a clear remediation
        # message instead of the SDK's terse "permission denied".
        fake_auth = mock.MagicMock()
        fake_auth.create_custom_token.side_effect = RuntimeError(
            "permission denied on iam.serviceAccounts.signBlob"
        )
        fake_credentials = mock.MagicMock()
        fake_admin = mock.MagicMock()
        fake_admin._apps = {}
        fake_admin.auth = fake_auth
        fake_admin.credentials = fake_credentials

        with mock.patch.dict("sys.modules", {
            "firebase_admin": fake_admin,
            "firebase_admin.auth": fake_auth,
            "firebase_admin.credentials": fake_credentials,
        }):
            r = self.client.post("/api/jobs/j1/critique/token")
        self.assertEqual(r.status_code, 503)
        self.assertIn("serviceAccountTokenCreator", r.json()["detail"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class EmailToUidTests(unittest.TestCase):
    def test_deterministic(self):
        a = critique_routes._email_to_uid("rohit@example.com")
        b = critique_routes._email_to_uid("rohit@example.com")
        self.assertEqual(a, b)

    def test_different_emails_different_uids(self):
        a = critique_routes._email_to_uid("rohit@example.com")
        b = critique_routes._email_to_uid("alice@example.com")
        self.assertNotEqual(a, b)

    def test_uid_starts_with_yt_prefix(self):
        # Firebase uids ≤ 128 chars; SHA-256 hex truncated to 48 +
        # "yt_" prefix = 51 chars. Must keep this stable across
        # versions or every existing critique loses its owner.
        uid = critique_routes._email_to_uid("rohit@example.com")
        self.assertTrue(uid.startswith("yt_"))
        self.assertEqual(len(uid), 3 + 48)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
