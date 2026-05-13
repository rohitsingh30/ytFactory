"""Regression tests for ``pipeline.observability.telemetry`` corner
cases — specifically the NonRecordingSpan crash that took down 4 cloud
renders on 2026-05-12.

The bug: ``_close_span`` read ``span.status.status_code`` to avoid a
redundant ``set_status(ERROR)`` call. Real ``RecordingSpan`` instances
have ``.status``, but ``NonRecordingSpan`` (returned by the OTel SDK
when instrumentation isn't fully booted, e.g. during a Cloud Run cold
start within the first ~1s) does NOT — the read raised
``AttributeError`` mid-render and crashed the worker.

The fix removes the read and unconditionally calls
``set_status(ERROR)`` on failure (it's a no-op on NonRecordingSpan
and idempotent on RecordingSpan). These tests pin both paths so the
read can't sneak back.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from opentelemetry import trace as _trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags

from pipeline import observability as obs


class TestTimedSurvivesNonRecordingSpan(unittest.TestCase):
    """The cloud render-worker hits NonRecordingSpan during a brief
    window at startup. ``obs.timed`` MUST NOT raise AttributeError on
    that span — telemetry must NEVER block the pipeline."""

    def setUp(self) -> None:
        obs.reset_for_tests()
        obs.init_in_memory()

    def tearDown(self) -> None:
        obs.reset_for_tests()

    def test_timed_with_nonrecording_span_no_failure(self) -> None:
        # Build a real NonRecordingSpan to mirror what tracer().start_span()
        # returns when instrumentation isn't booted.
        ctx = SpanContext(
            trace_id=0x12345,
            span_id=0x67890,
            is_remote=False,
            trace_flags=TraceFlags(0),
        )
        nr_span = NonRecordingSpan(ctx)
        # Sanity-check the upstream contract — if .status ever appears
        # on NonRecordingSpan in a future OTel release, this test
        # becomes redundant and we can simplify _close_span. Today
        # the attribute genuinely doesn't exist.
        self.assertFalse(hasattr(nr_span, "status"))

        # Patch the tracer to return the NonRecordingSpan so the real
        # `obs.timed` code path runs against it.
        from pipeline.observability import telemetry as obs_t
        original = obs_t.tracer
        try:
            tracer_mock = MagicMock()
            tracer_mock.start_span = MagicMock(return_value=nr_span)
            obs_t.tracer = MagicMock(return_value=tracer_mock)

            # Both success and failure paths must return cleanly.
            with obs.timed("smoke_event", category="render"):
                pass

            try:
                with obs.timed("smoke_failure", category="render"):
                    raise ValueError("simulated stage failure")
            except ValueError:
                pass  # the timed CM re-raises after recording — expected
        finally:
            obs_t.tracer = original

    def test_set_status_called_on_failure_path(self) -> None:
        """A mock span receives ``set_status(ERROR)`` on the failure
        path even when ``.status`` would have AttributeError'd.
        Without this we silently lose error spans in Cloud Trace."""
        from pipeline.observability import telemetry as obs_t
        # Mirror NonRecordingSpan's surface: methods exist (set_status,
        # set_attribute, end, is_recording, record_exception); the
        # ``status`` attribute does NOT — which is what triggered the
        # original crash.
        span_mock = MagicMock(spec=[
            "set_attribute", "set_status", "end",
            "is_recording", "record_exception",
        ])
        span_mock.is_recording.return_value = False
        original = obs_t.tracer
        try:
            tracer_mock = MagicMock()
            tracer_mock.start_span = MagicMock(return_value=span_mock)
            obs_t.tracer = MagicMock(return_value=tracer_mock)

            try:
                with obs.timed("failure_event", category="x"):
                    raise RuntimeError("boom")
            except RuntimeError:
                pass

            # set_status should have been called with an ERROR status.
            self.assertTrue(span_mock.set_status.called)
            sentinel = span_mock.set_status.call_args[0][0]
            self.assertIsInstance(sentinel, Status)
            self.assertEqual(sentinel.status_code, StatusCode.ERROR)
            # And the span must always be ended exactly once.
            span_mock.end.assert_called_once()
        finally:
            obs_t.tracer = original


class TestMetricAttrsCarryRenderContext(unittest.TestCase):
    """Audit T1.12 — track() and timed() must thread channel + slug +
    render_kind from the active RenderContext into metric_attrs so
    Cloud Monitoring can per-channel rollup. Pre-fix the metric path
    only carried event/category/success → every counter aggregated
    globally."""

    def setUp(self) -> None:
        obs.reset_for_tests()
        obs.init_in_memory()

    def tearDown(self) -> None:
        obs.reset_for_tests()

    def test_track_metric_attrs_include_context_fields(self) -> None:
        from unittest.mock import patch as _patch
        from pipeline.observability import telemetry as obs_t

        captured: list[dict] = []
        fake_counter = MagicMock()
        fake_counter.add = MagicMock(side_effect=lambda v, attributes: captured.append(dict(attributes)))
        with _patch.object(obs_t, "_counter", return_value=fake_counter):
            with obs.ctx(channel="historyrecapped", slug="aita-001",
                         render_kind="long_form"):
                obs.track("something_happened", category="render", success=True)

        self.assertTrue(captured)
        attrs = captured[0]
        self.assertEqual(attrs.get("channel"), "historyrecapped")
        self.assertEqual(attrs.get("slug"), "aita-001")
        self.assertEqual(attrs.get("render_kind"), "long_form")
        self.assertEqual(attrs.get("event"), "something_happened")

    def test_track_metric_attrs_omit_unset_context_fields(self) -> None:
        from unittest.mock import patch as _patch
        from pipeline.observability import telemetry as obs_t

        captured: list[dict] = []
        fake_counter = MagicMock()
        fake_counter.add = MagicMock(side_effect=lambda v, attributes: captured.append(dict(attributes)))
        with _patch.object(obs_t, "_counter", return_value=fake_counter):
            # No context envelope — fields stay unset.
            obs.track("ambient_event", category="cron", success=True)

        attrs = captured[0]
        # Channel / slug / render_kind absent → not in metric attrs
        # (so cloud monitoring doesn't bucket as "channel=None").
        self.assertNotIn("channel", attrs)
        self.assertNotIn("slug", attrs)
        self.assertNotIn("render_kind", attrs)

    def test_timed_metric_attrs_include_context_fields(self) -> None:
        from unittest.mock import patch as _patch
        from pipeline.observability import telemetry as obs_t

        captured: list[dict] = []
        fake_counter = MagicMock()
        fake_counter.add = MagicMock(side_effect=lambda v, attributes: captured.append(dict(attributes)))
        fake_hist = MagicMock()
        with _patch.object(obs_t, "_counter", return_value=fake_counter), \
             _patch.object(obs_t, "_histogram", return_value=fake_hist):
            with obs.ctx(channel="cosmosdecoded", slug="lhc-discovery",
                         render_kind="long_form"):
                with obs.timed("tts_synth", category="tts"):
                    pass

        self.assertTrue(captured)
        attrs = captured[0]
        self.assertEqual(attrs.get("channel"), "cosmosdecoded")
        self.assertEqual(attrs.get("slug"), "lhc-discovery")
        self.assertEqual(attrs.get("render_kind"), "long_form")


    def test_track_metric_attrs_include_job_id(self) -> None:
        from unittest.mock import patch as _patch
        from pipeline.observability import telemetry as obs_t

        captured: list[dict] = []
        fake_counter = MagicMock()
        fake_counter.add = MagicMock(side_effect=lambda v, attributes: captured.append(dict(attributes)))
        with _patch.object(obs_t, "_counter", return_value=fake_counter):
            obs.track("from_a_job", category="render",
                      success=True, job_id="abc-123")

        attrs = captured[0]
        self.assertEqual(attrs.get("job_id"), "abc-123")


class TestTimedKeyboardInterruptD354(unittest.TestCase):
    """Audit D3.54 — pre-fix `with obs.timed("...")` caught
    BaseException and recorded the span as ERROR. KeyboardInterrupt
    + SystemExit are control-flow exceptions, not failures: a
    user's Ctrl-C during a render shouldn't pollute the dashboard's
    error rate. Now: those two re-raise without error decoration
    and the span closes with success=True."""

    def test_keyboard_interrupt_does_not_record_as_error(self) -> None:
        from unittest.mock import patch as _patch
        from pipeline.observability import telemetry as obs_t

        with _patch.object(obs_t, "record_exception") as record_mock:
            with self.assertRaises(KeyboardInterrupt):
                with obs.timed("user_interrupted") as h:
                    raise KeyboardInterrupt()
        # KeyboardInterrupt MUST NOT be recorded as an exception on
        # the span (pre-fix this fired record_exception(fatal=True)).
        record_mock.assert_not_called()

    def test_system_exit_does_not_record_as_error(self) -> None:
        from unittest.mock import patch as _patch
        from pipeline.observability import telemetry as obs_t

        with _patch.object(obs_t, "record_exception") as record_mock:
            with self.assertRaises(SystemExit):
                with obs.timed("system_exit") as h:
                    raise SystemExit(0)
        record_mock.assert_not_called()

    def test_real_exception_still_recorded_as_error(self) -> None:
        # Sanity: the carve-out only covers KeyboardInterrupt /
        # SystemExit. A regular Exception STILL fires record_exception.
        from unittest.mock import patch as _patch
        from pipeline.observability import telemetry as obs_t

        with _patch.object(obs_t, "record_exception") as record_mock:
            with self.assertRaises(RuntimeError):
                with obs.timed("real_failure"):
                    raise RuntimeError("boom")
        record_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
