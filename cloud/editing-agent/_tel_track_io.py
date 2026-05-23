"""Self-contained telemetry body capture for the editing-agent service.

Cloud service images must not import ``pipeline.observability``. This file is a
small local twin of the render telemetry helpers: structured events are emitted
through the already-initialised OTel logger when available, with JSON-stdout
fallback handled by Cloud Run logging.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Optional

TEL_BODY_MAX_CHARS = int(os.environ.get("YTFACTORY_TEL_BODY_MAX_CHARS", "4000"))
EVENT_LOG_NAME = "ytfactory.event"

_LOG = logging.getLogger("editing-agent.telemetry")

_REDACTION_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "[REDACTED:sk-ant]"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "[REDACTED:sk]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "[REDACTED:google-api-key]"),
    (re.compile(r"ya29\.[A-Za-z0-9_-]{20,}"), "[REDACTED:oauth-access-token]"),
    (re.compile(r"api[-_]key=[A-Za-z0-9_-]{16,}", re.IGNORECASE), "api_key=[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED:jwt]"),
]


def _is_cloud_run() -> bool:
    return bool(os.environ.get("K_SERVICE") or os.environ.get("CLOUD_RUN_JOB"))


def bodies_enabled() -> bool:
    env = os.environ.get("YTFACTORY_TELEMETRY_BODIES", "").strip()
    if env:
        return env not in {"0", "false", "False"}
    return _is_cloud_run()


def redact_secrets(text: str) -> str:
    try:
        out = text
        for pat, repl in _REDACTION_PATTERNS:
            out = pat.sub(repl, out)
        return out
    except Exception:  # noqa: BLE001
        return text


def _normalise_body(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return repr(value)
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            return repr(value)
    return str(value)


def _sha256_short(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:16]


def hash_full(text: Any) -> Optional[str]:
    norm = _normalise_body(text)
    if norm is None:
        return None
    return _sha256_short(redact_secrets(norm))


def _body_fields(side: str, raw: Optional[str], *, capture_body: bool) -> dict[str, Any]:
    if raw is None:
        return {}
    redacted = redact_secrets(raw)
    full_chars = len(redacted)
    out: dict[str, Any] = {
        f"{side}_chars": full_chars,
        f"{side}_sha256": _sha256_short(redacted),
    }
    if capture_body:
        out[f"{side}_preview"] = redacted[:TEL_BODY_MAX_CHARS]
        out[f"{side}_truncated"] = full_chars > TEL_BODY_MAX_CHARS
    return out


def _attrs(event: str, category: str, success: bool, duration_ms: int | None,
           job_id: str | None, metadata: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "ytfactory.event": event,
        "ytfactory.category": category,
        "ytfactory.success": bool(success),
    }
    if duration_ms is not None:
        attrs["ytfactory.duration_ms"] = int(duration_ms)
    if job_id:
        attrs["ytfactory.job_id"] = job_id
    for k, v in metadata.items():
        if v is None:
            continue
        if isinstance(v, (str, bool, int, float)):
            attrs[f"ytfactory.meta.{k}"] = v
        else:
            attrs[f"ytfactory.meta.{k}"] = str(v)[:1024]
    return attrs


def track(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: int | None = None,
    job_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        md = metadata or {}
        body = {
            "event": event,
            "category": category,
            "success": bool(success),
            "duration_ms": duration_ms,
            "job_id": job_id,
            "metadata": md,
        }
        try:
            from opentelemetry import _logs as _logs_api  # noqa: PLC0415

            severity = (
                _logs_api.SeverityNumber.INFO
                if success else _logs_api.SeverityNumber.ERROR
            )
            rec = _logs_api.LogRecord(
                timestamp=time.time_ns(),
                observed_timestamp=time.time_ns(),
                severity_number=severity,
                severity_text=severity.name,
                body=body,
                attributes=_attrs(event, category, success, duration_ms, job_id, md),
            )
            _logs_api.get_logger(EVENT_LOG_NAME).emit(rec)
        except Exception:  # noqa: BLE001
            _LOG.info("ytfactory.event %s", json.dumps(body, default=str))
    except Exception:  # noqa: BLE001
        pass


def track_io(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: int | None = None,
    job_id: str | None = None,
    input_text: Any = None,
    output_text: Any = None,
    input_meta: dict[str, Any] | None = None,
    output_meta: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        capture_body = bodies_enabled()
        md: dict[str, Any] = {}
        if metadata:
            md.update(metadata)
        if input_meta:
            md.update(input_meta)
        if output_meta:
            md.update(output_meta)
        md.update(_body_fields("input", _normalise_body(input_text), capture_body=capture_body))
        md.update(_body_fields("output", _normalise_body(output_text), capture_body=capture_body))
        track(
            event,
            category=category,
            success=success,
            duration_ms=duration_ms,
            job_id=job_id,
            metadata=md,
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "TEL_BODY_MAX_CHARS",
    "bodies_enabled",
    "hash_full",
    "redact_secrets",
    "track",
    "track_io",
]
