"""P6 — verifies the new /api/telemetry/* routes."""
from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline import observability as obs
from control.routes.telemetry_routes import router


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        # Clear YTFACTORY_AGENT_TOKEN so the route's auth check passes
        # in dev posture (matches the Next.js proxy behaviour). The
        # e2e_happy_path test sets this env and never restores it,
        # which would otherwise 401 every request below.
        import os
        self._saved_token = os.environ.pop("YTFACTORY_AGENT_TOKEN", None)
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        import os
        if self._saved_token is not None:
            os.environ["YTFACTORY_AGENT_TOKEN"] = self._saved_token
        obs.reset_for_tests()

    def _seed_events(self) -> None:
        # 2 successes (one slow), 1 failure, in 2 categories.
        with obs.ctx(channel="historyrecapped", slug="aita-001"):
            with obs.timed("tts_synth", category="tts",
                           metadata={"provider": "cloudrun_chatterbox"}):
                pass
            with obs.timed("image_gen", category="image",
                           metadata={"provider": "cloudrun_flux2_klein"}):
                pass
            try:
                with obs.timed("upload_short", category="upload"):
                    raise ValueError("boom")
            except ValueError:
                pass


class TestInitStatus(_Base):
    def test_returns_inmemory_mode(self) -> None:
        r = self.client.get("/api/telemetry/init_status").json()
        self.assertTrue(r["initialised"])
        self.assertEqual(r["exporter"], "inmemory")
        self.assertFalse(r["has_gcp_exporter"])


class TestOverview(_Base):
    def test_overview_counts_events(self) -> None:
        self._seed_events()
        r = self.client.get("/api/telemetry/overview?hours=1").json()
        self.assertEqual(r["total_events"], 3)
        self.assertEqual(r["successes"], 2)
        self.assertEqual(r["failures"], 1)
        self.assertIn("historyrecapped", r["by_channel"])
        self.assertIn("tts", r["by_category"])
        self.assertIn("image", r["by_category"])
        self.assertIn("upload", r["by_category"])

    def test_overview_empty_window(self) -> None:
        r = self.client.get("/api/telemetry/overview?hours=1").json()
        self.assertEqual(r["total_events"], 0)
        self.assertIsNone(r["success_rate"])


class TestStages(_Base):
    def test_stages_groups_by_event_name(self) -> None:
        self._seed_events()
        r = self.client.get("/api/telemetry/stages?hours=1").json()
        names = [s["name"] for s in r["stages"]]
        self.assertIn("tts_synth", names)
        self.assertIn("image_gen", names)
        self.assertIn("upload_short", names)
        # upload_short failed → failed >= 1
        upload = next(s for s in r["stages"] if s["name"] == "upload_short")
        self.assertGreaterEqual(upload["failed"], 1)


class TestServices(_Base):
    def test_services_groups_by_provider_or_category(self) -> None:
        self._seed_events()
        r = self.client.get("/api/telemetry/services?hours=1").json()
        services = [s["service"] for s in r["services"]]
        self.assertIn("cloudrun_chatterbox", services)
        self.assertIn("cloudrun_flux2_klein", services)
        self.assertIn("upload", services)


class TestTimeline(_Base):
    def test_timeline_buckets_have_total_count(self) -> None:
        self._seed_events()
        r = self.client.get("/api/telemetry/timeline?hours=1").json()
        total = sum(b["count"] for b in r["series"])
        self.assertEqual(total, 3)
        # Errors split out separately.
        err_total = sum(b["errors"] for b in r["series"])
        self.assertEqual(err_total, 1)


class TestErrors(_Base):
    def test_errors_returns_only_failures(self) -> None:
        self._seed_events()
        r = self.client.get("/api/telemetry/errors?hours=1&limit=5").json()
        self.assertEqual(len(r["errors"]), 1)
        self.assertEqual(r["errors"][0]["event"], "upload_short")
        self.assertFalse(r["errors"][0]["success"])


class TestLinks(_Base):
    def test_no_project_returns_unavailable(self) -> None:
        import os
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        os.environ.pop("GCP_PROJECT", None)
        r = self.client.get("/api/telemetry/links").json()
        self.assertFalse(r["available"])

    def test_with_project_returns_url_dict(self) -> None:
        import os
        os.environ["GOOGLE_CLOUD_PROJECT"] = "ytfactory-test"
        try:
            r = self.client.get(
                "/api/telemetry/links?channel=hr&slug=aita-001",
            ).json()
            self.assertTrue(r["available"])
            self.assertEqual(r["channel"], "hr")
            self.assertIn("traces/list", r["links"]["trace"])
            self.assertIn("logs/query", r["links"]["logging"])
            self.assertIn("metrics-explorer", r["links"]["monitoring"])
        finally:
            os.environ.pop("GOOGLE_CLOUD_PROJECT", None)


if __name__ == "__main__":
    unittest.main()
