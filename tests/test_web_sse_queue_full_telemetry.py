"""Audit Q2.42 — SSE QueueFull drops emit a telemetry event.

Pre-fix the QueueFull catch was silent (`pass`). Faster-than-drain
producers caused random missed events in the dashboard's per-stage
waterfall with no signal for the operator. Now record an
``sse_event_dropped`` telemetry event so the dashboard's "drops"
panel can surface the throughput problem.

Tests exercise: web/server.py::emit
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "test-token")
os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")

for name in ("google.cloud", "google.cloud.storage", "google.cloud.firestore"):
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

from web import server as _server


class TestSseQueueFullEmitsTelemetry(unittest.TestCase):
    def test_dropped_event_records_telemetry(self):
        # Build a Job + StageEvent + a maxsize=1 queue that's already full.
        job = _server.Job(job_id="test-q242", niche="x", slug="s",
                          options={}, created_at=0.0)
        ev = _server.StageEvent(job_id="j", ts=0.0, stage="compose", status="start", message="m")
        full_q: asyncio.Queue = asyncio.Queue(maxsize=1)
        # Pre-fill so put_nowait raises QueueFull on the next call.
        full_q.put_nowait("filler")
        _server.SUBSCRIBERS[job.job_id] = [full_q]
        try:
            with patch.object(_server.tlm, "track") as mock_track:
                _server.emit(job, ev)
            # Should have called tlm.track with sse_event_dropped at
            # least once. (tlm.track is also called by stage_error /
            # job_cancelled paths in emit; we check for the
            # sse_event_dropped name specifically.)
            calls = mock_track.call_args_list
            names = [c.args[0] if c.args else c.kwargs.get("name") for c in calls]
            self.assertIn("sse_event_dropped", names,
                          f"sse_event_dropped not in tracked names: {names}")
        finally:
            _server.SUBSCRIBERS.pop(job.job_id, None)

    def test_drained_queue_does_not_emit_drop_event(self):
        # Roomy queue → put_nowait succeeds → no drop telemetry.
        job = _server.Job(job_id="test-q242b", niche="x", slug="s",
                          options={}, created_at=0.0)
        ev = _server.StageEvent(job_id="j", ts=0.0, stage="compose", status="start", message="m")
        ok_q: asyncio.Queue = asyncio.Queue(maxsize=10)
        _server.SUBSCRIBERS[job.job_id] = [ok_q]
        try:
            with patch.object(_server.tlm, "track") as mock_track:
                _server.emit(job, ev)
            calls = mock_track.call_args_list
            names = [c.args[0] if c.args else c.kwargs.get("name") for c in calls]
            self.assertNotIn("sse_event_dropped", names)
            # And the event landed in the queue.
            self.assertEqual(ok_q.qsize(), 1)
        finally:
            _server.SUBSCRIBERS.pop(job.job_id, None)

    def test_telemetry_failure_doesnt_break_emit(self):
        """Audit Q2.42 — telemetry must never break the producer path."""
        job = _server.Job(job_id="test-q242c", niche="x", slug="s",
                          options={}, created_at=0.0)
        ev = _server.StageEvent(job_id="j", ts=0.0, stage="compose", status="start", message="m")
        full_q: asyncio.Queue = asyncio.Queue(maxsize=1)
        full_q.put_nowait("filler")
        _server.SUBSCRIBERS[job.job_id] = [full_q]
        try:
            with patch.object(_server.tlm, "track",
                              side_effect=RuntimeError("telemetry down")):
                # Must not raise.
                _server.emit(job, ev)
        finally:
            _server.SUBSCRIBERS.pop(job.job_id, None)


if __name__ == "__main__":
    unittest.main()
