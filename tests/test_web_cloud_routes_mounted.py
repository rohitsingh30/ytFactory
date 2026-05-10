"""Pin: web/server.py mounts the control cloud router.

The /api/cloud/* surface (health, cost, deploys, services, refresh,
warm) was added in the "feat(cloud): admin Cloud tab" cutover but the
production app — web/server.py — was not updated to mount it. The
operator's Cloud admin tab consequently 404'd in prod.

This test imports web/server.py and asserts the routes exist on the
ASGI app, so the next person who reorganises the control router
imports can't silently drop the cloud tab again.
"""
from __future__ import annotations

import os
import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

from web import server


class CloudRoutesMountedTests(unittest.TestCase):
    def test_cloud_routes_present(self) -> None:
        paths = {r.path for r in server.app.routes if hasattr(r, "path")}
        for needed in (
            "/api/cloud/health",
            "/api/cloud/cost",
            "/api/cloud/deploys",
            "/api/cloud/services",
            "/api/cloud/refresh",
            "/api/cloud/warm",
        ):
            self.assertIn(needed, paths, f"web/server.py missing route {needed}")


if __name__ == "__main__":
    unittest.main()
