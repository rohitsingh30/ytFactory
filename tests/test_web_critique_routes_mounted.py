"""Pin: web/server.py mounts the control critique router.

The /api/jobs/{job_id}/critique/* surface (start + token) was added in
the 2026-05-11 critique-chat cutover but the production app —
web/server.py — was not updated to mount it. The browser's "Start
critique" button consequently 404'd in prod (POST
/api/jobs/<id>/critique/start → 404).

This test imports web/server.py and asserts the critique routes exist
on the ASGI app so the next reorganisation of the control router
imports can't silently drop the critique chat again.

Mirrors tests/test_web_cloud_routes_mounted.py (same regression class).
"""
from __future__ import annotations

import os
import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

from web import server


class CritiqueRoutesMountedTests(unittest.TestCase):
    def test_critique_routes_present(self) -> None:
        paths = {r.path for r in server.app.routes if hasattr(r, "path")}
        for needed in (
            "/api/jobs/{job_id}/critique/start",
            "/api/jobs/{job_id}/critique/token",
        ):
            self.assertIn(
                needed, paths,
                f"web/server.py missing route {needed} — the critique-chat "
                "router (control/routes/critique_routes.py) must be both "
                "imported and included via app.include_router(...)",
            )


if __name__ == "__main__":
    unittest.main()
