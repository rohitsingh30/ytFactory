"""Tests for the cross-service Cloud Logging reader.

The reader is the dashboard's production read path — in real cloud
deployments the in-process shadow buffer is structurally near-empty
(the web-server is a thin BFF; render-worker JOBs and TTS / image
Cloud Run services emit events from *different* processes). These
tests pin the contract end-to-end:

* ``_normalise_payload`` round-trips a Cloud-Run-shaped jsonPayload
  back into the legacy :func:`pipeline.telemetry.read_events` dict
  shape the dashboard expects.
* :class:`CloudLoggingEventReader` queries with the right filter,
  honours the time bound, caps result size, and never raises.
* The TTL cache de-dupes overlapping dashboard polls.
* Every failure mode degrades to ``None`` so the caller falls back
  to the in-process buffer rather than crashing.
"""
from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

from pipeline.observability import cloud_log_reader as clr


def _entry(payload: dict, ts: datetime | None = None) -> Any:
    obj = MagicMock()
    obj.payload = payload
    obj.timestamp = ts or datetime.now(tz=timezone.utc)
    return obj


class TestNormalisePayload(unittest.TestCase):
    def test_skips_payload_without_ytfactory_event(self) -> None:
        out = clr._normalise_payload({"severity": "INFO"}, time.time())
        self.assertIsNone(out)

    def test_round_trip_full_payload(self) -> None:
        payload = {
            "severity": "INFO",
            "ytfactory.event": "tts_synth",
            "ytfactory.category": "tts",
            "ytfactory.success": True,
            "ytfactory.duration_ms": 1240,
            "ytfactory.job_id": "j-123",
            "ytfactory.channel": "historyrecapped",
            "ytfactory.slug": "aita-001",
            "ytfactory.run_id": "r-9",
            "ytfactory.meta.provider": "cloudrun_chatterbox",
            "ytfactory.meta.chars": 1234,
            # Non-ytfactory.* keys must NOT pollute the metadata dict.
            "logging.googleapis.com/trace": "projects/p/traces/abc",
        }
        out = clr._normalise_payload(payload, 1700.5)
        self.assertEqual(out["event"], "tts_synth")
        self.assertEqual(out["category"], "tts")
        self.assertTrue(out["success"])
        self.assertEqual(out["duration_ms"], 1240)
        self.assertEqual(out["job_id"], "j-123")
        self.assertEqual(out["ts"], 1700.5)
        self.assertEqual(out["metadata"]["channel"], "historyrecapped")
        self.assertEqual(out["metadata"]["slug"], "aita-001")
        self.assertEqual(out["metadata"]["run_id"], "r-9")
        self.assertEqual(out["metadata"]["provider"], "cloudrun_chatterbox")
        self.assertEqual(out["metadata"]["chars"], 1234)
        # Non-ytfactory keys excluded.
        self.assertNotIn("logging.googleapis.com/trace", out["metadata"])

    def test_falsey_success_string_coerced(self) -> None:
        # If anything ever writes "false" instead of False, we still
        # mark the event as failed.
        payload = {
            "ytfactory.event": "x",
            "ytfactory.success": "false",
        }
        out = clr._normalise_payload(payload, 1.0)
        self.assertFalse(out["success"])

    def test_invalid_duration_dropped(self) -> None:
        payload = {
            "ytfactory.event": "x",
            "ytfactory.duration_ms": "not-a-number",
        }
        out = clr._normalise_payload(payload, 1.0)
        self.assertIsNone(out["duration_ms"])


class TestEntryAccessors(unittest.TestCase):
    def test_payload_from_object(self) -> None:
        e = MagicMock()
        e.payload = {"k": "v"}
        self.assertEqual(clr._entry_payload(e), {"k": "v"})

    def test_payload_from_rest_dict(self) -> None:
        self.assertEqual(
            clr._entry_payload({"jsonPayload": {"k": "v"}}),
            {"k": "v"},
        )

    def test_payload_missing(self) -> None:
        # A text-payload entry has no jsonPayload at all.
        e = MagicMock(spec=["timestamp"])
        e.timestamp = datetime.now(tz=timezone.utc)
        self.assertIsNone(clr._entry_payload(e))

    def test_timestamp_from_datetime(self) -> None:
        e = MagicMock()
        e.timestamp = datetime(2026, 5, 12, 13, 0, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            clr._entry_timestamp_seconds(e),
            e.timestamp.timestamp(),
            places=3,
        )

    def test_timestamp_from_iso_string(self) -> None:
        e = {"timestamp": "2026-05-12T13:00:00Z"}
        self.assertAlmostEqual(
            clr._entry_timestamp_seconds(e),
            datetime(2026, 5, 12, 13, 0, 0, tzinfo=timezone.utc).timestamp(),
            places=3,
        )


class TestCloudLoggingEventReader(unittest.TestCase):
    def setUp(self) -> None:
        clr.reset_for_tests()

    def tearDown(self) -> None:
        clr.reset_for_tests()

    def _make_reader(self, entries):
        client = MagicMock()
        client.list_entries = MagicMock(return_value=iter(entries))
        return (
            clr.CloudLoggingEventReader(
                project_id="ytfactory-test",
                ttl_s=60.0,
                max_records=1000,
                client_factory=lambda: client,
            ),
            client,
        )

    def test_returns_normalised_events(self) -> None:
        entries = [
            _entry(
                {
                    "ytfactory.event": "tts_synth",
                    "ytfactory.category": "tts",
                    "ytfactory.success": True,
                    "ytfactory.duration_ms": 1240,
                    "ytfactory.channel": "historyrecapped",
                    "ytfactory.meta.provider": "cloudrun_chatterbox",
                },
                ts=datetime.now(tz=timezone.utc),
            ),
            _entry(
                {
                    "ytfactory.event": "image_gen",
                    "ytfactory.category": "image",
                    "ytfactory.success": False,
                    "ytfactory.channel": "historyrecapped",
                },
                ts=datetime.now(tz=timezone.utc),
            ),
        ]
        reader, client = self._make_reader(entries)
        events = reader.read(since_ts=time.time() - 3600)
        self.assertEqual(len(events), 2)
        names = {e["event"] for e in events}
        self.assertEqual(names, {"tts_synth", "image_gen"})
        client.list_entries.assert_called_once()

    def test_filter_includes_jsonpayload_field_and_timestamp(self) -> None:
        reader, client = self._make_reader([])
        reader.read(since_ts=1700000000.0)
        kwargs = client.list_entries.call_args.kwargs
        filter_ = kwargs["filter_"]
        self.assertIn('jsonPayload."ytfactory.event"!=""', filter_)
        self.assertIn('resource.type="cloud_run_revision"', filter_)
        self.assertRegex(filter_, r'timestamp >= "[0-9TZ:\-]+"')
        self.assertEqual(kwargs["order_by"], "timestamp desc")

    def test_drops_entries_older_than_since_ts(self) -> None:
        old = _entry(
            {"ytfactory.event": "old"},
            ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        new = _entry(
            {"ytfactory.event": "new"},
            ts=datetime.now(tz=timezone.utc),
        )
        reader, _ = self._make_reader([old, new])
        events = reader.read(since_ts=time.time() - 3600)
        self.assertEqual([e["event"] for e in events], ["new"])

    def test_skips_non_ytfactory_jsonpayload(self) -> None:
        # Cloud Logging may surface other JSON-payload entries — third-
        # party libs sometimes use structured logging too. Without a
        # ytfactory.event key we MUST skip them silently.
        unrelated = _entry({"severity": "INFO", "message": "lib log"})
        ours = _entry(
            {"ytfactory.event": "ours"},
            ts=datetime.now(tz=timezone.utc),
        )
        reader, _ = self._make_reader([unrelated, ours])
        events = reader.read(since_ts=time.time() - 3600)
        self.assertEqual([e["event"] for e in events], ["ours"])

    def test_returns_none_on_client_failure(self) -> None:
        client = MagicMock()
        client.list_entries = MagicMock(
            side_effect=RuntimeError("API down"),
        )
        reader = clr.CloudLoggingEventReader(
            project_id="p",
            client_factory=lambda: client,
        )
        events = reader.read(since_ts=time.time() - 60)
        self.assertIsNone(events)

    def test_ttl_cache_skips_second_query(self) -> None:
        entries = [
            _entry(
                {"ytfactory.event": "x"},
                ts=datetime.now(tz=timezone.utc),
            )
        ]
        reader, client = self._make_reader(entries)
        # First call hits Cloud Logging.
        reader.read(since_ts=1700000000.0)
        # Second call same window → served from cache, NO new API call.
        reader.read(since_ts=1700000000.0)
        self.assertEqual(client.list_entries.call_count, 1)

    def test_invalidate_clears_cache(self) -> None:
        entries = [
            _entry(
                {"ytfactory.event": "x"},
                ts=datetime.now(tz=timezone.utc),
            )
        ]
        reader, client = self._make_reader(entries)
        reader.read(since_ts=1700000000.0)
        reader.invalidate()
        # Re-arm the iterator (consumed by the first call).
        client.list_entries = MagicMock(return_value=iter(entries))
        reader.read(since_ts=1700000000.0)
        self.assertEqual(client.list_entries.call_count, 1)  # the new mock

    def test_caps_result_size(self) -> None:
        entries = [
            _entry(
                {"ytfactory.event": f"e{i}"},
                ts=datetime.now(tz=timezone.utc),
            )
            for i in range(20)
        ]
        reader = clr.CloudLoggingEventReader(
            project_id="p",
            max_records=5,
            client_factory=lambda: MagicMock(
                list_entries=MagicMock(return_value=iter(entries)),
            ),
        )
        events = reader.read(since_ts=time.time() - 60)
        self.assertEqual(len(events), 5)


class TestGetReader(unittest.TestCase):
    def setUp(self) -> None:
        self._env = dict(os.environ)
        clr.reset_for_tests()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        clr.reset_for_tests()

    def test_returns_none_without_project(self) -> None:
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        os.environ.pop("GCP_PROJECT", None)
        self.assertIsNone(clr.get_reader())

    def test_disable_env_returns_none(self) -> None:
        os.environ["GOOGLE_CLOUD_PROJECT"] = "p"
        os.environ["YTFACTORY_TELEMETRY_CLOUDLOG_DISABLE"] = "1"
        self.assertIsNone(clr.get_reader())

    def test_singleton_per_process(self) -> None:
        # Inject a stub that bypasses the lazy google-cloud-logging
        # import path so we don't depend on the package in tests that
        # never actually call read().
        import pipeline.observability.cloud_log_reader as mod
        original = mod.CloudLoggingEventReader

        class _FakeReader(mod.CloudLoggingEventReader):
            def _get_client(self):  # type: ignore[override]
                return MagicMock()

        mod.CloudLoggingEventReader = _FakeReader
        try:
            os.environ["GOOGLE_CLOUD_PROJECT"] = "p"
            r1 = clr.get_reader()
            r2 = clr.get_reader()
            self.assertIs(r1, r2)
        finally:
            mod.CloudLoggingEventReader = original


if __name__ == "__main__":
    unittest.main()
