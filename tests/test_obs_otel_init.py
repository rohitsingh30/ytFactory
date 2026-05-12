"""Smoke tests for :mod:`pipeline.observability` P0 scaffold.

What we're verifying at this layer:

* The SDK boots (idempotently, with auto-mode resolution).
* Spans, logs, and metrics all flow into the in-memory exporter
  bundle so subsequent tests + the dashboard read path can introspect
  them.
* :class:`RenderContext` propagates as ``ytfactory.*`` attrs onto
  every span/log emitted inside ``ctx(...)``.
* :func:`obs.timed` records duration on success and marks ERROR on
  exception (re-raising).
* :func:`obs.track` emits a counter increment + a log record (and
  histogram observation when ``duration_ms`` is supplied).

These tests deliberately do NOT exercise the GCP exporters — that is
P8's smoke test against the real project.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from pipeline import observability as obs


class _Base(unittest.TestCase):
    """Reset the SDK between every test so signal buffers don't leak."""

    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()

    def tearDown(self) -> None:
        obs.reset_for_tests()

    # ----- helpers
    def flush(self) -> None:
        self.bundle.span_processor.force_flush()
        self.bundle.log_processor.force_flush()

    def spans(self):
        self.flush()
        return list(self.bundle.span_inmemory.get_finished_spans())

    def logs(self):
        self.flush()
        return list(self.bundle.log_inmemory.get_finished_logs())

    def metric_data(self):
        return self.bundle.metric_reader.get_metrics_data()


# ---- init / mode resolution ---------------------------------------------


class TestInit(_Base):
    def test_init_is_idempotent(self) -> None:
        first = obs.init()  # already in inmemory mode from setUp
        second = obs.init()
        self.assertIs(first, second)

    def test_init_with_force_replaces_bundle(self) -> None:
        first_mode = obs.current_mode()
        replaced = obs.init(mode="console", force=True)
        self.assertEqual(replaced.mode, "console")
        self.assertNotEqual(obs.current_mode(), first_mode)

    def test_is_initialised_after_init(self) -> None:
        self.assertTrue(obs.is_initialised())

    def test_reset_for_tests_clears_state(self) -> None:
        obs.reset_for_tests()
        self.assertFalse(obs.is_initialised())
        self.assertIsNone(obs.current_bundle())


# ---- track ----------------------------------------------------------------


class TestTrack(_Base):
    def test_track_emits_log_record(self) -> None:
        obs.track("ev", category="cat", metadata={"k": "v"})
        records = self.logs()
        self.assertEqual(len(records), 1)
        rec = records[0].log_record
        self.assertEqual(rec.body["event"], "ev")
        self.assertEqual(rec.body["category"], "cat")
        self.assertTrue(rec.body["success"])
        self.assertEqual(rec.body["metadata"], {"k": "v"})

    def test_track_failure_raises_severity(self) -> None:
        obs.track("boom", success=False, metadata={"why": "oops"})
        rec = self.logs()[0].log_record
        self.assertFalse(rec.body["success"])
        # OTel SeverityNumber.ERROR = 17
        self.assertEqual(int(rec.severity_number.value), 17)

    def test_track_with_duration_records_histogram(self) -> None:
        obs.track("with_dur", duration_ms=42)
        data = self.metric_data()
        # Find the histogram observation.
        seen = False
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name == obs.EVENT_HISTOGRAM_NAME:
                        for dp in m.data.data_points:
                            if dp.attributes.get("event") == "with_dur":
                                seen = True
                                self.assertGreaterEqual(dp.sum, 42)
        self.assertTrue(seen, "histogram should have a data point for with_dur")

    def test_track_never_raises_on_provider_failure(self) -> None:
        # Force a provider that raises on emit; track must swallow it.
        from unittest.mock import patch
        with patch.object(obs, "logger", side_effect=RuntimeError("dead")):
            obs.track("safe")  # must not raise


# ---- timed ----------------------------------------------------------------


class TestTimed(_Base):
    def test_timed_success_creates_span_and_log(self) -> None:
        with obs.timed("stage1", category="render") as t:
            t.add(metadata={"n": 7})
            time.sleep(0.005)
        spans = self.spans()
        self.assertEqual(len(spans), 1)
        s = spans[0]
        self.assertEqual(s.name, "stage1")
        self.assertTrue(s.status.is_ok)
        self.assertEqual(s.attributes["ytfactory.event"], "stage1")
        self.assertEqual(s.attributes["ytfactory.category"], "render")
        self.assertEqual(s.attributes["ytfactory.meta.n"], 7)
        self.assertGreaterEqual(s.attributes["ytfactory.duration_ms"], 1)

        rec = self.logs()[0].log_record
        self.assertEqual(rec.body["event"], "stage1")
        self.assertTrue(rec.body["success"])
        self.assertGreaterEqual(rec.body["duration_ms"], 1)

    def test_timed_failure_marks_error_and_reraises(self) -> None:
        with self.assertRaises(ValueError):
            with obs.timed("stage_err"):
                raise ValueError("boom")
        spans = self.spans()
        self.assertEqual(len(spans), 1)
        s = spans[0]
        self.assertFalse(s.status.is_ok)
        self.assertEqual(s.attributes["ytfactory.success"], False)
        # exception event recorded
        events = list(s.events)
        self.assertTrue(any(e.name == "exception" for e in events))

        rec = self.logs()[0].log_record
        self.assertFalse(rec.body["success"])

    def test_timed_soft_fail_via_handle(self) -> None:
        with obs.timed("soft_fail") as t:
            t.fail("downstream returned 500")
        s = self.spans()[0]
        self.assertFalse(s.status.is_ok)
        self.assertEqual(s.attributes["ytfactory.meta.fail_reason"],
                         "downstream returned 500")


# ---- context propagation -------------------------------------------------


class TestContext(_Base):
    def test_ctx_attrs_appear_on_nested_spans(self) -> None:
        with obs.ctx(channel="historyrecapped", slug="aita-001",
                     render_kind="short", render_mode="laptop"):
            with obs.timed("nested_stage"):
                pass
        s = self.spans()[0]
        self.assertEqual(s.attributes["ytfactory.channel"], "historyrecapped")
        self.assertEqual(s.attributes["ytfactory.slug"], "aita-001")
        self.assertEqual(s.attributes["ytfactory.render_kind"], "short")
        self.assertEqual(s.attributes["ytfactory.render_mode"], "laptop")

    def test_ctx_attrs_appear_on_track_log(self) -> None:
        with obs.ctx(channel="rhymetimejunction", slug="hathi"):
            obs.track("scoped_event")
        rec = self.logs()[0].log_record
        # track's body doesn't carry ctx fields, but its attributes do.
        self.assertEqual(rec.attributes["ytfactory.channel"],
                         "rhymetimejunction")
        self.assertEqual(rec.attributes["ytfactory.slug"], "hathi")

    def test_ctx_nesting_inherits_unset_fields(self) -> None:
        with obs.ctx(channel="cosmosdecoded", render_kind="long_form"):
            with obs.ctx(slug="eddington-1919"):
                cur = obs.current_context()
                self.assertEqual(cur.channel, "cosmosdecoded")
                self.assertEqual(cur.render_kind, "long_form")
                self.assertEqual(cur.slug, "eddington-1919")

    def test_ctx_pop_restores_parent(self) -> None:
        with obs.ctx(channel="A"):
            with obs.ctx(channel="B"):
                self.assertEqual(obs.current_context().channel, "B")
            self.assertEqual(obs.current_context().channel, "A")
        self.assertIsNone(obs.current_context().channel)


# ---- counter / metric assertions ----------------------------------------


class TestMetrics(_Base):
    def test_track_increments_counter(self) -> None:
        obs.track("e", category="c")
        obs.track("e", category="c")
        data = self.metric_data()
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name == obs.EVENT_COUNTER_NAME:
                        for dp in m.data.data_points:
                            if dp.attributes.get("event") == "e":
                                self.assertGreaterEqual(dp.value, 2)
                                return
        self.fail("counter for event 'e' not found")


class TestCloudRunIdentityAttrs(unittest.TestCase):
    """Pin the per-process collision-breaker attrs that flip the
    Cloud Monitoring resource projection from ``generic_node`` (one
    bucket per region — every Cloud Run JOB execution + every
    spawned subprocess collide and get rejected as
    "Points must be written in order") to ``generic_task`` (one
    bucket per ``service.instance.id``).

    Regression-pins the 2026-05-13 OTel-noise post-mortem where
    a phantom Cloud Monitoring 400 traceback drowned the actual
    render error in the subprocess error surface.
    """

    def setUp(self) -> None:
        from pipeline.observability import otel as _otel
        self._otel = _otel
        self._saved_env = {}
        for key in (
            "K_SERVICE", "K_REVISION", "CLOUD_RUN_JOB",
            "CLOUD_RUN_EXECUTION", "CLOUD_RUN_TASK_INDEX",
            "GOOGLE_CLOUD_REGION", "CLOUD_RUN_REGION",
        ):
            if key in os.environ:
                self._saved_env[key] = os.environ.pop(key)

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key in {
                "K_SERVICE", "K_REVISION", "CLOUD_RUN_JOB",
                "CLOUD_RUN_EXECUTION", "CLOUD_RUN_TASK_INDEX",
                "GOOGLE_CLOUD_REGION", "CLOUD_RUN_REGION",
            }:
                del os.environ[key]
        os.environ.update(self._saved_env)

    def test_off_cloud_run_returns_empty(self) -> None:
        self.assertEqual(self._otel._cloud_run_identity_attrs("svc"), {})

    def test_on_cloud_run_job_emits_unique_instance_id(self) -> None:
        os.environ["CLOUD_RUN_JOB"] = "render-worker-v2"
        os.environ["CLOUD_RUN_EXECUTION"] = "render-worker-v2-abcd-1"
        os.environ["CLOUD_RUN_TASK_INDEX"] = "0"
        attrs = self._otel._cloud_run_identity_attrs("render-worker-v2")
        # task_id format: <execution>-<task_index>-<pid>
        self.assertIn("service.instance.id", attrs)
        self.assertIn("render-worker-v2-abcd-1", attrs["service.instance.id"])
        self.assertTrue(attrs["service.instance.id"].endswith(f"-{os.getpid()}"))
        self.assertEqual(attrs["service.namespace"], "render-worker-v2")
        self.assertEqual(attrs["cloud.region"], "asia-southeast1")

    def test_on_cloud_run_service_uses_k_service_for_namespace(self) -> None:
        os.environ["K_SERVICE"] = "ytfactory-web"
        os.environ["K_REVISION"] = "ytfactory-web-00042-abc"
        attrs = self._otel._cloud_run_identity_attrs("ytfactory-web")
        self.assertEqual(attrs["service.namespace"], "ytfactory-web")
        self.assertIn("ytfactory-web-00042-abc", attrs["service.instance.id"])

    def test_region_overrides_default(self) -> None:
        os.environ["CLOUD_RUN_JOB"] = "x"
        os.environ["GOOGLE_CLOUD_REGION"] = "us-central1"
        attrs = self._otel._cloud_run_identity_attrs("x")
        self.assertEqual(attrs["cloud.region"], "us-central1")

    def test_two_processes_get_distinct_instance_ids(self) -> None:
        # Same execution, simulate different PIDs by mocking os.getpid.
        os.environ["CLOUD_RUN_JOB"] = "x"
        os.environ["CLOUD_RUN_EXECUTION"] = "exec-1"
        with mock.patch("pipeline.observability.otel.os.getpid", return_value=11):
            a = self._otel._cloud_run_identity_attrs("x")
        with mock.patch("pipeline.observability.otel.os.getpid", return_value=22):
            b = self._otel._cloud_run_identity_attrs("x")
        self.assertNotEqual(a["service.instance.id"], b["service.instance.id"])

    def test_resource_yields_generic_task_via_gcp_mapping(self) -> None:
        # End-to-end: build the resource and run it through the SAME
        # OTel→Cloud Monitoring mapper the production exporter uses.
        # `generic_task` (NOT `generic_node`) is what proves the
        # collision is actually broken in the API call shape.
        os.environ["CLOUD_RUN_JOB"] = "render-worker-v2"
        os.environ["CLOUD_RUN_EXECUTION"] = "exec-xyz"
        os.environ["CLOUD_RUN_TASK_INDEX"] = "0"
        resource = self._otel._build_resource(
            service_name="render-worker-v2",
            service_version="rev-001",
            extra=None,
        )
        from opentelemetry.resourcedetector.gcp_resource_detector._mapping import (
            get_monitored_resource,
        )
        mr = get_monitored_resource(resource)
        self.assertEqual(mr.type, "generic_task")
        self.assertTrue(mr.labels.get("task_id"))
        self.assertNotEqual(mr.labels["task_id"], "")
        self.assertEqual(mr.labels["namespace"], "render-worker-v2")
        self.assertEqual(mr.labels["job"], "render-worker-v2")
        # `location` falls back per the upstream mapping; `cloud.region`
        # we set above is the source.
        self.assertEqual(mr.labels["location"], "asia-southeast1")


if __name__ == "__main__":
    unittest.main()
