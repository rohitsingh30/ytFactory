"""Tests for the exporter selection logic in
:mod:`pipeline.observability.exporters`.

The GCP exporter path is exercised by importing the module — actual
network I/O is reserved for P8's e2e smoke test.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from pipeline.observability import exporters


class TestResolveMode(unittest.TestCase):
    def setUp(self) -> None:
        # Snapshot env so test cases can mutate freely.
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_explicit_arg_wins(self) -> None:
        os.environ["OTEL_EXPORTER"] = "console"
        self.assertEqual(exporters.resolve_mode("inmemory"), "inmemory")

    def test_env_var_wins_over_auto(self) -> None:
        os.environ.pop("K_SERVICE", None)
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ["OTEL_EXPORTER"] = "console"
        self.assertEqual(exporters.resolve_mode(), "console")

    def test_invalid_env_falls_through_to_auto(self) -> None:
        os.environ["OTEL_EXPORTER"] = "garbage"
        os.environ["K_SERVICE"] = "tts-chatterbox"
        self.assertEqual(exporters.resolve_mode(), "gcp")

    def test_auto_cloud_run(self) -> None:
        os.environ.pop("OTEL_EXPORTER", None)
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ["K_SERVICE"] = "render-worker-v2"
        self.assertEqual(exporters.resolve_mode(), "gcp")

    def test_auto_pytest(self) -> None:
        os.environ.pop("OTEL_EXPORTER", None)
        os.environ.pop("K_SERVICE", None)
        os.environ["PYTEST_CURRENT_TEST"] = "x"
        self.assertEqual(exporters.resolve_mode(), "inmemory")

    def test_auto_laptop(self) -> None:
        os.environ.pop("OTEL_EXPORTER", None)
        os.environ.pop("K_SERVICE", None)
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        self.assertEqual(exporters.resolve_mode(), "console")


class TestBuildExporters(unittest.TestCase):
    def test_none_mode_returns_no_processors(self) -> None:
        b = exporters.build_exporters("none")
        self.assertIsNone(b.span_processor)
        self.assertIsNone(b.metric_reader)
        self.assertIsNone(b.log_processor)

    def test_inmemory_mode_attaches_inmemory_buffers(self) -> None:
        b = exporters.build_exporters("inmemory")
        self.assertIsNotNone(b.span_processor)
        self.assertIsNotNone(b.metric_reader_inmemory)
        self.assertIsNotNone(b.span_inmemory)
        self.assertIsNotNone(b.log_inmemory)

    def test_console_mode_builds_processors(self) -> None:
        b = exporters.build_exporters("console")
        self.assertIsNotNone(b.span_processor)
        self.assertIsNotNone(b.metric_reader)
        self.assertIsNotNone(b.log_processor)

    def test_console_mode_attaches_shadow_buffer(self) -> None:
        """Primary console log processor PLUS in-process shadow buffer.

        The dashboard's ``/api/telemetry/*`` routes read from the
        shadow buffer; without this the laptop dashboard was empty
        even after months of pipeline activity.
        """
        b = exporters.build_exporters("console")
        self.assertIsNotNone(b.log_processor, "primary still required")
        self.assertIsNotNone(b.shadow_log_processor)
        self.assertIsNotNone(b.log_inmemory)

    def test_otlp_mode_attaches_shadow_buffer(self) -> None:
        try:
            b = exporters.build_exporters("otlp")
        except ImportError:
            self.skipTest("otlp http exporter not installed")
        self.assertIsNotNone(b.shadow_log_processor)
        self.assertIsNotNone(b.log_inmemory)

    def test_none_mode_has_no_shadow_buffer(self) -> None:
        b = exporters.build_exporters("none")
        self.assertIsNone(b.shadow_log_processor)
        self.assertIsNone(b.log_inmemory)

    def test_shadow_buffer_disabled_via_env(self) -> None:
        os.environ["YTFACTORY_TELEMETRY_BUFFER_DISABLE"] = "1"
        try:
            b = exporters.build_exporters("console")
            self.assertIsNotNone(b.log_processor)
            self.assertIsNone(b.shadow_log_processor)
            self.assertIsNone(b.log_inmemory)
        finally:
            os.environ.pop("YTFACTORY_TELEMETRY_BUFFER_DISABLE", None)

    def test_bounded_buffer_drops_oldest(self) -> None:
        from opentelemetry.sdk._logs import ReadableLogRecord
        from opentelemetry.sdk._logs._internal import LogRecord
        from opentelemetry.sdk.resources import Resource
        exp = exporters.BoundedInMemoryLogRecordExporter(maxlen=3)
        resource = Resource.get_empty()
        for i in range(5):
            rec = ReadableLogRecord(
                log_record=LogRecord(body={"i": i}),
                resource=resource,
            )
            exp.export([rec])
        records = exp.get_finished_logs()
        self.assertEqual(len(records), 3)
        # Oldest two dropped → bodies are 2, 3, 4.
        self.assertEqual(
            [r.log_record.body["i"] for r in records],
            [2, 3, 4],
        )

    def test_bounded_buffer_safe_after_shutdown(self) -> None:
        from opentelemetry.sdk._logs import ReadableLogRecord
        from opentelemetry.sdk._logs._internal import LogRecord
        from opentelemetry.sdk.resources import Resource
        exp = exporters.BoundedInMemoryLogRecordExporter(maxlen=10)
        rec = ReadableLogRecord(
            log_record=LogRecord(body={"x": 1}),
            resource=Resource.get_empty(),
        )
        exp.export([rec])
        exp.shutdown()
        # Subsequent calls must be safe.
        self.assertEqual(len(exp.get_finished_logs()), 1)
        result = exp.export([rec])  # post-shutdown no-op, still SUCCESS
        from opentelemetry.sdk._logs.export import LogRecordExportResult
        self.assertEqual(result, LogRecordExportResult.SUCCESS)
        # Idempotent shutdown.
        exp.shutdown()

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            exporters.build_exporters("nonsense")

    def test_gcp_mode_builds_without_creds(self) -> None:
        # We're not on GCP and don't have ADC, but the exporter classes
        # should still construct without raising. Network I/O happens
        # later, lazily.
        #
        # That assumption only holds when SOMETHING — gcloud ADC, a GCP
        # metadata server, or a fake-creds env — provides credentials
        # to ``CloudTraceSpanExporter._create_default_client``. CI has
        # neither, so skip there. Laptop devs running ``gcloud auth
        # application-default login`` once still get coverage.
        try:
            import google.auth  # type: ignore[import-not-found]
            google.auth.default()
        except Exception as e:
            self.skipTest(f"no GCP ADC available — skipping gcp-mode build: {e}")

        b = exporters.build_exporters("gcp")
        self.assertEqual(b.mode, "gcp")
        self.assertIsNotNone(b.span_processor)
        # metric reader is best-effort (alpha exporter); may be None.
        # log processor falls through to console when not on Cloud Run.
        self.assertIsNotNone(b.log_processor)
        # Shadow buffer powers the in-process /api/telemetry/* dashboard
        # in cloud too (per-instance view; cross-instance still needs
        # Cloud Logging queries — see docs/telemetry.md).
        self.assertIsNotNone(b.shadow_log_processor)
        self.assertIsNotNone(b.log_inmemory)


if __name__ == "__main__":
    unittest.main()
