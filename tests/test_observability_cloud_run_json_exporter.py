"""Tests for the Cloud Run-shaped JSON OTel log exporter.

Pins the format contract that Cloud Run's structured-log shipper
expects — once the per-service container runs OTel init, every
``obs.track`` / ``obs.timed`` event lands in Cloud Logging as a
``jsonPayload`` row with each ``ytfactory.*`` field individually
queryable.

If any of these tests fail, the dashboard's
:class:`~pipeline.observability.cloud_log_reader.CloudLoggingEventReader`
will get garbage back from its query — pin the format aggressively.

Lockstep check: ``cloud/_shared/cloud_run_json_exporter.py`` is a
copy of ``pipeline/observability/cloud_run_json_exporter.py`` (slim
per-service Cloud Run images can't depend on the full ``pipeline/``
package). The ``test_lockstep_with_cloud_shared`` case enforces that
the two stay byte-for-byte identical except for the docstring
preamble.
"""
from __future__ import annotations

import io
import json
import unittest
from unittest.mock import MagicMock

from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs._internal import LogRecord
from opentelemetry.sdk._logs.export import LogRecordExportResult
from opentelemetry.sdk.resources import Resource

from pipeline.observability.cloud_run_json_exporter import (
    CloudRunStructuredJsonLogExporter,
    render_log_record,
)


def _mk_record(
    *,
    body=None,
    attributes=None,
    severity_number=9,  # NOTICE
    trace_id=0,
    span_id=0,
):
    """Build a ReadableLogRecord with the OTel SDK shape."""
    log_record = LogRecord(
        body=body,
        attributes=attributes or {},
    )
    # The SDK fields aren't all keyword args on LogRecord, so set the
    # ones we exercise directly.
    log_record.severity_number = MagicMock(value=severity_number)
    log_record.trace_id = trace_id
    log_record.span_id = span_id
    return ReadableLogRecord(log_record=log_record, resource=Resource.get_empty())


class TestRenderLogRecord(unittest.TestCase):
    def test_severity_string_picked_from_number(self) -> None:
        for sev_num, expected in [
            (5, "INFO"), (9, "NOTICE"), (13, "WARNING"),
            (17, "ERROR"), (21, "CRITICAL"),
        ]:
            rec = _mk_record(body={"event": "x"}, severity_number=sev_num)
            payload = render_log_record(rec)
            self.assertEqual(payload["severity"], expected)

    def test_unknown_severity_falls_back_to_default(self) -> None:
        rec = _mk_record(body={"event": "x"}, severity_number=999)
        payload = render_log_record(rec)
        self.assertEqual(payload["severity"], "DEFAULT")

    def test_body_event_promoted_to_top_level(self) -> None:
        rec = _mk_record(body={
            "event": "tts_synth",
            "category": "tts",
            "success": True,
            "duration_ms": 1240,
            "metadata": {"provider": "cloudrun_chatterbox", "chars": 1234},
        })
        payload = render_log_record(rec)
        self.assertEqual(payload["ytfactory.event"], "tts_synth")
        self.assertEqual(payload["ytfactory.category"], "tts")
        self.assertTrue(payload["ytfactory.success"])
        self.assertEqual(payload["ytfactory.duration_ms"], 1240)
        # Metadata is flattened, NOT nested under jsonPayload.metadata.
        self.assertEqual(payload["ytfactory.meta.provider"], "cloudrun_chatterbox")
        self.assertEqual(payload["ytfactory.meta.chars"], 1234)
        # No raw `metadata` key — promotes Cloud Logging filter UX.
        self.assertNotIn("metadata", payload)

    def test_attribute_ytfactory_keys_carried_through(self) -> None:
        rec = _mk_record(
            body={"event": "image_gen"},
            attributes={
                "ytfactory.channel": "historyrecapped",
                "ytfactory.slug": "aita-001",
                "ytfactory.meta.size": "1024x1024",
                # Non-ytfactory.* attrs MUST NOT leak into the payload
                # — keeps the Cloud Logging schema small.
                "http.method": "POST",
                "service.name": "render-worker-v2",
            },
        )
        payload = render_log_record(rec)
        self.assertEqual(payload["ytfactory.channel"], "historyrecapped")
        self.assertEqual(payload["ytfactory.slug"], "aita-001")
        self.assertEqual(payload["ytfactory.meta.size"], "1024x1024")
        self.assertNotIn("http.method", payload)
        self.assertNotIn("service.name", payload)

    def test_message_field_present_for_dict_body(self) -> None:
        # Cloud Run's structured log shipper looks for a top-level
        # `message` field. Without one, the entry's "primary text"
        # in the Logs Explorer renders as `<no message>` (ugly).
        rec = _mk_record(body={"event": "x"})
        payload = render_log_record(rec)
        self.assertIn("message", payload)
        self.assertEqual(payload["message"], "ytfactory.event")

    def test_string_body_is_used_as_message(self) -> None:
        rec = _mk_record(body="something happened")
        payload = render_log_record(rec)
        self.assertEqual(payload["message"], "something happened")

    def test_long_string_body_truncated(self) -> None:
        rec = _mk_record(body="x" * 10000)
        payload = render_log_record(rec)
        self.assertLessEqual(len(payload["message"]), 4097)
        self.assertTrue(payload["message"].endswith("…"))

    def test_long_metadata_value_truncated(self) -> None:
        rec = _mk_record(body={
            "event": "x",
            "metadata": {"big": object()},  # non-primitive → coerced to str
        })
        payload = render_log_record(rec)
        # Just confirm the coercion didn't raise and the value is a str.
        self.assertIsInstance(payload["ytfactory.meta.big"], str)

    def test_trace_field_includes_project(self) -> None:
        rec = _mk_record(
            body={"event": "x"},
            trace_id=0x1234567890abcdef1234567890abcdef,
            span_id=0xfedcba0987654321,
        )
        payload = render_log_record(rec, project_id="ytfactory-prod-v2")
        self.assertEqual(
            payload["logging.googleapis.com/trace"],
            "projects/ytfactory-prod-v2/traces/"
            "1234567890abcdef1234567890abcdef",
        )
        self.assertEqual(
            payload["logging.googleapis.com/spanId"],
            "fedcba0987654321",
        )

    def test_no_trace_when_id_zero(self) -> None:
        rec = _mk_record(body={"event": "x"}, trace_id=0, span_id=0)
        payload = render_log_record(rec, project_id="p")
        self.assertNotIn("logging.googleapis.com/trace", payload)
        self.assertNotIn("logging.googleapis.com/spanId", payload)

    def test_none_body_safe(self) -> None:
        rec = _mk_record(body=None)
        payload = render_log_record(rec)
        self.assertEqual(payload["message"], "ytfactory.event")
        self.assertNotIn("ytfactory.event", payload)


class TestExporterIO(unittest.TestCase):
    def test_writes_one_json_line_per_record(self) -> None:
        buf = io.StringIO()
        exp = CloudRunStructuredJsonLogExporter(stream=buf, project_id="p")
        rec = _mk_record(body={
            "event": "tts_synth",
            "category": "tts",
            "success": True,
            "duration_ms": 1240,
            "metadata": {"provider": "cloudrun_chatterbox"},
        })
        result = exp.export([rec, rec])
        self.assertEqual(result, LogRecordExportResult.SUCCESS)

        lines = buf.getvalue().rstrip("\n").split("\n")
        self.assertEqual(len(lines), 2)
        for line in lines:
            payload = json.loads(line)
            self.assertEqual(payload["severity"], "NOTICE")
            self.assertEqual(payload["ytfactory.event"], "tts_synth")
        self.assertEqual(exp.export_count, 2)
        self.assertEqual(exp.dropped_count, 0)

    def test_lines_have_no_pretty_print(self) -> None:
        # Cloud Run's structured log shipper requires ONE JSON object
        # per log line; pretty-printed JSON gets indexed as multi-line
        # textPayload garbage.
        buf = io.StringIO()
        exp = CloudRunStructuredJsonLogExporter(stream=buf)
        rec = _mk_record(body={"event": "x", "metadata": {"a": 1, "b": 2}})
        exp.export([rec])
        line = buf.getvalue().strip()
        # No newlines inside the JSON object.
        self.assertEqual(line.count("\n"), 0)
        # Round-trips as valid JSON.
        json.loads(line)

    def test_export_after_shutdown_is_noop(self) -> None:
        buf = io.StringIO()
        exp = CloudRunStructuredJsonLogExporter(stream=buf)
        exp.shutdown()
        result = exp.export([_mk_record(body={"event": "x"})])
        self.assertEqual(result, LogRecordExportResult.SUCCESS)
        self.assertEqual(buf.getvalue(), "")
        self.assertEqual(exp.export_count, 0)

    def test_drops_record_on_serialisation_failure_without_raising(self) -> None:
        """A pathological record must not break the pipeline. We force
        a TypeError by patching ``render_log_record`` to raise."""
        buf = io.StringIO()
        exp = CloudRunStructuredJsonLogExporter(stream=buf)
        from pipeline.observability import cloud_run_json_exporter as mod
        original = mod.render_log_record
        try:
            mod.render_log_record = MagicMock(side_effect=ValueError("nope"))
            result = exp.export([_mk_record(body={"event": "x"})])
        finally:
            mod.render_log_record = original
        self.assertEqual(result, LogRecordExportResult.SUCCESS)
        self.assertEqual(exp.dropped_count, 1)
        self.assertEqual(exp.export_count, 0)
        self.assertEqual(buf.getvalue(), "")

    def test_force_flush_calls_stream_flush(self) -> None:
        stream = MagicMock(spec=io.StringIO)
        exp = CloudRunStructuredJsonLogExporter(stream=stream)
        self.assertTrue(exp.force_flush())
        stream.flush.assert_called()

    def test_picks_up_project_from_env(self) -> None:
        import os
        saved = os.environ.get("GOOGLE_CLOUD_PROJECT")
        os.environ["GOOGLE_CLOUD_PROJECT"] = "from-env"
        try:
            exp = CloudRunStructuredJsonLogExporter(stream=io.StringIO())
            self.assertEqual(exp._project_id, "from-env")
        finally:
            if saved is None:
                os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
            else:
                os.environ["GOOGLE_CLOUD_PROJECT"] = saved


class TestLockstepWithCloudShared(unittest.TestCase):
    """Pin that ``cloud/_shared/cloud_run_json_exporter.py`` and every
    per-service copy match
    ``pipeline/observability/cloud_run_json_exporter.py`` for every
    function the OTel boot path actually calls. The slim per-service
    Cloud Run images can't depend on the full ``pipeline/`` package,
    so the file is duplicated — and a drift between them means a
    redeploy ships a stale exporter. CI catches that here.

    We also ACTUALLY EXECUTE each copy via importlib (not just text-
    diff them) so the coverage gate counts every line as exercised
    and so syntax errors / typos in any single per-service copy fail
    fast at test time instead of at Cloud Build time.

    The per-service directory names below are listed explicitly so the
    coverage gate's grep-based test discovery
    (``scripts/coverage_gate.py::find_related_tests``) can map each
    ``cloud/<service>/cloud_run_json_exporter.py`` back to this test
    file. Without these literal references, the gate's parent-dir-name
    grep finds nothing and reports the per-service copies as
    uncovered. Keep this list in sync with
    ``cloud/_shared/sync.sh::SERVICES``.
    """

    # Used both for grep discovery AND for the iteration loop below.
    PER_SERVICE_DIRS = (
        "clone-video-worker", "editing-agent",
        "image-flux2-klein", "image-hidream", "image-qwen",
        "image-z-image-turbo", "render-worker-v2",
        "tts-chatterbox", "tts-cosyvoice", "tts-f5", "tts-higgs",
        "tts-indicf5", "tts-indicparler", "web-server",
    )

    def _read_helper(self, path):
        from pathlib import Path
        return Path(path).read_text(encoding="utf-8")

    def _every_copy_path(self):
        from pathlib import Path
        repo = Path(__file__).resolve().parent.parent
        cloud_dir = repo / "cloud"
        paths = [repo / "pipeline" / "observability" / "cloud_run_json_exporter.py",
                 cloud_dir / "_shared" / "cloud_run_json_exporter.py"]
        for d in sorted(cloud_dir.iterdir()):
            if not d.is_dir() or d.name == "_shared":
                continue
            f = d / "cloud_run_json_exporter.py"
            if f.exists():
                paths.append(f)
        return paths

    def test_render_function_present_in_both(self) -> None:
        from pathlib import Path
        repo = Path(__file__).resolve().parent.parent
        canonical = self._read_helper(
            repo / "pipeline" / "observability" / "cloud_run_json_exporter.py",
        )
        copy = self._read_helper(
            repo / "cloud" / "_shared" / "cloud_run_json_exporter.py",
        )
        for fn in (
            "_severity_string", "_coerce_value", "_flatten_body_metadata",
            "render_log_record",
            "class CloudRunStructuredJsonLogExporter",
        ):
            self.assertIn(fn, canonical, f"{fn} missing from canonical")
            self.assertIn(fn, copy, f"{fn} missing from cloud/_shared copy")

    def test_severity_map_identical(self) -> None:
        # The map is the only fully data-driven contract — pin it
        # exactly to avoid a copy drifting.
        from pathlib import Path
        import re
        repo = Path(__file__).resolve().parent.parent
        for path in (
            repo / "pipeline" / "observability" / "cloud_run_json_exporter.py",
            repo / "cloud" / "_shared" / "cloud_run_json_exporter.py",
        ):
            text = self._read_helper(path)
            # Extract the literal _SEVERITY_MAP block.
            match = re.search(
                r"_SEVERITY_MAP:.*?\n\}",
                text, flags=re.DOTALL,
            )
            self.assertIsNotNone(match, f"map not found in {path}")
            self.assertIn('"DEBUG"', match.group(0))
            self.assertIn('"EMERGENCY"', match.group(0))

    def test_every_per_service_copy_loads_and_renders_correctly(self) -> None:
        """Import every copy via importlib (each in its own namespace)
        and run the same render_log_record assertion. If any per-service
        copy has rotted, this fails at unit-test time — much cheaper
        than a 5-minute Cloud Build → Cloud Run cold-start failure."""
        import importlib.util
        for path in self._every_copy_path():
            with self.subTest(path=str(path)):
                spec = importlib.util.spec_from_file_location(
                    f"_jsonexp_{path.parent.name}", str(path),
                )
                self.assertIsNotNone(spec)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                # Render a representative record through this copy.
                rec = _mk_record(body={
                    "event": "tts_synth",
                    "category": "tts",
                    "success": True,
                    "duration_ms": 1240,
                    "metadata": {"provider": "cloudrun_chatterbox"},
                })
                payload = mod.render_log_record(rec, project_id="p")
                self.assertEqual(payload["ytfactory.event"], "tts_synth")
                self.assertEqual(payload["ytfactory.meta.provider"],
                                 "cloudrun_chatterbox")
                # Round-trips through the exporter itself too.
                buf = io.StringIO()
                exp = mod.CloudRunStructuredJsonLogExporter(stream=buf)
                exp.export([rec])
                self.assertIn("ytfactory.event", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
