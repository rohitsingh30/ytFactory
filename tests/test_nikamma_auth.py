"""Authentication at the real middleware boundary outside Cloud Run."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

from fastapi import FastAPI, Header, Request
from fastapi.testclient import TestClient
from pipeline.auth import sign_session
from control.routes.state_routes import _require_auth
from web import server


class NikammaAuthTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "YTFACTORY_REQUIRE_AUTH": "1",
            "YTFACTORY_SESSION_SECRET": "test-session-secret",
            "YTFACTORY_AGENT_TOKEN": "test-agent-token",
        }, clear=True)
        self.env.start()
        self.token = patch.object(server, "AGENT_TOKEN", "test-agent-token")
        self.token.start()
        self.legacy = patch.object(server, "AUTH_TOKEN", None)
        self.legacy.start()
        app = FastAPI()
        app.middleware("http")(server.auth_middleware)

        @app.get("/api/state/test")
        @app.get("/api/scheduler/state")
        def operator(request: Request, authorization: str | None = Header(None)):
            _require_auth(authorization)
            return {"email": getattr(request.state, "user_email", None)}

        @app.post("/api/scheduler/tick")
        def tick():
            return {"ok": True}

        self.client = TestClient(app, headers={"Accept": "application/json"})

    def tearDown(self):
        self.client.close()
        self.legacy.stop()
        self.token.stop()
        self.env.stop()

    def test_missing_oauth_config_stays_closed(self):
        self.assertFalse(server._server_is_open())
        self.assertEqual(self.client.get("/api/state/test").status_code, 401)

    def test_approved_session_reaches_operator_routes(self):
        self.client.cookies.set("yt_session", sign_session("operator@example.com"))
        with patch("pipeline.auth.get_user", return_value={"status": "approved", "is_admin": False}):
            for path in ("/api/state/test", "/api/scheduler/state"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"email": "operator@example.com"})
                self.assertNotIn("test-agent-token", response.text)

    def test_pending_or_denied_session_cannot_reach_operator_routes(self):
        self.client.cookies.set("yt_session", sign_session("operator@example.com"))
        for status in ("pending", "denied"):
            with patch("pipeline.auth.get_user", return_value={"status": status}):
                self.assertEqual(self.client.get("/api/state/test").status_code, 403)

    def test_valid_session_does_not_authorize_scheduler_mutation(self):
        self.client.cookies.set("yt_session", sign_session("operator@example.com"))
        with patch("pipeline.auth.get_user", return_value={"status": "approved"}):
            self.assertEqual(self.client.post("/api/scheduler/tick").status_code, 401)

    def test_cloud_run_scheduler_read_keeps_iam_behavior(self):
        with patch.dict(os.environ, {"K_SERVICE": "existing-cloud-service"}):
            self.assertEqual(self.client.get("/api/scheduler/state").status_code, 200)

    def test_agent_bearer_still_works(self):
        response = self.client.get("/api/state/test", headers={"Authorization": "Bearer test-agent-token"})
        self.assertEqual(response.status_code, 200)

    def test_forged_cookie_and_bearer_are_rejected(self):
        self.client.cookies.set("yt_session", "forged")
        response = self.client.get("/api/state/test", headers={"Authorization": "Bearer forged"})
        self.assertEqual(response.status_code, 401)
