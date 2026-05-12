"""Cloud Run-native structured JSON OTel log exporter — copied per service.

This module is the SELF-CONTAINED twin of
``pipeline/observability/cloud_run_json_exporter.py``. It lives next
to ``otel_init.py`` and is copied into every Cloud Run service's
container image by ``cloud/_shared/sync.sh`` so the slim per-service
images don't have to depend on the full ``pipeline/`` package.

Why a duplicate? Because changing this also requires
``bash cloud/_shared/sync.sh`` + a redeploy. Keeping the two files in
lockstep is enforced by ``tests/test_obs_cloud_run_json_exporter.py``.

Format contract (one JSON object per stdout line):

.. code-block:: json

    {
      "severity": "INFO",
      "message": "ytfactory.event",
      "ytfactory.event": "tts_synth",
      "ytfactory.category": "tts",
      "ytfactory.success": true,
      "ytfactory.duration_ms": 1240,
      "ytfactory.channel": "historyrecapped",
      "ytfactory.slug": "aita-001",
      "ytfactory.meta.provider": "cloudrun_chatterbox",
      "logging.googleapis.com/trace": "projects/<proj>/traces/<trace_id>",
      "logging.googleapis.com/spanId": "<span_id>"
    }

Cloud Run promotes any stdout JSON line containing a ``severity`` key
to a ``jsonPayload`` Cloud Logging entry. The dashboard's
:class:`pipeline.observability.cloud_log_reader.CloudLoggingEventReader`
then queries those records cross-service to populate
``/api/telemetry/*``.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from typing import Any, Dict, Optional, Sequence, TextIO

from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import (
    LogExporter,
    LogRecordExportResult,
)


_logger = logging.getLogger(__name__)


_SEVERITY_MAP: Dict[int, str] = {
    1: "DEBUG", 2: "DEBUG", 3: "DEBUG", 4: "DEBUG",
    5: "INFO", 6: "INFO", 7: "INFO", 8: "INFO",
    9: "NOTICE", 10: "NOTICE", 11: "NOTICE", 12: "NOTICE",
    13: "WARNING", 14: "WARNING", 15: "WARNING", 16: "WARNING",
    17: "ERROR", 18: "ERROR", 19: "ERROR", 20: "ERROR",
    21: "CRITICAL", 22: "ALERT", 23: "EMERGENCY", 24: "EMERGENCY",
}


def _severity_string(rec_sev_number: Any) -> str:
    try:
        n = rec_sev_number.value if hasattr(rec_sev_number, "value") else int(rec_sev_number)
    except Exception:  # noqa: BLE001
        return "DEFAULT"
    return _SEVERITY_MAP.get(int(n), "DEFAULT")


def _coerce_value(v: Any) -> Any:
    if v is None or isinstance(v, (str, bool, int, float)):
        return v
    if isinstance(v, (list, tuple)):
        return [_coerce_value(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _coerce_value(val) for k, val in v.items()}
    s = str(v)
    return s if len(s) <= 4096 else s[:4096] + "…"


def _flatten_body_metadata(body: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not isinstance(body, dict):
        return out
    for k, v in body.items():
        if k == "metadata":
            continue
        if v is None:
            continue
        if k in ("event", "category", "success", "duration_ms", "job_id"):
            out[f"ytfactory.{k}"] = _coerce_value(v)
        else:
            out[k] = _coerce_value(v)
    md = body.get("metadata") or {}
    if isinstance(md, dict):
        for k, v in md.items():
            if v is None:
                continue
            out[f"ytfactory.meta.{k}"] = _coerce_value(v)
    return out


def render_log_record(
    rec: ReadableLogRecord,
    *,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    log_record = rec.log_record if hasattr(rec, "log_record") else rec
    body = log_record.body
    payload: Dict[str, Any] = {
        "severity": _severity_string(getattr(log_record, "severity_number", None)),
    }
    payload.update(_flatten_body_metadata(body))
    if isinstance(body, dict):
        msg = "ytfactory.event"
    elif body is None:
        msg = "ytfactory.event"
    else:
        msg = str(body)
        if len(msg) > 4096:
            msg = msg[:4096] + "…"
    payload["message"] = msg
    attrs = getattr(log_record, "attributes", None) or {}
    for k, v in attrs.items():
        if not isinstance(k, str):
            continue
        if not k.startswith("ytfactory."):
            continue
        payload.setdefault(k, _coerce_value(v))

    trace_id = getattr(log_record, "trace_id", 0) or 0
    span_id = getattr(log_record, "span_id", 0) or 0
    if trace_id:
        if project_id:
            payload["logging.googleapis.com/trace"] = (
                f"projects/{project_id}/traces/{format(trace_id, '032x')}"
            )
        else:
            payload["logging.googleapis.com/trace"] = format(trace_id, "032x")
    if span_id:
        payload["logging.googleapis.com/spanId"] = format(span_id, "016x")
    return payload


class CloudRunStructuredJsonLogExporter(LogExporter):
    """OTel log exporter writing one Cloud-Run-shaped JSON line per
    record to stdout. Defensive on every operation.
    """

    def __init__(
        self,
        *,
        stream: Optional[TextIO] = None,
        project_id: Optional[str] = None,
    ) -> None:
        self._project_id = (
            project_id
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCP_PROJECT")
        )
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()
        self._stopped = False
        self.export_count = 0
        self.dropped_count = 0

    def export(
        self,
        batch: Sequence[ReadableLogRecord],
    ) -> LogRecordExportResult:
        if self._stopped:
            return LogRecordExportResult.SUCCESS
        for rec in batch:
            try:
                payload = render_log_record(rec, project_id=self._project_id)
                line = json.dumps(payload, ensure_ascii=False, default=str)
                with self._lock:
                    self._stream.write(line)
                    self._stream.write("\n")
                    self._stream.flush()
                self.export_count += 1
            except Exception as e:  # noqa: BLE001
                self.dropped_count += 1
                _logger.debug(
                    "CloudRunStructuredJsonLogExporter dropped record: %s", e,
                )
        return LogRecordExportResult.SUCCESS

    def shutdown(self) -> None:
        self._stopped = True

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        try:
            with self._lock:
                self._stream.flush()
        except Exception:  # noqa: BLE001
            return False
        return True


__all__ = [
    "CloudRunStructuredJsonLogExporter",
    "render_log_record",
]
