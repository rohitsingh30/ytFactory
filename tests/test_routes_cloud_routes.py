"""Tests for control/routes/cloud_routes.py auth helper.

Currently scoped to the audit S1.16 fix — constant-time bearer-token
compare. Other routes in the file should grow tests as their coverage
gaps are addressed; this file is the focused starting point.
"""
from __future__ import annotations

import os
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from control.routes.cloud_routes import router, _require_auth
from fastapi import HTTPException


class TestRequireAuth(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.pop("YTFACTORY_AGENT_TOKEN", None)
        self._saved_k = os.environ.pop("K_SERVICE", None)

    def tearDown(self) -> None:
        os.environ.pop("YTFACTORY_AGENT_TOKEN", None)
        os.environ.pop("K_SERVICE", None)
        if self._saved is not None:
            os.environ["YTFACTORY_AGENT_TOKEN"] = self._saved
        if self._saved_k is not None:
            os.environ["K_SERVICE"] = self._saved_k

    def test_cloud_run_trusts_iam_returns_silently(self) -> None:
        # When K_SERVICE is set, IAM upstream has already validated the
        # OIDC token; the helper must early-return regardless of header.
        os.environ["K_SERVICE"] = "ytfactory-web"
        # No header, no token — must NOT raise.
        _require_auth(None)

    def test_no_token_set_is_dev_friendly(self) -> None:
        # Documented dev posture: no env token → allow (matches the
        # Next.js proxy behaviour in dev). Helper must NOT raise.
        _require_auth(None)
        _require_auth("Bearer something")

    def test_missing_bearer_raises_401(self) -> None:
        os.environ["YTFACTORY_AGENT_TOKEN"] = "secret-token"
        with self.assertRaises(HTTPException) as ctx:
            _require_auth(None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_bearer_raises_403_with_constant_time_compare(self) -> None:
        # Audit S1.16 — must reach the hmac.compare_digest branch and
        # return 403, not silently no-op or 500.
        os.environ["YTFACTORY_AGENT_TOKEN"] = "secret-token"
        with self.assertRaises(HTTPException) as ctx:
            _require_auth("Bearer wrong-token")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_correct_bearer_passes(self) -> None:
        os.environ["YTFACTORY_AGENT_TOKEN"] = "secret-token"
        _require_auth("Bearer secret-token")


if __name__ == "__main__":
    unittest.main()
