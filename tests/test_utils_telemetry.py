"""100% line coverage for pipeline/utils/telemetry.py."""
from __future__ import annotations

import json
import sys
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import telemetry as tlm


def _make_scratch(base: Path) -> Path:
    return Path(tempfile.mkdtemp(dir=str(base)))


_BASE = Path(__file__).resolve().parent


class TestTodayPath(unittest.TestCase):
    def test_returns_jsonl_path_with_date(self) -> None:
        p = tlm._today_path()
        self.assertIsInstance(p, Path)
        self.assertTrue(p.name.startswith("events-"))
        self.assertTrue(p.name.endswith(".jsonl"))
        self.assertEqual(p.parent, tlm.TELEMETRY_DIR)


class _TelemetryBase(unittest.TestCase):
    """Set TELEMETRY_DIR to a private scratch dir for isolation."""

    def setUp(self) -> None:
        self._scratch = _make_scratch(_BASE)
        self._orig = tlm.TELEMETRY_DIR
        tlm.TELEMETRY_DIR = Path(self._scratch) / "telemetry"

    def tearDown(self) -> None:
        tlm.TELEMETRY_DIR = self._orig
        import shutil
        shutil.rmtree(self._scratch, ignore_errors=True)


class TestTrack(_TelemetryBase):
    def test_success_writes_record(self) -> None:
        tlm.track("ev", category="cat", success=True, duration_ms=99,
                  job_id="j1", metadata={"k": "v"})
        files = list(tlm.TELEMETRY_DIR.glob("events-*.jsonl"))
        self.assertEqual(len(files), 1)
        r = json.loads(files[0].read_text().splitlines()[0])
        self.assertEqual(r["event"], "ev")
        self.assertEqual(r["category"], "cat")
        self.assertTrue(r["success"])
        self.assertEqual(r["duration_ms"], 99)
        self.assertEqual(r["job_id"], "j1")
        self.assertEqual(r["metadata"], {"k": "v"})

    def test_defaults(self) -> None:
        tlm.track("minimal")
        files = list(tlm.TELEMETRY_DIR.glob("events-*.jsonl"))
        r = json.loads(files[0].read_text().splitlines()[0])
        self.assertEqual(r["category"], "pipeline")
        self.assertTrue(r["success"])
        self.assertIsNone(r["duration_ms"])
        self.assertIsNone(r["job_id"])
        self.assertEqual(r["metadata"], {})

    def test_success_false(self) -> None:
        tlm.track("fail", success=False)
        files = list(tlm.TELEMETRY_DIR.glob("events-*.jsonl"))
        r = json.loads(files[0].read_text().splitlines()[0])
        self.assertFalse(r["success"])

    def test_failure_prints_warning_and_does_not_raise(self) -> None:
        err = io.StringIO()
        with patch.object(tlm.TELEMETRY_DIR.__class__, "mkdir",
                          side_effect=OSError("disk full")):
            with patch("sys.stderr", err):
                tlm.track("bad_event")  # must not raise
        self.assertIn("write failed", err.getvalue())

    def test_multiple_writes_append(self) -> None:
        tlm.track("e1")
        tlm.track("e2")
        files = list(tlm.TELEMETRY_DIR.glob("events-*.jsonl"))
        lines = files[0].read_text().splitlines()
        self.assertEqual(len(lines), 2)


class TestTimer(_TelemetryBase):
    def test_add_merges_metadata(self) -> None:
        t = tlm._Timer("ev", category="test", job_id=None, metadata={"a": 1})
        t.add(metadata={"b": 2})
        t.end()
        files = list(tlm.TELEMETRY_DIR.glob("events-*.jsonl"))
        r = json.loads(files[0].read_text().splitlines()[0])
        self.assertEqual(r["metadata"], {"a": 1, "b": 2})

    def test_add_none_is_noop(self) -> None:
        t = tlm._Timer("ev", category="c", job_id=None, metadata={})
        t.add(metadata=None)
        t.end()  # should not crash

    def test_end_idempotent_only_one_record(self) -> None:
        t = tlm._Timer("ev", category="c", job_id=None, metadata={})
        t.end()
        t.end()  # second call: no-op
        files = list(tlm.TELEMETRY_DIR.glob("events-*.jsonl"))
        self.assertEqual(len(files[0].read_text().splitlines()), 1)

    def test_end_success_false_and_metadata(self) -> None:
        t = tlm._Timer("ev", category="c", job_id="j", metadata={})
        t.end(success=False, metadata={"err": "oops"})
        files = list(tlm.TELEMETRY_DIR.glob("events-*.jsonl"))
        r = json.loads(files[0].read_text().splitlines()[0])
        self.assertFalse(r["success"])
        self.assertEqual(r["metadata"]["err"], "oops")


class TestTimed(_TelemetryBase):
    def test_success_records_event(self) -> None:
        with tlm.timed("stage", category="test", job_id="j", metadata={"x": 1}) as t:
            t.add(metadata={"y": 2})
        files = list(tlm.TELEMETRY_DIR.glob("events-*.jsonl"))
        r = json.loads(files[0].read_text().splitlines()[0])
        self.assertTrue(r["success"])
        self.assertEqual(r["event"], "stage")
        self.assertIn("x", r["metadata"])
        self.assertIn("y", r["metadata"])

    def test_exception_records_failure_and_reraises(self) -> None:
        with self.assertRaises(ValueError):
            with tlm.timed("err_stage") as _t:
                raise ValueError("boom")
        files = list(tlm.TELEMETRY_DIR.glob("events-*.jsonl"))
        r = json.loads(files[0].read_text().splitlines()[0])
        self.assertFalse(r["success"])
        self.assertIn("ValueError", r["metadata"]["error"])

    def test_timed_no_metadata_kwarg(self) -> None:
        with tlm.timed("bare"):
            pass
        files = list(tlm.TELEMETRY_DIR.glob("events-*.jsonl"))
        self.assertTrue(files)


class TestReadEvents(_TelemetryBase):
    def test_no_dir_returns_empty(self) -> None:
        # TELEMETRY_DIR doesn't exist yet
        result = tlm.read_events()
        self.assertEqual(result, [])

    def test_reads_and_sorts_oldest_first(self) -> None:
        tlm.TELEMETRY_DIR.mkdir(parents=True)
        f = tlm.TELEMETRY_DIR / "events-2024-01-01.jsonl"
        f.write_text(
            json.dumps({"ts": 2.0, "event": "b"}) + "\n" +
            json.dumps({"ts": 1.0, "event": "a"}) + "\n"
        )
        result = tlm.read_events()
        self.assertEqual(result[0]["event"], "a")
        self.assertEqual(result[1]["event"], "b")

    def test_skips_malformed_json_lines(self) -> None:
        tlm.TELEMETRY_DIR.mkdir(parents=True)
        f = tlm.TELEMETRY_DIR / "events-2024-01-01.jsonl"
        f.write_text("not json\n" + json.dumps({"ts": 1.0, "event": "ok"}) + "\n")
        result = tlm.read_events()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["event"], "ok")

    def test_skips_empty_lines(self) -> None:
        tlm.TELEMETRY_DIR.mkdir(parents=True)
        f = tlm.TELEMETRY_DIR / "events-2024-01-01.jsonl"
        f.write_text("\n" + json.dumps({"ts": 1.0, "event": "ok"}) + "\n\n")
        result = tlm.read_events()
        self.assertEqual(len(result), 1)

    def test_since_ts_filters_old_events(self) -> None:
        tlm.TELEMETRY_DIR.mkdir(parents=True)
        f = tlm.TELEMETRY_DIR / "events-2024-01-01.jsonl"
        f.write_text(
            json.dumps({"ts": 1.0, "event": "old"}) + "\n" +
            json.dumps({"ts": 5.0, "event": "new"}) + "\n"
        )
        result = tlm.read_events(since_ts=3.0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["event"], "new")

    def test_limit_keeps_most_recent(self) -> None:
        tlm.TELEMETRY_DIR.mkdir(parents=True)
        f = tlm.TELEMETRY_DIR / "events-2024-01-01.jsonl"
        lines = [json.dumps({"ts": float(i), "event": f"e{i}"}) for i in range(5)]
        f.write_text("\n".join(lines) + "\n")
        result = tlm.read_events(limit=2)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[-1]["event"], "e4")

    def test_oserror_on_file_read_is_skipped(self) -> None:
        tlm.TELEMETRY_DIR.mkdir(parents=True)
        f = tlm.TELEMETRY_DIR / "events-2024-01-01.jsonl"
        f.write_text(json.dumps({"ts": 1.0}) + "\n")
        with patch("pathlib.Path.open", side_effect=OSError("perm")):
            result = tlm.read_events()
        self.assertEqual(result, [])

    def test_record_without_ts_key(self) -> None:
        tlm.TELEMETRY_DIR.mkdir(parents=True)
        f = tlm.TELEMETRY_DIR / "events-2024-01-01.jsonl"
        f.write_text(json.dumps({"event": "no_ts"}) + "\n")
        result = tlm.read_events(since_ts=0.0)
        # ts defaults to 0 in rec.get("ts", 0), so 0 < 0.0 is False → included
        self.assertEqual(len(result), 1)


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
        result = tlm.percentile([0.0, 10.0], 0.5)
        self.assertAlmostEqual(result, 5.0)

    def test_q_midpoint_odd_list(self) -> None:
        result = tlm.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5)
        self.assertAlmostEqual(result, 3.0)

    def test_single_element(self) -> None:
        self.assertEqual(tlm.percentile([42.0], 0.5), 42.0)


if __name__ == "__main__":
    unittest.main()
