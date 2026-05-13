"""Audit D3.69 + D3.70 — pipeline/observability/gcp_log_bridge.py.

D3.69: pre-fix `_write_one` did `rec.severity_number.value`,
which assumed the SeverityNumber enum surface. Older OTel
versions exposed the field as a plain int (no `.value`); the
AttributeError caught by `export()`'s blanket `except` showed
up as a generic "Cloud Logging write failed" warning with no
diagnostic context.

D3.70: pre-fix `export()` always returned SUCCESS even if every
record raised. The SDK's BatchLogRecordProcessor uses the export
result to decide whether to retry; SUCCESS-on-failure means real
cloud-side outages got silently swallowed. Now: if MORE THAN HALF
the batch failed, return FAILURE so the SDK retries.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from opentelemetry.sdk._logs.export import LogRecordExportResult


def _fake_record(severity_number, body="hello"):
    """Build a minimal ReadableLogRecord-like stub."""
    rec = MagicMock()
    rec.log_record.body = body
    rec.log_record.attributes = {"k": "v"}
    rec.log_record.severity_number = severity_number
    rec.log_record.trace_id = 0
    rec.log_record.span_id = 0
    return rec


class _StubExporter:
    """Construct the bridge without exercising google.cloud.logging."""

    def __new__(cls):
        from pipeline.observability import gcp_log_bridge as mod
        inst = mod.CloudLoggingLogExporter.__new__(
            mod.CloudLoggingLogExporter
        )
        inst._client = MagicMock()
        inst._client.project = "test-proj"
        inst._cloud_logger = MagicMock()
        inst.SEVERITY_MAP = mod.CloudLoggingLogExporter.SEVERITY_MAP
        return inst


class WriteOneSeverityNumberShapesTest(unittest.TestCase):
    """Audit D3.69 — handle both the new SeverityNumber enum surface
    AND the older plain-int surface."""

    def test_enum_surface_works(self):
        exporter = _StubExporter()
        # Mimic SeverityNumber.INFO (value=9 → NOTICE per the bridge's
        # severity mapping; OTel INFO = severity_number 9).
        sev = MagicMock()
        sev.value = 9
        rec = _fake_record(sev)
        exporter._write_one(rec)
        kw = exporter._cloud_logger.log_struct.call_args.kwargs
        self.assertEqual(kw["severity"], "NOTICE")

    def test_int_surface_works(self):
        exporter = _StubExporter()
        # Older OTel: severity_number is a plain int (e.g. 13 →
        # WARNING per the bridge's mapping).
        rec = _fake_record(13)
        exporter._write_one(rec)
        kw = exporter._cloud_logger.log_struct.call_args.kwargs
        self.assertEqual(kw["severity"], "WARNING")

    def test_unknown_severity_falls_back_to_default(self):
        exporter = _StubExporter()
        rec = _fake_record(999)  # unmapped severity
        exporter._write_one(rec)
        kw = exporter._cloud_logger.log_struct.call_args.kwargs
        self.assertEqual(kw["severity"], "DEFAULT")


class ExportPartialFailureTest(unittest.TestCase):
    """Audit D3.70 — export should return FAILURE when MORE THAN HALF
    the batch failed, so BatchLogRecordProcessor retries on transient
    Cloud Logging outages instead of silently dropping the batch."""

    def test_all_succeed_returns_success(self):
        exporter = _StubExporter()
        batch = [_fake_record(9) for _ in range(3)]
        result = exporter.export(batch)
        self.assertEqual(result, LogRecordExportResult.SUCCESS)

    def test_majority_fail_returns_failure(self):
        exporter = _StubExporter()
        # Make every log_struct call fail.
        exporter._cloud_logger.log_struct.side_effect = RuntimeError("cloud down")
        batch = [_fake_record(9) for _ in range(3)]
        result = exporter.export(batch)
        self.assertEqual(result, LogRecordExportResult.FAILURE)

    def test_minority_fail_returns_success(self):
        exporter = _StubExporter()
        # 1 of 3 fails — minority, still SUCCESS so SDK doesn't retry
        # (the failure is recorded locally + the other two landed).
        call_count = [0]
        def _maybe_fail(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("transient")
        exporter._cloud_logger.log_struct.side_effect = _maybe_fail
        batch = [_fake_record(9) for _ in range(3)]
        result = exporter.export(batch)
        self.assertEqual(result, LogRecordExportResult.SUCCESS)

    def test_empty_batch_returns_success(self):
        exporter = _StubExporter()
        result = exporter.export([])
        self.assertEqual(result, LogRecordExportResult.SUCCESS)

    def test_failure_does_not_raise(self):
        exporter = _StubExporter()
        exporter._cloud_logger.log_struct.side_effect = RuntimeError("boom")
        batch = [_fake_record(9)]
        # Must NOT raise — the exporter's contract is "best effort".
        result = exporter.export(batch)
        # 1 of 1 failed → fail > ok → FAILURE.
        self.assertEqual(result, LogRecordExportResult.FAILURE)


if __name__ == "__main__":
    unittest.main()
