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


if __name__ == "__main__":
    unittest.main()
