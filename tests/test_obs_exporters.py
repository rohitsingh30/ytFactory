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


if __name__ == "__main__":
    unittest.main()
