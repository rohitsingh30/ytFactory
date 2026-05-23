"""Small best-effort telemetry helpers for render plugins.

Every helper in this module is intentionally non-throwing. Render stages use
these functions from hot paths where telemetry must never affect business
logic.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def enum_value(value: Any) -> Any:
    """Return ``Enum.value`` when present; otherwise the value itself."""
    return getattr(value, "value", value)


def current_job_id() -> str | None:
    """Best-effort render job id from the active context or environment."""
    try:
        from pipeline import observability as obs  # noqa: PLC0415

        job_id = getattr(obs.current_context(), "job_id", None)
        if job_id:
            return str(job_id)
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("YTFACTORY_JOB_ID") or os.environ.get("JOB_ID") or None


def track_event(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: int | None = None,
    metadata: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> None:
    """Emit a telemetry event without letting failures escape."""
    try:
        from pipeline import observability as obs  # noqa: PLC0415

        obs.track(
            event,
            category=category,
            success=success,
            duration_ms=duration_ms,
            job_id=job_id or current_job_id(),
            metadata=metadata or {},
        )
    except Exception:  # noqa: BLE001
        pass


def emit_json_artifact(
    kind: str,
    payload: Any,
    *,
    job_id: str | None = None,
    filename: str | None = None,
    extras: dict[str, Any] | None = None,
) -> str | None:
    """Upload a JSON telemetry artifact when a job id is available."""
    try:
        jid = job_id or current_job_id()
        if not jid:
            return None
        from pipeline.render.artifacts import emit_artifact_json  # noqa: PLC0415

        return emit_artifact_json(
            job_id=jid,
            kind=kind,
            data=payload,
            filename=filename,
            extras=extras,
        )
    except Exception:  # noqa: BLE001
        return None


def path_size(path: Path | str | None) -> int:
    """Return file size if the path exists, else 0."""
    if path is None:
        return 0
    try:
        p = Path(path)
        return p.stat().st_size if p.exists() else 0
    except Exception:  # noqa: BLE001
        return 0


def timeline_payload(timeline: list[Any]) -> list[dict[str, Any]]:
    """Serialise timing-relevant Segment fields for timeline artifacts."""
    beats: list[dict[str, Any]] = []
    for i, seg in enumerate(timeline or []):
        try:
            start_s = float(getattr(seg, "start_s", 0.0) or 0.0)
            end_s = float(getattr(seg, "end_s", start_s) or start_s)
            words = getattr(seg, "words", None) or []
            beats.append({
                "index": i,
                "start_s": start_s,
                "end_s": end_s,
                "duration_s": max(0.0, end_s - start_s),
                "anchor_id": getattr(seg, "anchor_id", None),
                "kind": getattr(seg, "kind", None),
                "text": getattr(seg, "text", ""),
                "word_count": len(words) if hasattr(words, "__len__") else 0,
            })
        except Exception:  # noqa: BLE001
            continue
    return beats


def timeline_total_seconds(timeline: list[Any], fallback: float | None = None) -> float:
    """Return max end time for a timeline, falling back to narration duration."""
    try:
        if timeline:
            return max(float(getattr(seg, "end_s", 0.0) or 0.0) for seg in timeline)
    except Exception:  # noqa: BLE001
        pass
    try:
        return float(fallback or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def emit_timeline_telemetry(
    timeline: list[Any],
    *,
    total_seconds: float | None = None,
    unanchored_count: int = 0,
    anchor_method: str,
) -> None:
    """Emit timeline.beat_map + timeline artifact for a built timeline."""
    payload = timeline_payload(timeline)
    total = timeline_total_seconds(timeline, total_seconds)
    metadata = {
        "beat_count": len(timeline or []),
        "total_seconds": total,
        "unanchored_count": int(unanchored_count),
        "anchor_method": anchor_method,
    }
    track_event("timeline.beat_map", category="pipeline", metadata=metadata)
    emit_json_artifact("timeline", {"beats": payload})


__all__ = [
    "current_job_id",
    "emit_json_artifact",
    "emit_timeline_telemetry",
    "enum_value",
    "path_size",
    "timeline_payload",
    "timeline_total_seconds",
    "track_event",
]
