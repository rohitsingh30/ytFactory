"""End-to-end tests for the legacy ``pipeline.telemetry`` shim.

The shim's job is to keep the legacy public surface (``track``,
``track_stage_done``, ``timed``, ``read_events``, ``percentile``)
working byte-compatibly while routing every signal through OTel under
the hood. These tests verify that contract using the OTel in-memory
exporter — the JSONL backend they originally exercised has been
deleted (see ``docs/telemetry.md``).

Coverage mirrors the original ``test_utils_telemetry.py`` so any
regression in the shim is caught with the same shape of assertions
the team already understands.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from pipeline import observability as obs
from pipeline import telemetry as tlm


class _Base(unittest.TestCase):
    """Boot a fresh in-memory OTel SDK per test — no leakage."""

    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()

    def tearDown(self) -> None:
        obs.reset_for_tests()

    def _events(self) -> list[dict]:
        return tlm.read_events()


class TestTrack(_Base):
    def test_success_writes_record(self) -> None:
        tlm.track("ev", category="cat", success=True, duration_ms=99,
                  job_id="j1", metadata={"k": "v"})
        events = self._events()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["event"], "ev")
        self.assertEqual(e["category"], "cat")
        self.assertTrue(e["success"])
        self.assertEqual(e["duration_ms"], 99)
        self.assertEqual(e["job_id"], "j1")
        self.assertEqual(e["metadata"], {"k": "v"})

    def test_defaults(self) -> None:
        tlm.track("minimal")
        events = self._events()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["category"], "pipeline")
        self.assertTrue(e["success"])
        self.assertIsNone(e["duration_ms"])
        self.assertIsNone(e["job_id"])
        self.assertEqual(e["metadata"], {})

    def test_success_false(self) -> None:
        tlm.track("fail", success=False)
        e = self._events()[0]
        self.assertFalse(e["success"])

    def test_failure_does_not_raise(self) -> None:
        with patch.object(obs, "logger", side_effect=RuntimeError("dead")):
            tlm.track("safe")  # must not raise

    def test_multiple_writes_appear_in_order(self) -> None:
        tlm.track("e1")
        tlm.track("e2")
        events = self._events()
        self.assertEqual([e["event"] for e in events], ["e1", "e2"])


class TestTrackStageDone(_Base):
    def test_track_stage_done_emits_event(self) -> None:
        tlm.track_stage_done("stage_done", duration_ms=12,
                             metadata={"stage": "tts"})
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "stage_done")
        self.assertEqual(events[0]["duration_ms"], 12)
        self.assertEqual(events[0]["metadata"], {"stage": "tts"})


class TestTimed(_Base):
    def test_success_records_event_with_duration(self) -> None:
        with tlm.timed("stage", category="test", job_id="j",
                       metadata={"x": 1}) as t:
            t.add(metadata={"y": 2})
        events = self._events()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertTrue(e["success"])
        self.assertEqual(e["event"], "stage")
        self.assertEqual(e["category"], "test")
        self.assertEqual(e["job_id"], "j")
        self.assertIsInstance(e["duration_ms"], int)
        self.assertGreaterEqual(e["duration_ms"], 0)
        self.assertEqual(e["metadata"]["x"], 1)
        self.assertEqual(e["metadata"]["y"], 2)

    def test_exception_records_failure_and_reraises(self) -> None:
        with self.assertRaises(ValueError):
            with tlm.timed("err_stage"):
                raise ValueError("boom")
        e = self._events()[0]
        self.assertFalse(e["success"])
        self.assertIn("ValueError", e["metadata"]["error"])

    def test_timed_no_metadata_kwarg(self) -> None:
        with tlm.timed("bare"):
            pass
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["success"])

    def test_explicit_end_with_success_false(self) -> None:
        with tlm.timed("soft", metadata={"a": 1}) as t:
            t.end(success=False, metadata={"b": 2})
        e = self._events()[0]
        self.assertFalse(e["success"])
        self.assertEqual(e["metadata"]["a"], 1)
        self.assertEqual(e["metadata"]["b"], 2)


class TestReadEvents(_Base):
    def test_no_events_returns_empty(self) -> None:
        self.assertEqual(self._events(), [])

    def test_sorted_oldest_first(self) -> None:
        tlm.track("a")
        tlm.track("b")
        tlm.track("c")
        events = self._events()
        self.assertEqual([e["event"] for e in events], ["a", "b", "c"])

    def test_since_ts_filters(self) -> None:
        import time
        tlm.track("old")
        cutoff = time.time()
        time.sleep(0.001)
        tlm.track("new")
        filtered = tlm.read_events(since_ts=cutoff)
        self.assertEqual([e["event"] for e in filtered], ["new"])

    def test_limit_keeps_most_recent(self) -> None:
        for i in range(5):
            tlm.track(f"e{i}")
        out = tlm.read_events(limit=2)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[-1]["event"], "e4")

    def test_console_mode_shadow_buffer_serves_events(self) -> None:
        """``console`` mode now installs a bounded in-process shadow
        log buffer alongside the primary console exporter so the
        dashboard's ``/api/telemetry/*`` routes have data without
        depending on Cloud Logging ingestion. Pre-fix this returned
        ``[]`` and the dashboard was silent in every real deployment.
        """
        obs.reset_for_tests()
        obs.init(mode="console", force=True)
        tlm.track("stub", category="cat", metadata={"k": "v"})
        events = tlm.read_events()
        self.assertEqual([e["event"] for e in events], ["stub"])
        self.assertEqual(events[0]["category"], "cat")

    def test_shadow_buffer_disabled_via_env(self) -> None:
        import os
        obs.reset_for_tests()
        os.environ["YTFACTORY_TELEMETRY_BUFFER_DISABLE"] = "1"
        try:
            obs.init(mode="console", force=True)
            tlm.track("stub")
            self.assertEqual(tlm.read_events(), [])
        finally:
            os.environ.pop("YTFACTORY_TELEMETRY_BUFFER_DISABLE", None)
            obs.reset_for_tests()


class TestPercentile(unittest.TestCase):
    def test_empty_returns_zero(self) -> None:
        self.assertEqual(tlm.percentile([], 0.5), 0.0)

    def test_none_values_filtered(self) -> None:
        result = tlm.percentile([None, 1.0, 3.0], 0.5)  # type: ignore[arg-type]
        self.assertAlmostEqual(result, 2.0)

    def test_q_zero_returns_min(self) -> None:
        self.assertEqual(tlm.percentile([3.0, 1.0, 2.0], 0), 1.0)

    def test_q_one_returns_max(self) -> None:
        self.assertEqual(tlm.percentile([3.0, 1.0, 2.0], 1), 3.0)

    def test_q_half_interpolates(self) -> None:
        self.assertAlmostEqual(tlm.percentile([0.0, 10.0], 0.5), 5.0)

    def test_q_midpoint_odd_list(self) -> None:
        self.assertAlmostEqual(
            tlm.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5), 3.0,
        )

    def test_single_element(self) -> None:
        self.assertEqual(tlm.percentile([42.0], 0.5), 42.0)


if __name__ == "__main__":
    unittest.main()
