"""Self-contained body telemetry helper for the cloud ASR service.

Cloud service images intentionally do not import ``pipeline.*``. This file
mirrors ``pipeline.observability.bodies.track_io`` closely enough for service
side request logging while staying dependency-light and best-effort.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Optional

_LOGGER = logging.getLogger("ytfactory.event")
TEL_BODY_MAX_CHARS = int(os.environ.get("YTFACTORY_TEL_BODY_MAX_CHARS", "4000"))


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
        return env not in ("0", "false", "False")
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


def _body_fields(side: str, raw: Optional[str], *, capture_body: bool) -> dict[str, Any]:
    if raw is None:
        return {}
    redacted = redact_secrets(raw)
    out: dict[str, Any] = {
        f"{side}_chars": len(redacted),
        f"{side}_sha256": _sha256_short(redacted),
    }
    if capture_body:
        out[f"{side}_preview"] = redacted[:TEL_BODY_MAX_CHARS]
        out[f"{side}_truncated"] = len(redacted) > TEL_BODY_MAX_CHARS
    return out


def _attrs(event: str, category: str, success: bool, duration_ms: Optional[int], job_id: Optional[str], metadata: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "ytfactory.event": event,
        "ytfactory.category": category,
        "ytfactory.success": bool(success),
    }
    if duration_ms is not None:
        attrs["ytfactory.duration_ms"] = int(duration_ms)
    if job_id:
        attrs["ytfactory.job_id"] = job_id
    for k, v in (metadata or {}).items():
        if isinstance(v, (str, bool, int, float)):
            attrs[f"ytfactory.meta.{k}"] = v
        elif v is not None:
            attrs[f"ytfactory.meta.{k}"] = str(v)[:1024]
    return attrs


def _emit(body: dict[str, Any], attrs: dict[str, Any]) -> None:
    try:
        from opentelemetry import _logs as _logs_api  # type: ignore
        rec = _logs_api.LogRecord(
            timestamp=time.time_ns(),
            observed_timestamp=time.time_ns(),
            severity_number=(
                _logs_api.SeverityNumber.INFO
                if body.get("success")
                else _logs_api.SeverityNumber.ERROR
            ),
            severity_text="INFO" if body.get("success") else "ERROR",
            body=body,
            attributes=attrs,
        )
        _logs_api.get_logger("ytfactory.event").emit(rec)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        fallback = {"severity": "INFO" if body.get("success") else "ERROR", **body}
        print(json.dumps(fallback, default=str), flush=True)
    except Exception:  # noqa: BLE001
        try:
            _LOGGER.info("ytfactory.event %s", body)
        except Exception:  # noqa: BLE001
            pass


def track_io(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: Optional[int] = None,
    job_id: Optional[str] = None,
    input_text: Any = None,
    output_text: Any = None,
    input_meta: Optional[dict[str, Any]] = None,
    output_meta: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    try:
        meta: dict[str, Any] = {}
        if metadata:
            meta.update(metadata)
        if input_meta:
            meta.update(input_meta)
        if output_meta:
            meta.update(output_meta)
        capture_body = bodies_enabled()
        meta.update(_body_fields("input", _normalise_body(input_text), capture_body=capture_body))
        meta.update(_body_fields("output", _normalise_body(output_text), capture_body=capture_body))
        body = {
            "event": event,
            "category": category,
            "success": bool(success),
            "duration_ms": duration_ms,
            "job_id": job_id,
            "metadata": meta,
        }
        _emit(body, _attrs(event, category, success, duration_ms, job_id, meta))
    except Exception:  # noqa: BLE001
        pass


__all__ = ["TEL_BODY_MAX_CHARS", "bodies_enabled", "redact_secrets", "track_io"]
