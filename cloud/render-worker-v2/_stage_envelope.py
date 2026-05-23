"""``@stage_envelope`` — wrap a worker stage function with structured telemetry.

Why this exists
---------------

Every ``_stage_*`` function in ``cloud/render-worker-v2/entrypoint.py``
needs the same wrapping:

* emit a ``stage.start`` event with job_id, slug, channel, variant,
  inputs summary,
* run the handler under an ``obs.timed`` span,
* on success: emit ``stage.end`` with outputs summary + duration,
* on fail: emit ``stage.failed`` with a structured error reason +
  full traceback in the event metadata,
* upload (best-effort) the stage's primary artifact to GCS via
  :func:`pipeline.render.artifacts.emit_artifact_json` so a
  post-mortem can read the state without grepping Cloud Logs,
* append the start/end records to the in-process
  :class:`EventsBuffer` so the upload stage can ship them as
  ``telemetry/events.jsonl``.

Doing this inline at each of the 13 stages = 130 lines of copy-paste
that drift. A decorator makes the diff one line per stage.

Usage
-----

::

    from cloud_run_v2._stage_envelope import stage_envelope

    @stage_envelope("rewrite", artifact_kind="script",
                    artifact_extractor=lambda job: job.get("_script_path"))
    def _stage_rewrite_real(job: dict, work_dir: Path) -> None:
        ...

The decorator:

* Looks up ``YTFACTORY_JOB_ID`` (or ``job["job_id"]``) for telemetry.
* Reads ``job["proposal"]["channel"]`` and ``["format"]`` for context.
* Calls the wrapped handler.
* Reads ``job["_slug"]``, ``job["_script_path"]``, etc. after the
  call to populate the ``stage.end`` event.

The ``artifact_kind`` + ``artifact_extractor`` are optional shortcuts
for stages that produce a single canonical file; stages that produce
multiple artifacts call ``emit_artifact_json`` directly inside their
body and skip these kwargs.

Failure semantics
-----------------

The decorator NEVER swallows the wrapped function's exception — if a
stage fails, the worker still raises. The decorator just guarantees
the failure is observable.
"""
from __future__ import annotations

import functools
import inspect
import logging
import os
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Lazy imports: pipeline.observability + pipeline.render.artifacts may
# not be importable from every test environment that imports this
# module (e.g. unit tests that don't have google-cloud-storage).
def _obs():
    from pipeline import observability as _o  # noqa: PLC0415
    return _o


def _artifacts():
    from pipeline.render import artifacts as _a  # noqa: PLC0415
    return _a


# ---------------------------------------------------------------------
# EventsBuffer — append every emitted event to /workdir/telemetry/events.jsonl
# ---------------------------------------------------------------------


class EventsBuffer:
    """Process-singleton sink for telemetry events.

    Every ``track`` / ``track_io`` call also lands here (via the
    :class:`_LoggingHandler` installed by :func:`install_events_buffer`).
    The upload stage flushes the buffer to
    ``gs://.../jobs/<job_id>/telemetry/events.jsonl`` so a post-mortem
    sees the full chronological event stream — even if Cloud Logging
    retention has expired.

    Bounded at :data:`MAX_EVENTS` (default 20 000) to defend against
    runaway pipelines spamming millions of events. When the cap is
    hit, oldest entries are dropped and a ``_truncated`` marker is
    added to the next emit.
    """
    MAX_EVENTS = int(os.environ.get("YTFACTORY_EVENTS_BUFFER_MAX", "20000"))

    _instance: "EventsBuffer | None" = None

    @classmethod
    def instance(cls) -> "EventsBuffer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._truncated_count = 0

    def append(self, rec: dict[str, Any]) -> None:
        try:
            if len(self._events) >= self.MAX_EVENTS:
                self._events.pop(0)
                self._truncated_count += 1
                if self._truncated_count == 1:
                    self._events.insert(0, {
                        "_event": "events_buffer.truncated",
                        "ts": time.time(),
                        "max": self.MAX_EVENTS,
                    })
            item = dict(rec)
            item.setdefault("ts", time.time())
            self._events.append(item)
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._events)

    def flush_to_disk(self, path: Path) -> Path | None:
        """Write the buffer as jsonl. Returns the path or None on failure."""
        import json as _json  # noqa: PLC0415
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fp:
                for ev in self._events:
                    fp.write(_json.dumps(ev, default=str) + "\n")
            return path
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventsBuffer flush failed: %s", exc)
            return None


class _EventsLoggingHandler(logging.Handler):
    """Mirrors ``pipeline.observability`` events into the EventsBuffer.

    The OpenTelemetry log bridge already emits structured records
    through Python's stdlib ``logging`` (via the otel-log-bridge in
    ``pipeline.observability.gcp_log_bridge``). We attach this handler
    to root so EVERY ``track`` / ``track_io`` call gets a second copy
    appended to the in-process buffer.

    Only records with ``ytfactory.event`` in their attribute-ish body
    are captured — random unrelated INFO/DEBUG noise is filtered.
    """
    def emit(self, record: logging.LogRecord) -> None:
        try:
            body = getattr(record, "msg", None)
            if isinstance(body, dict) and "event" in body:
                rec = {
                    "ts": getattr(record, "created", time.time()),
                    "level": record.levelname,
                    **body,
                }
                EventsBuffer.instance().append(rec)
        except Exception:  # noqa: BLE001
            pass


_events_handler_installed = False


def install_events_buffer() -> None:
    """Attach :class:`_EventsLoggingHandler` to root. Idempotent."""
    global _events_handler_installed
    if _events_handler_installed:
        return
    try:
        handler = _EventsLoggingHandler()
        handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(handler)
        _events_handler_installed = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("install_events_buffer failed: %s", exc)


# ---------------------------------------------------------------------
# @stage_envelope decorator
# ---------------------------------------------------------------------


def _job_context(job: dict[str, Any]) -> dict[str, Any]:
    """Pull job_id / slug / channel / variant for telemetry attrs."""
    proposal = job.get("proposal") or {}
    return {
        "job_id": job.get("job_id") or os.environ.get("YTFACTORY_JOB_ID") or "",
        "slug": job.get("_slug") or "",
        "channel": proposal.get("channel") or "",
        "variant": (proposal.get("format") or "").strip() or "",
        "mode": (job.get("mode") or "real"),
    }


def stage_envelope(
    stage_name: str,
    *,
    artifact_kind: Optional[str] = None,
    artifact_extractor: Optional[Callable[[dict[str, Any]], Any]] = None,
    artifact_extras_extractor: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
) -> Callable:
    """Wrap a stage function with start/end/failed telemetry events.

    Parameters
    ----------
    stage_name
        Logical stage name. Becomes ``ytfactory.stage`` on every event.
    artifact_kind
        Optional :class:`KNOWN_KINDS` member. If set, on stage success
        we look up ``artifact_extractor(job)`` and upload its result
        (file path → ``emit_artifact``, dict/list → ``emit_artifact_json``).
    artifact_extractor
        Callable that returns either a file path (str/Path) or a
        JSON-serialisable dict/list extracted from the job dict
        after the handler ran. Required if ``artifact_kind`` is set.
    artifact_extras_extractor
        Optional callable returning a dict of metadata merged into the
        Firestore artifact record (e.g. ``{n_beats: 12, hook: "..."}``).
    """
    def decorate(fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        @functools.wraps(fn)
        def wrapper(job: dict[str, Any], work_dir: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
            ctx = _job_context(job)
            t0 = time.perf_counter()
            try:
                _obs().track(
                    "stage.start",
                    category="render",
                    job_id=ctx["job_id"],
                    metadata={
                        "stage": stage_name,
                        "slug": ctx["slug"],
                        "channel": ctx["channel"],
                        "variant": ctx["variant"],
                        "mode": ctx["mode"],
                    },
                )
            except Exception:  # noqa: BLE001
                pass

            try:
                # Hand the call off — note `args/kwargs` propagated so
                # stages that take extra optional kwargs (e.g.
                # `_stage_render_real(..., progress_cb=...)`) still work.
                result = fn(job, work_dir, *args, **kwargs)
            except BaseException as exc:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                tb = traceback.format_exc()
                try:
                    _obs().track(
                        "stage.failed",
                        category="render",
                        success=False,
                        duration_ms=duration_ms,
                        job_id=ctx["job_id"],
                        metadata={
                            "stage": stage_name,
                            "slug": ctx["slug"],
                            "channel": ctx["channel"],
                            "variant": ctx["variant"],
                            "error": str(exc)[:500],
                            "error_type": type(exc).__name__,
                            "traceback": tb[-3000:],
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
                raise

            duration_ms = int((time.perf_counter() - t0) * 1000)
            # Best-effort artifact upload AFTER the handler succeeded.
            artifact_uri: str | None = None
            artifact_summary: dict[str, Any] = {}
            if artifact_kind and artifact_extractor:
                try:
                    payload = artifact_extractor(job)
                    if payload is None:
                        pass  # nothing produced — fine, just no upload
                    elif isinstance(payload, (str, Path)):
                        p = Path(payload)
                        if p.exists():
                            extras = (
                                artifact_extras_extractor(job)
                                if artifact_extras_extractor else None
                            ) or {}
                            artifact_uri = _artifacts().emit_artifact(
                                job_id=ctx["job_id"],
                                kind=artifact_kind,
                                local_path=p,
                                extras=extras,
                            )
                            artifact_summary = {
                                "artifact_kind": artifact_kind,
                                "artifact_bytes": p.stat().st_size,
                                "artifact_uri": artifact_uri,
                            }
                    elif isinstance(payload, (dict, list)):
                        extras = (
                            artifact_extras_extractor(job)
                            if artifact_extras_extractor else None
                        ) or {}
                        artifact_uri = _artifacts().emit_artifact_json(
                            job_id=ctx["job_id"],
                            kind=artifact_kind,
                            data=payload,
                            extras=extras,
                        )
                        artifact_summary = {
                            "artifact_kind": artifact_kind,
                            "artifact_uri": artifact_uri,
                        }
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "stage_envelope: artifact upload failed stage=%s "
                        "kind=%s: %s",
                        stage_name, artifact_kind, exc,
                    )

            try:
                _obs().track(
                    "stage.end",
                    category="render",
                    success=True,
                    duration_ms=duration_ms,
                    job_id=ctx["job_id"],
                    metadata={
                        "stage": stage_name,
                        "slug": job.get("_slug") or ctx["slug"],
                        "channel": ctx["channel"],
                        "variant": ctx["variant"],
                        **artifact_summary,
                    },
                )
            except Exception:  # noqa: BLE001
                pass

            return result

        # Preserve signature for stages whose callers pass extra kwargs.
        wrapper.__signature__ = sig  # type: ignore[attr-defined]
        return wrapper

    return decorate


__all__ = [
    "EventsBuffer",
    "install_events_buffer",
    "stage_envelope",
]
