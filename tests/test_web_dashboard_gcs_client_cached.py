"""Audit Q2.35 — `_dashboard_gcs_client` is process-cached.

Pre-fix every dashboard read (~5-10s polling × multiple tabs × 4-6
endpoints/tab) constructed a fresh ``google.cloud.storage.Client()``.
Each Client builds an auth-refresh thread + connection pool internally;
under polled-dashboard load that meant constant connection-pool churn
and a steady-state of ~30 idle auth threads in the web service.

This test file pins:
  - second invocation returns the same Client object (no rebuild)
  - changing GOOGLE_CLOUD_PROJECT busts the cache (multi-project dev)
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Set required env BEFORE importing web.server.
os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

# Stub out heavyweight google.cloud deps so import doesn't fail.
for name in ("google.cloud", "google.cloud.storage", "google.cloud.firestore"):
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

from web import server as _server


class TestDashboardGcsClientCached(unittest.TestCase):
    def setUp(self):
        # Reset the module-level cache between tests so each test
        # starts from a clean slate.
        _server._DASHBOARD_GCS_CLIENT = None
        _server._DASHBOARD_GCS_CLIENT_PROJECT = None

    def test_second_call_returns_same_client_instance(self):
        fake_client = MagicMock(name="FakeStorageClient")
        # The function imports `from google.cloud import storage` lazily,
        # so we patch `storage.Client` on whatever module shape exists
        # in sys.modules at call time.
        import google.cloud.storage as _storage  # noqa: PLC0415
        with patch.object(_storage, "Client", return_value=fake_client) as m:
            c1 = _server._dashboard_gcs_client()
            c2 = _server._dashboard_gcs_client()
            c3 = _server._dashboard_gcs_client()
        self.assertIs(c1, c2)
        self.assertIs(c1, c3)
        m.assert_called_once()

    def test_project_change_busts_the_cache(self):
        fake_a = MagicMock(name="ClientA")
        fake_b = MagicMock(name="ClientB")
        import google.cloud.storage as _storage  # noqa: PLC0415
        with patch.object(_storage, "Client", side_effect=[fake_a, fake_b]) as m:
            with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "proj-a"}):
                c1 = _server._dashboard_gcs_client()
            with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "proj-b"}):
                c2 = _server._dashboard_gcs_client()
        self.assertIsNot(c1, c2)
        self.assertEqual(m.call_count, 2)


if __name__ == "__main__":
    unittest.main()
