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


class TestPublicSchedulerStateApi(unittest.TestCase):
    """Audit S1.9 — cross-module callers (the FastAPI route) must use
    the public ``read_state`` alias rather than reaching into the
    underscore-prefixed in-module helper. Pinning both:

      1. ``scheduler.read_state()`` exists and delegates to ``_read_state``.
      2. The route source itself does NOT reference ``_read_state``.
    """

    def test_public_alias_exists_and_returns_same_dict(self) -> None:
        from control.core import scheduler as _sched
        from unittest.mock import patch

        sentinel = {"k": "v", "n": 1}
        with patch.object(_sched, "_read_state", return_value=sentinel) as m:
            self.assertEqual(_sched.read_state(), sentinel)
            m.assert_called_once_with()

    def test_route_source_uses_public_alias_not_underscore(self) -> None:
        from pathlib import Path
        src = Path(__file__).resolve().parent.parent / "control" / "routes" / "scheduler_routes.py"
        text = src.read_text()
        # The public alias must be the call site for the route handler.
        self.assertIn("scheduler.read_state()", text)
        # And the route MUST NOT reach into the underscore helper.
        self.assertNotIn("scheduler._read_state(", text)

    def test_route_handler_invokes_public_alias_via_fastapi(self) -> None:
        """Pin the live wiring — the FastAPI handler actually calls
        the public alias, not just the source-string presence above.
        Exercises the changed line directly so coverage gate sees it.
        """
        import asyncio
        import os as _os
        from unittest.mock import patch

        from control.routes import scheduler_routes as _routes

        sentinel = {"last_channel": "historyrecapped", "last_at": "2026-05-12T00:00:00Z"}
        _os.environ["YTFACTORY_AGENT_TOKEN"] = "tkn"
        try:
            with patch.object(_routes.scheduler, "read_state", return_value=sentinel) as m:
                got = asyncio.run(_routes.scheduler_state("Bearer tkn"))
            self.assertEqual(got, sentinel)
            m.assert_called_once_with()
        finally:
            _os.environ.pop("YTFACTORY_AGENT_TOKEN", None)


class TestPublicCrossEngageRegistryApi(unittest.TestCase):
    """Audit S1.9 — same rule for ``cross_engage._load_registry`` /
    ``_save_registry``. ``web/server.py`` must use the public aliases.
    """

    def test_public_load_registry_alias_exists_and_delegates(self) -> None:
        from pipeline.research import cross_engage as _ce
        from unittest.mock import patch

        sentinel = {"a": {"channel_id": "UCabc"}}
        with patch.object(_ce, "_load_registry", return_value=sentinel) as m:
            self.assertEqual(_ce.load_registry(), sentinel)
            m.assert_called_once_with()

    def test_public_save_registry_alias_exists_and_delegates(self) -> None:
        from pipeline.research import cross_engage as _ce
        from unittest.mock import patch

        captured: dict = {}
        with patch.object(_ce, "_save_registry", side_effect=lambda r: captured.setdefault("r", r)):
            _ce.save_registry({"x": {"channel_id": "UCx"}})
        self.assertEqual(captured["r"], {"x": {"channel_id": "UCx"}})

    def test_web_server_source_uses_public_alias_not_underscore(self) -> None:
        from pathlib import Path
        src = Path(__file__).resolve().parent.parent / "web" / "server.py"
        text = src.read_text()
        # web/server.py is the documented offender — pin it explicitly.
        self.assertIn("_ce.load_registry()", text)
        self.assertNotIn("_ce._load_registry(", text)


if __name__ == "__main__":
    unittest.main()
