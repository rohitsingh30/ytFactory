"""Tests for control/routes/scheduler_routes.py auth helper.

Currently scoped to the audit S1.16 fix — constant-time bearer-token
compare. The router itself is exercised end-to-end via web/server.py
integration tests; this file pins the standalone auth-helper contract.
"""
from __future__ import annotations

import os
import unittest

from fastapi import HTTPException

from control.routes.scheduler_routes import _require_auth


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
        os.environ["K_SERVICE"] = "ytfactory-web"
        _require_auth(None)

    def test_no_token_set_fails_closed_with_503(self) -> None:
        # Different posture from cloud_routes — scheduler MUST refuse
        # to run unauthenticated even in dev.
        with self.assertRaises(HTTPException) as ctx:
            _require_auth(None)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_missing_bearer_raises_401(self) -> None:
        os.environ["YTFACTORY_AGENT_TOKEN"] = "secret-token"
        with self.assertRaises(HTTPException) as ctx:
            _require_auth(None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_bearer_raises_403_with_constant_time_compare(self) -> None:
        # Audit S1.16 — exercises the hmac.compare_digest fail branch.
        os.environ["YTFACTORY_AGENT_TOKEN"] = "secret-token"
        with self.assertRaises(HTTPException) as ctx:
            _require_auth("Bearer wrong-token")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_correct_bearer_passes(self) -> None:
        os.environ["YTFACTORY_AGENT_TOKEN"] = "secret-token"
        _require_auth("Bearer secret-token")


if __name__ == "__main__":
    unittest.main()
