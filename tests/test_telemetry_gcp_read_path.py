"""Pin the gcp-mode read path in ``pipeline.telemetry.read_events``.

The web-server in production runs OTel in ``gcp`` mode but only sees
its own emissions in the in-process buffer. ``read_events`` MUST
consult the cross-service Cloud Logging reader before falling back to
the empty-by-design buffer — without this, the ``/api/telemetry/*``
dashboard would render zeros forever.

These tests mock the Cloud Logging client (no network) and pin every
branch:

* gcp mode + reader returns events → events flow through.
* gcp mode + reader returns ``None`` (failure) → falls back to
  in-process buffer.
* gcp mode + reader disabled via env → falls back to in-process
  buffer.
* non-gcp modes (inmemory / console) → reader is NEVER consulted.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest.mock import MagicMock, patch

from pipeline import observability as obs
from pipeline import telemetry as tlm
from pipeline.observability import cloud_log_reader as clr
from pipeline.observability.exporters import ExporterBundle


class _GcpModeBase(unittest.TestCase):
    """Force ``read_events`` to take the gcp branch by patching the
    bundle returned by ``current_bundle()`` to claim ``mode='gcp'``.
    Avoids needing real GCP creds in tests."""

    def setUp(self) -> None:
        self._env_snap = dict(os.environ)
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()
        # Mutate the bundle in place so all downstream callers see the
        # gcp mode without rebuilding providers.
        self.bundle.mode = "gcp"
        clr.reset_for_tests()
        os.environ["GOOGLE_CLOUD_PROJECT"] = "ytfactory-test"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_snap)
        obs.reset_for_tests()
        clr.reset_for_tests()


class TestReadEventsGcpMode(_GcpModeBase):
    def test_reader_events_flow_through(self) -> None:
        fake_events = [{
            "ts": time.time(),
            "event": "tts_synth",
            "category": "tts",
            "success": True,
            "duration_ms": 1240,
            "job_id": None,
            "metadata": {"channel": "historyrecapped",
                         "provider": "cloudrun_chatterbox"},
        }]
        fake_reader = MagicMock()
        fake_reader.read = MagicMock(return_value=fake_events)

        with patch.object(clr, "get_reader", return_value=fake_reader):
            events = tlm.read_events(since_ts=0.0)

        self.assertEqual(events, fake_events)
        fake_reader.read.assert_called_once()

    def test_reader_failure_falls_back_to_inprocess_buffer(self) -> None:
        # First seed the in-process buffer so we can prove the fallback.
        with obs.timed("local_event", category="local"):
            pass

        fake_reader = MagicMock()
        # Reader returns None → caller MUST fall back.
        fake_reader.read = MagicMock(return_value=None)

        with patch.object(clr, "get_reader", return_value=fake_reader):
            events = tlm.read_events(since_ts=0.0)

        names = [e["event"] for e in events]
        self.assertIn("local_event", names)

    def test_reader_disabled_env_falls_back(self) -> None:
        os.environ["YTFACTORY_TELEMETRY_CLOUDLOG_DISABLE"] = "1"
        with obs.timed("local_only", category="local"):
            pass
        events = tlm.read_events(since_ts=0.0)
        self.assertIn("local_only", [e["event"] for e in events])

    def test_no_project_falls_back(self) -> None:
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        os.environ.pop("GCP_PROJECT", None)
        with obs.timed("local_no_project", category="local"):
            pass
        events = tlm.read_events(since_ts=0.0)
        self.assertIn("local_no_project", [e["event"] for e in events])

    def test_limit_applied_to_reader_results(self) -> None:
        many = [
            {
                "ts": float(i),
                "event": f"e{i}",
                "category": "x",
                "success": True,
                "duration_ms": None,
                "job_id": None,
                "metadata": {},
            }
            for i in range(20)
        ]
        fake_reader = MagicMock()
        fake_reader.read = MagicMock(return_value=many)
        with patch.object(clr, "get_reader", return_value=fake_reader):
            events = tlm.read_events(since_ts=0.0, limit=5)
        self.assertEqual(len(events), 5)
        # Most-recent five (legacy semantics).
        self.assertEqual(events[0]["event"], "e15")
        self.assertEqual(events[-1]["event"], "e19")


class TestReadEventsNonGcpModes(unittest.TestCase):
    def setUp(self) -> None:
        obs.reset_for_tests()
        self.bundle = obs.init_in_memory()  # mode='inmemory'
        clr.reset_for_tests()

    def tearDown(self) -> None:
        obs.reset_for_tests()
        clr.reset_for_tests()

    def test_inmemory_mode_never_consults_reader(self) -> None:
        with obs.timed("imem_event", category="x"):
            pass
        with patch.object(clr, "get_reader") as get_reader_mock:
            events = tlm.read_events(since_ts=0.0)
        get_reader_mock.assert_not_called()
        self.assertIn("imem_event", [e["event"] for e in events])


if __name__ == "__main__":
    unittest.main()
