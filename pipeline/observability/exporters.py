"""Exporter selection for the OTel SDK.

Resolves the trio (span exporter, metric reader, log exporter) from the
single env var ``OTEL_EXPORTER`` (with reasonable fallbacks so the dev
laptop and the test suite both work without GCP creds).

Modes:

- ``gcp`` (default in cloud) — Cloud Trace + Cloud Monitoring + Cloud
  Logging. Requires ADC / a service account with the right roles. The
  Cloud Monitoring exporter is *alpha* upstream (1.12.0a0) so we wrap
  it in a try/except and fall back to no metric exporter if it cannot
  be constructed (spans + logs still flow).
- ``otlp`` — OTLP-HTTP exporter, e.g. for a self-hosted collector.
- ``console`` — pretty-print to stdout (laptop dev convenience).
- ``inmemory`` — in-process queues. Used by the test suite + by
  :func:`pipeline.observability.read_recent` when the dashboard needs
  a single shared buffer.
- ``none`` — no-op (zero overhead). Useful for one-shot scripts that
  do not want to pay any cost for telemetry.

When ``OTEL_EXPORTER`` is unset:

- If we are running on Cloud Run (``K_SERVICE`` set) → ``gcp``.
- If pytest is running (``PYTEST_CURRENT_TEST`` set) → ``inmemory``.
- Otherwise → ``console`` (laptop dev).

This keeps every developer / CI / cloud surface working out-of-the-box
with no extra env to remember.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogRecordExporter,
    InMemoryLogRecordExporter,
    LogExporter,
)
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    InMemoryMetricReader,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


_logger = logging.getLogger(__name__)


# ---- mode resolution ---------------------------------------------------


VALID_MODES = ("gcp", "otlp", "console", "inmemory", "none")


def resolve_mode(explicit: Optional[str] = None) -> str:
    """Pick the exporter mode based on env / runtime."""
    raw = (explicit or os.environ.get("OTEL_EXPORTER") or "").strip().lower()
    if raw in VALID_MODES:
        return raw
    if raw:
        _logger.warning(
            "OTEL_EXPORTER=%r is not one of %s; falling back to auto",
            raw, VALID_MODES,
        )
    if os.environ.get("K_SERVICE"):
        return "gcp"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return "inmemory"
    return "console"


# ---- exporter bundle ---------------------------------------------------


@dataclass
class ExporterBundle:
    """Built-out exporters for the three signal types.

    Any field may be ``None`` — for ``mode='none'`` everything is None;
    for ``gcp`` the metric reader may be None if the alpha exporter
    fails to construct.

    The InMemory references are exposed (``span_inmemory``,
    ``log_inmemory``, ``metric_reader_inmemory``) so the test suite
    can read raw data back without poking at the providers.
    """

    mode: str
    span_processor: Optional[SpanProcessor]
    metric_reader: Optional[MetricReader]
    log_processor: Optional[BatchLogRecordProcessor]

    span_inmemory: Optional[InMemorySpanExporter] = None
    log_inmemory: Optional[InMemoryLogExporter] = None
    metric_reader_inmemory: Optional[InMemoryMetricReader] = None


def build_exporters(mode: str) -> ExporterBundle:
    """Build the exporter bundle for ``mode``.

    Caller is responsible for attaching the bundle to the providers
    (see :mod:`pipeline.observability.otel`). This helper is split out
    so tests can swap modes without re-running the full provider
    init dance.
    """
    if mode == "none":
        return ExporterBundle(mode, None, None, None)

    if mode == "inmemory":
        span_exp = InMemorySpanExporter()
        log_exp = InMemoryLogRecordExporter()
        reader = InMemoryMetricReader()
        return ExporterBundle(
            mode,
            SimpleSpanProcessor(span_exp),
            reader,
            BatchLogRecordProcessor(log_exp),
            span_inmemory=span_exp,
            log_inmemory=log_exp,
            metric_reader_inmemory=reader,
        )

    if mode == "console":
        return ExporterBundle(
            mode,
            SimpleSpanProcessor(ConsoleSpanExporter()),
            PeriodicExportingMetricReader(
                ConsoleMetricExporter(),
                export_interval_millis=60_000,
            ),
            BatchLogRecordProcessor(ConsoleLogRecordExporter()),
        )

    if mode == "otlp":
        # Lazy-import so ``otlp`` mode is opt-in on environments that
        # do not have the http exporter installed.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )
        return ExporterBundle(
            mode,
            BatchSpanProcessor(OTLPSpanExporter()),
            PeriodicExportingMetricReader(
                OTLPMetricExporter(),
                export_interval_millis=60_000,
            ),
            BatchLogRecordProcessor(OTLPLogExporter()),
        )

    if mode == "gcp":
        return _build_gcp()

    # Defensive — resolve_mode should never let us get here.
    raise ValueError(f"unknown OTel mode: {mode}")


def _build_gcp() -> ExporterBundle:
    """Cloud Trace + Cloud Monitoring + Cloud Logging.

    The metric exporter is alpha upstream so we wrap it; on failure
    we still keep traces + logs flowing (the dashboard's metric tab
    will show "unconfigured" until the exporter stabilises).
    """
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get(
        "GCP_PROJECT"
    )

    span_exp: SpanExporter = CloudTraceSpanExporter(project_id=project)
    span_processor: SpanProcessor = BatchSpanProcessor(span_exp)

    metric_reader: Optional[MetricReader] = None
    try:
        from opentelemetry.exporter.cloud_monitoring import (
            CloudMonitoringMetricsExporter,
        )
        metric_reader = PeriodicExportingMetricReader(
            CloudMonitoringMetricsExporter(project_id=project),
            export_interval_millis=60_000,
        )
    except Exception as e:  # noqa: BLE001
        _logger.warning(
            "Cloud Monitoring exporter unavailable (alpha?); metrics "
            "will not be exported: %s", e,
        )

    log_processor = _build_gcp_log_processor(project)
    return ExporterBundle(
        "gcp",
        span_processor=span_processor,
        metric_reader=metric_reader,
        log_processor=log_processor,
    )


def _build_gcp_log_processor(
    project: Optional[str],
) -> Optional[BatchLogRecordProcessor]:
    """Bridge OTel logs into Cloud Logging.

    The official OTel→Cloud Logging exporter is not yet a single PyPI
    package; the supported pattern is ``google-cloud-logging``'s
    :class:`google.cloud.logging.handlers.StructuredLogHandler` which
    writes JSON to stderr that Cloud Run / GKE / GCE picks up natively.
    Inside Cloud Run we therefore prefer ``ConsoleLogExporter`` because
    Cloud Run already collects stdout/stderr into Cloud Logging.

    If the caller wants per-record API writes instead (laptop without
    Cloud Run), we use ``CloudLoggingExporter`` from the
    ``google-cloud-logging`` integration.
    """
    if os.environ.get("K_SERVICE"):
        return BatchLogRecordProcessor(ConsoleLogRecordExporter())
    try:
        from .gcp_log_bridge import CloudLoggingLogExporter
        exp: LogExporter = CloudLoggingLogExporter(project_id=project)
        return BatchLogRecordProcessor(exp)
    except Exception as e:  # noqa: BLE001
        _logger.warning(
            "Cloud Logging exporter unavailable, falling back to console: %s",
            e,
        )
        return BatchLogRecordProcessor(ConsoleLogRecordExporter())


__all__ = [
    "ExporterBundle",
    "VALID_MODES",
    "build_exporters",
    "resolve_mode",
]
