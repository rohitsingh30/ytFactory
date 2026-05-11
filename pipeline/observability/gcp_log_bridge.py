"""OTel log → Cloud Logging bridge.

Why a custom exporter exists: the OpenTelemetry Python ecosystem does
not (yet) ship an official Cloud Logging log exporter. The supported
patterns are:

1. **Stdout JSON** (Cloud Run, GKE, GCE) — write structured JSON to
   stderr; the platform's logging agent ships it. We use this in
   :mod:`pipeline.observability.exporters._build_gcp_log_processor`
   when ``K_SERVICE`` is set.

2. **Direct API write** (laptop / generic VM) — call the
   ``google-cloud-logging`` REST client directly. That's this file.
   We translate each :class:`opentelemetry.sdk._logs.LogRecord` into
   a :class:`google.cloud.logging.LogEntry` and call
   :meth:`Logger.log_struct`. Trace context is preserved so logs
   appear correlated with spans inside the Cloud Trace UI.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from opentelemetry.sdk._logs import LogData
from opentelemetry.sdk._logs.export import LogExporter, LogExportResult


_logger = logging.getLogger(__name__)


class CloudLoggingLogExporter(LogExporter):
    """Write OTel log records to Cloud Logging via the google-cloud-logging
    Python client.

    All records are written into a single log named
    ``ytfactory-telemetry`` so the dashboard's read path can filter
    cleanly. Severity is mapped from OTel severity numbers.
    """

    LOG_NAME = "ytfactory-telemetry"

    SEVERITY_MAP = {
        # OTel severity number → Cloud Logging severity string
        # (https://cloud.google.com/logging/docs/reference/v2/rest/v2/LogEntry#LogSeverity)
        1: "DEBUG", 2: "DEBUG", 3: "DEBUG", 4: "DEBUG",
        5: "INFO", 6: "INFO", 7: "INFO", 8: "INFO",
        9: "NOTICE", 10: "NOTICE", 11: "NOTICE", 12: "NOTICE",
        13: "WARNING", 14: "WARNING", 15: "WARNING", 16: "WARNING",
        17: "ERROR", 18: "ERROR", 19: "ERROR", 20: "ERROR",
        21: "CRITICAL", 22: "ALERT", 23: "EMERGENCY", 24: "EMERGENCY",
    }

    def __init__(self, project_id: Optional[str] = None) -> None:
        # Lazy-import so callers without google-cloud-logging installed
        # don't pay an import cost.
        import google.cloud.logging  # noqa: PLC0415
        self._client = google.cloud.logging.Client(project=project_id)
        self._cloud_logger = self._client.logger(self.LOG_NAME)

    # ------------------------------------------------------------ exporter API

    def export(self, batch: Sequence[LogData]) -> LogExportResult:
        """Best-effort write of every record. We never raise; failure
        logs locally and reports SUCCESS so the SDK doesn't retry the
        whole batch (we'd rather drop a record than block the pipeline).
        """
        for ld in batch:
            try:
                self._write_one(ld)
            except Exception as e:  # noqa: BLE001
                _logger.warning("Cloud Logging write failed: %s", e)
        return LogExportResult.SUCCESS

    def shutdown(self) -> None:
        """No-op — :class:`google.cloud.logging.Client` cleans itself up."""

    # ----------------------------------------------------------------- helpers

    def _write_one(self, ld: LogData) -> None:
        rec = ld.log_record
        body = rec.body
        struct: dict = {}
        if isinstance(body, dict):
            struct.update(body)
        elif body is not None:
            struct["message"] = str(body)
        if rec.attributes:
            struct.update(dict(rec.attributes))

        severity = self.SEVERITY_MAP.get(rec.severity_number.value, "DEFAULT")

        kwargs: dict = {"severity": severity}
        if rec.trace_id and rec.trace_id != 0:
            kwargs["trace"] = (
                f"projects/{self._client.project}/traces/"
                f"{format(rec.trace_id, '032x')}"
            )
        if rec.span_id and rec.span_id != 0:
            kwargs["span_id"] = format(rec.span_id, "016x")
        self._cloud_logger.log_struct(struct, **kwargs)


__all__ = ["CloudLoggingLogExporter"]
