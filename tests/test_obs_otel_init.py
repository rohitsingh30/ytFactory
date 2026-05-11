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

import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
