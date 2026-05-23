"""Small best-effort helpers for telemetry call sites.

These wrappers centralise the "telemetry must never break the pipeline"
contract for modules that are not themselves part of the observability
package.
"""
from __future__ import annotations

from typing import Any, MutableMapping


def safe_track(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: int | None = None,
    job_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        from pipeline import observability as obs  # noqa: PLC0415
        obs.track(
            event,
            category=category,
            success=success,
            duration_ms=duration_ms,
            job_id=job_id,
            metadata=metadata or {},
        )
    except Exception:  # noqa: BLE001
        pass


def safe_track_io(
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
        from pipeline.observability.bodies import track_io  # noqa: PLC0415
        track_io(
            event,
            category=category,
            success=success,
            duration_ms=duration_ms,
            job_id=job_id,
            input_text=input_text,
            output_text=output_text,
            input_meta=input_meta,
            output_meta=output_meta,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001
        pass


def inject_trace_headers(headers: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Best-effort W3C trace-context injection into outbound HTTP headers."""
    try:
        from pipeline.observability import propagation  # noqa: PLC0415
        propagation.inject_into_dict(headers)
    except Exception:  # noqa: BLE001
        pass
    return headers


def short_hash(body: Any) -> str | None:
    try:
        from pipeline.observability.bodies import hash_full  # noqa: PLC0415
        return hash_full(body)
    except Exception:  # noqa: BLE001
        return None


def track_http_call(
    *,
    service: str,
    method: str,
    url: str,
    status_code: int | None = None,
    request_body: Any = None,
    response_body: Any = None,
    duration_ms: int | None = None,
    success: bool | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    meta: dict[str, Any] = {
        "service": service,
        "method": method.upper(),
        "url": url,
    }
    if status_code is not None:
        meta["status_code"] = status_code
    req_hash = short_hash(request_body)
    resp_hash = short_hash(response_body)
    if req_hash:
        meta["request_sha256"] = req_hash
    if resp_hash:
        meta["response_sha256"] = resp_hash
    if metadata:
        meta.update(metadata)
    ok = success if success is not None else (
        status_code is None or 200 <= int(status_code) < 400
    )
    safe_track(
        "http.call",
        category="http",
        success=bool(ok),
        duration_ms=duration_ms,
        metadata=meta,
    )


def trace_id_from_traceparent(traceparent: str | None) -> str | None:
    if not traceparent:
        return None
    try:
        parts = traceparent.strip().split("-")
        if len(parts) >= 4 and len(parts[1]) == 32:
            return parts[1]
    except Exception:  # noqa: BLE001
        return None
    return None


__all__ = [
    "inject_trace_headers",
    "safe_track",
    "safe_track_io",
    "short_hash",
    "trace_id_from_traceparent",
    "track_http_call",
]
