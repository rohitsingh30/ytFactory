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

    def _seed_full_render(self) -> None:
        """Two complete renders so /renders + /stage_latency have
        data shaped like a real dashboard hit (envelope + child
        stages, with one failure to exercise the success aggregation).
        """
        # Render 1: short, fully successful. envelope wraps stages so
        # they all share channel/slug/render_kind context.
        with obs.render_envelope(channel="historyrecapped",
                                 slug="aita-001",
                                 render_kind="short"):
            with obs.timed("tts_synth", category="tts",
                           metadata={"provider": "cloudrun_chatterbox"}):
                pass
            with obs.timed("image_gen", category="image",
                           metadata={"provider": "cloudrun_flux2_klein"}):
                pass
            with obs.timed("compose", category="render"):
                pass
            with obs.timed("upload_short", category="upload"):
                pass
        # Render 2: long_form, image_gen failed.
        with obs.render_envelope(channel="historyrecapped",
                                 slug="napoleon-collapse",
                                 render_kind="long_form"):
            with obs.timed("tts_synth", category="tts",
                           metadata={"provider": "cloudrun_chatterbox"}):
                pass
            try:
                with obs.timed("image_gen", category="image"):
                    raise RuntimeError("flux down")
            except RuntimeError:
                pass


class TestInitStatus(_Base):
    def test_returns_inmemory_mode(self) -> None:
        r = self.client.get("/api/telemetry/init_status").json()
        self.assertTrue(r["initialised"])
        self.assertEqual(r["exporter"], "inmemory")
        self.assertFalse(r["has_gcp_exporter"])
        # Cross-service Cloud Logging reader is gcp-only — must
        # report unavailable in inmemory mode so the dashboard
        # doesn't claim a feature it can't use.
        self.assertFalse(r["cloud_logging_reader_available"])


class TestStageLatency(_Base):
    def test_returns_per_stage_p50_p95(self) -> None:
        self._seed_full_render()
        r = self.client.get("/api/telemetry/stage_latency?hours=1").json()
        names = {s["stage"] for s in r["stages"]}
        # All four stage events from the successful render plus the
        # render envelopes themselves end up in the latency rollup.
        self.assertIn("tts_synth", names)
        self.assertIn("image_gen", names)
        self.assertIn("compose", names)
        self.assertIn("upload_short", names)
        # Envelopes are render-pipeline events too — they should
        # appear so the operator can read total render duration off
        # the same chart.
        self.assertTrue(any(n.startswith("render.") for n in names))
        # Non-stage events MUST be excluded so the chart isn't
        # dominated by 0-ms bookkeeping.
        obs.track("cache_hit", category="cache")
        obs.track("cloud.health.sweep", category="cloud", duration_ms=15000)
        r2 = self.client.get("/api/telemetry/stage_latency?hours=1").json()
        names2 = {s["stage"] for s in r2["stages"]}
        self.assertNotIn("cache_hit", names2)
        self.assertNotIn("cloud.health.sweep", names2)

    def test_failed_stage_counted(self) -> None:
        self._seed_full_render()
        r = self.client.get("/api/telemetry/stage_latency?hours=1").json()
        image = next(s for s in r["stages"] if s["stage"] == "image_gen")
        self.assertGreaterEqual(image["failed"], 1)

    def test_sorted_by_p95_desc(self) -> None:
        self._seed_full_render()
        r = self.client.get("/api/telemetry/stage_latency?hours=1").json()
        p95s = [s["p95_ms"] for s in r["stages"]]
        self.assertEqual(p95s, sorted(p95s, reverse=True))


class TestRenders(_Base):
    def test_returns_one_row_per_render(self) -> None:
        self._seed_full_render()
        r = self.client.get("/api/telemetry/renders?hours=1").json()
        slugs = {row["slug"] for row in r["renders"]}
        self.assertEqual(slugs, {"aita-001", "napoleon-collapse"})

    def test_render_carries_stage_breakdown(self) -> None:
        self._seed_full_render()
        r = self.client.get("/api/telemetry/renders?hours=1").json()
        aita = next(row for row in r["renders"] if row["slug"] == "aita-001")
        stage_names = [s["name"] for s in aita["stages"]]
        # All four child stages must be present.
        self.assertIn("tts_synth", stage_names)
        self.assertIn("image_gen", stage_names)
        self.assertIn("compose", stage_names)
        self.assertIn("upload_short", stage_names)
        self.assertEqual(aita["render_kind"], "short")
        self.assertGreaterEqual(aita["total_ms"], 0)
        self.assertTrue(aita["has_envelope"])
        self.assertTrue(aita["success"])

    def test_render_marked_failed_when_any_stage_failed(self) -> None:
        self._seed_full_render()
        r = self.client.get("/api/telemetry/renders?hours=1").json()
        nap = next(row for row in r["renders"]
                   if row["slug"] == "napoleon-collapse")
        self.assertFalse(nap["success"])
        self.assertEqual(nap["render_kind"], "long_form")

    def test_channel_filter(self) -> None:
        self._seed_full_render()
        with obs.render_envelope(channel="mystoriesanimated",
                                 slug="other-slug",
                                 render_kind="short"):
            with obs.timed("tts_synth", category="tts"):
                pass
        r = self.client.get(
            "/api/telemetry/renders?hours=1&channel=historyrecapped",
        ).json()
        channels = {row["channel"] for row in r["renders"]}
        self.assertEqual(channels, {"historyrecapped"})

    def test_sorted_newest_first(self) -> None:
        self._seed_full_render()
        r = self.client.get("/api/telemetry/renders?hours=1").json()
        starts = [row["started_at"] for row in r["renders"]]
        self.assertEqual(starts, sorted(starts, reverse=True))

    def test_renders_without_channel_or_slug_excluded(self) -> None:
        # An obs.timed with no ctx → no channel/slug in metadata →
        # not a render. Must be silently dropped instead of showing
        # up as a "?" / "?" row.
        with obs.timed("tts_synth", category="tts"):
            pass
        r = self.client.get("/api/telemetry/renders?hours=1").json()
        self.assertEqual(r["renders"], [])


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
