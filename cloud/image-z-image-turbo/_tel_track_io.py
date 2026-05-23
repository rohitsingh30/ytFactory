"""Tiny self-contained telemetry body logger for slim Cloud Run services."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Optional

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


def bodies_enabled() -> bool:
    env = os.environ.get("YTFACTORY_TELEMETRY_BODIES", "")
    if env.strip():
        return env.strip() not in ("0", "false", "False")
    return bool(os.environ.get("K_SERVICE") or os.environ.get("CLOUD_RUN_JOB"))


def redact_secrets(text: str) -> str:
    try:
        out = text
        for pat, repl in _REDACTION_PATTERNS:
            out = pat.sub(repl, out)
        return out
    except Exception:
        return text


def _normalise_body(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except Exception:
            return repr(value)
    return str(value)


def _sha256_short(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:16]


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


def _attrs(event: str, category: str, success: bool, duration_ms: Optional[int],
           job_id: Optional[str], metadata: dict[str, Any]) -> dict[str, Any]:
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
        attrs[f"ytfactory.meta.{k}"] = v if isinstance(v, (str, bool, int, float)) else str(v)[:1024]
    return attrs


def _emit_log(body: dict[str, Any], attrs: dict[str, Any]) -> None:
    try:
        from opentelemetry import _logs as _logs_api  # type: ignore
        sev = _logs_api.SeverityNumber.INFO if body.get("success", True) else _logs_api.SeverityNumber.ERROR
        rec = _logs_api.LogRecord(
            timestamp=time.time_ns(),
            observed_timestamp=time.time_ns(),
            severity_number=sev,
            severity_text=sev.name,
            body=body,
            attributes=attrs,
        )
        _logs_api.get_logger("ytfactory.event").emit(rec)
        return
    except Exception:
        pass
    payload: dict[str, Any] = {
        "severity": "INFO" if body.get("success", True) else "ERROR",
        "message": "ytfactory.event",
    }
    for k in ("event", "category", "success", "duration_ms", "job_id"):
        if body.get(k) is not None:
            payload[f"ytfactory.{k}"] = body[k]
    for k, v in (body.get("metadata") or {}).items():
        if v is not None:
            payload[f"ytfactory.meta.{k}"] = v
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


def track_io(event: str, *, category: str = "pipeline", success: bool = True,
             duration_ms: Optional[int] = None, job_id: Optional[str] = None,
             input_text: Any = None, output_text: Any = None,
             input_meta: Optional[dict[str, Any]] = None,
             output_meta: Optional[dict[str, Any]] = None,
             metadata: Optional[dict[str, Any]] = None) -> None:
    try:
        meta: dict[str, Any] = {}
        if metadata:
            meta.update(metadata)
        if input_meta:
            meta.update(input_meta)
        if output_meta:
            meta.update(output_meta)
        capture = bodies_enabled()
        meta.update(_body_fields("input", _normalise_body(input_text), capture_body=capture))
        meta.update(_body_fields("output", _normalise_body(output_text), capture_body=capture))
        body = {
            "event": event,
            "category": category,
            "success": bool(success),
            "duration_ms": duration_ms,
            "job_id": job_id,
            "metadata": meta,
        }
        _emit_log(body, _attrs(event, category, success, duration_ms, job_id, meta))
    except Exception:
        pass
