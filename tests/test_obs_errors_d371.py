"""Tests for pipeline.observability.errors.record_exception.

Audit D3.71 — pre-fix this kept the FIRST 4096 chars of the
traceback, which truncated the END — losing the actual exception
message + closest frames. Now keeps head + tail.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from pipeline.observability import errors as obs_errors


class RecordExceptionTest(unittest.TestCase):
    def test_no_recording_span_no_op(self):
        # Span is None — should not raise.
        obs_errors.record_exception(ValueError("boom"), span=None)

    def test_short_traceback_kept_whole(self):
        try:
            raise ValueError("small")
        except ValueError as exc:
            span = MagicMock()
            span.is_recording.return_value = True
            obs_errors.record_exception(exc, span=span)
        span.record_exception.assert_called_once()
        attrs = span.record_exception.call_args.kwargs["attributes"]
        self.assertIn("exception.type", attrs)
        self.assertEqual(attrs["exception.type"], "ValueError")
        self.assertEqual(attrs["exception.message"], "small")
        # Short traceback — no truncation marker.
        self.assertNotIn("[truncated]", attrs["exception.stacktrace"])

    def test_long_traceback_keeps_head_and_tail(self):
        # Build a synthetic exception with a long pre-formatted traceback.
        try:
            raise ValueError("boom" * 5)
        except ValueError as exc:
            # Replace the formatted traceback by patching format_exception.
            from unittest.mock import patch
            big_tb = (
                "Traceback (most recent call last):\n"
                + ("  File 'a.py', line 1, in head_frame\n    do_head()\n" * 100)
                + ("  File 'b.py', line 2, in tail_frame\n    do_tail()\n" * 200)
                + "ValueError: boomboomboomboomboom\n"
            )
            with patch("pipeline.observability.errors.traceback.format_exception",
                        return_value=[big_tb]):
                span = MagicMock()
                span.is_recording.return_value = True
                obs_errors.record_exception(exc, span=span)
        attrs = span.record_exception.call_args.kwargs["attributes"]
        tb = attrs["exception.stacktrace"]
        # Truncated marker present.
        self.assertIn("[truncated]", tb)
        # Head: first frame info preserved.
        self.assertIn("Traceback (most recent call last)", tb)
        # Tail: actual error type + message preserved (the most useful
        # diagnostic info — pre-fix this was LOST when truncating from
        # the end).
        self.assertIn("ValueError: boom", tb)
        # Total size capped at the configured limit.
        self.assertLessEqual(len(tb), obs_errors._MAX_TRACE_CHARS + 100,
                             "must not exceed the truncation budget")

    def test_extra_attrs_prefixed(self):
        try:
            raise RuntimeError("oops")
        except RuntimeError as exc:
            span = MagicMock()
            span.is_recording.return_value = True
            obs_errors.record_exception(
                exc, span=span,
                extra={"job_id": "j-123", "stage": "tts"},
            )
        attrs = span.record_exception.call_args.kwargs["attributes"]
        self.assertEqual(attrs["ytfactory.error.job_id"], "j-123")
        self.assertEqual(attrs["ytfactory.error.stage"], "tts")

    def test_status_code_set_on_fatal(self):
        try:
            raise RuntimeError("fatal")
        except RuntimeError as exc:
            span = MagicMock()
            span.is_recording.return_value = True
            obs_errors.record_exception(exc, span=span, fatal=True)
        # Status set with ERROR code.
        span.set_status.assert_called_once()

    def test_status_not_set_when_fatal_false(self):
        try:
            raise RuntimeError("warning-only")
        except RuntimeError as exc:
            span = MagicMock()
            span.is_recording.return_value = True
            obs_errors.record_exception(exc, span=span, fatal=False)
        span.set_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
