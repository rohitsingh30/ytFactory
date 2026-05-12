"""High-level event emission API: :func:`track` + :func:`timed`.

These are what real call sites use. They sit on top of the OTel
providers from :mod:`pipeline.observability.otel`, and decorate every
emission with the active :class:`RenderContext`.

Conventions
-----------

- Span name = the ``event`` parameter (so trace explorers group by it).
- Span attributes start with ``ytfactory.`` (so they don't collide
  with OTel semantic conventions or upstream library attrs).
- Histograms record duration in milliseconds, named
  ``ytfactory.stage_duration_ms`` with attribute ``event=<name>``.
- Counters record event volume, named ``ytfactory.events`` with
  attributes ``event=<name>``, ``category=<cat>``, ``success=<bool>``.
- Logs are emitted at INFO (success) / ERROR (failure) with a
  structured body so the GCP log bridge writes them as JSON entries.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from opentelemetry import _logs as _logs_api
from opentelemetry.trace import SpanKind, Status, StatusCode

from .context import current_context
from .errors import record_exception
from .otel import logger, meter, tracer


EVENT_HISTOGRAM_NAME = "ytfactory.stage_duration_ms"
EVENT_COUNTER_NAME = "ytfactory.events"
EVENT_LOG_NAME = "ytfactory.event"


# ---- lazy-init counter + histogram -----------------------------------


_INSTRUMENTS: dict[str, Any] = {}


def _histogram():
    h = _INSTRUMENTS.get("hist")
    if h is None:
        h = meter().create_histogram(
            EVENT_HISTOGRAM_NAME,
            unit="ms",
            description="Wall-clock duration of a ytFactory stage / event.",
        )
        _INSTRUMENTS["hist"] = h
    return h


def _counter():
    c = _INSTRUMENTS.get("count")
    if c is None:
        c = meter().create_counter(
            EVENT_COUNTER_NAME,
            description="Count of ytFactory events emitted via track / timed.",
        )
        _INSTRUMENTS["count"] = c
    return c


def _emit_log(
    *,
    event: str,
    category: str,
    success: bool,
    duration_ms: Optional[int],
    job_id: Optional[str],
    metadata: dict[str, Any],
    attrs: dict[str, Any],
) -> None:
    """Emit one structured log record."""
    body = {
        "event": event,
        "category": category,
        "success": success,
        "duration_ms": duration_ms,
        "job_id": job_id,
        "metadata": metadata or {},
    }
    severity = (
        _logs_api.SeverityNumber.INFO
        if success
        else _logs_api.SeverityNumber.ERROR
    )
    rec = _logs_api.LogRecord(
        timestamp=time.time_ns(),
        observed_timestamp=time.time_ns(),
        severity_number=severity,
        severity_text=severity.name,
        body=body,
        attributes=attrs,
    )
    logger(EVENT_LOG_NAME).emit(rec)


def _build_attrs(
    *,
    event: str,
    category: str,
    success: bool,
    duration_ms: Optional[int],
    job_id: Optional[str],
    metadata: Optional[dict],
) -> dict[str, Any]:
    """Compose the full attribute dict for span/log/metric."""
    attrs: dict[str, Any] = current_context().as_attributes()
    attrs["ytfactory.event"] = event
    attrs["ytfactory.category"] = category
    attrs["ytfactory.success"] = bool(success)
    if job_id:
        attrs["ytfactory.job_id"] = job_id
    if duration_ms is not None:
        attrs["ytfactory.duration_ms"] = int(duration_ms)
    if metadata:
        for k, v in metadata.items():
            # OTel attrs must be primitives; coerce non-primitives to str.
            if isinstance(v, (str, bool, int, float)):
                attrs[f"ytfactory.meta.{k}"] = v
            elif v is None:
                continue
            else:
                attrs[f"ytfactory.meta.{k}"] = str(v)[:1024]
    return attrs


# ---- public API --------------------------------------------------------


def track(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: Optional[int] = None,
    job_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Emit a discrete event.

    Records:
      * one log record (so it shows up in Cloud Logging),
      * one counter increment (so dashboards can chart event volume),
      * one histogram observation IF ``duration_ms`` is supplied.

    Never raises — telemetry must never break the pipeline.
    """
    try:
        attrs = _build_attrs(
            event=event,
            category=category,
            success=success,
            duration_ms=duration_ms,
            job_id=job_id,
            metadata=metadata,
        )
        # Audit T1.12 — thread channel + slug + render_kind from the
        # active RenderContext into the metric attributes so Cloud
        # Monitoring per-channel / per-render-kind rollups work.
        # Pre-fix metric_attrs only carried event/category/success →
        # every counter aggregated globally and the dashboard had no
        # way to slice by channel.
        ctx = current_context()
        metric_attrs = {
            "event": event,
            "category": category,
            "success": str(bool(success)).lower(),
        }
        if ctx.channel:
            metric_attrs["channel"] = ctx.channel
        if ctx.slug:
            metric_attrs["slug"] = ctx.slug
        if ctx.render_kind:
            metric_attrs["render_kind"] = ctx.render_kind
        if job_id:
            metric_attrs["job_id"] = job_id
        _counter().add(1, attributes=metric_attrs)
        if duration_ms is not None:
            _histogram().record(int(duration_ms), attributes=metric_attrs)
        _emit_log(
            event=event,
            category=category,
            success=success,
            duration_ms=duration_ms,
            job_id=job_id,
            metadata=metadata or {},
            attrs=attrs,
        )
    except Exception:  # noqa: BLE001
        # Telemetry failure must never break the pipeline.
        pass


def emit_span(
    name: str,
    *,
    duration_ms: int,
    success: bool = True,
    category: str = "pipeline",
    job_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Emit a span with a known duration (synthetic start/end times).

    For call sites that measured their own ``t0/t1`` and already
    track the result via :func:`track` / :func:`track_stage_done` —
    use this to also surface a span in Cloud Trace without rewriting
    the call site to use the :func:`timed` context manager.

    Internally the span has a synthetic start time
    (``now − duration_ms``) and an immediate end so its duration in
    Cloud Trace matches the value the caller measured. All standard
    ``ytfactory.*`` attributes are attached.

    Never raises.
    """
    try:
        attrs = _build_attrs(
            event=name,
            category=category,
            success=success,
            duration_ms=duration_ms,
            job_id=job_id,
            metadata=metadata,
        )
        # Wall-clock start (ns) so the span's trace-explorer bar is
        # positioned correctly; perf_counter would be a monotonic
        # clock whose origin Cloud Trace can't interpret.
        end_ns = time.time_ns()
        start_ns = end_ns - max(0, int(duration_ms)) * 1_000_000
        span = tracer().start_span(name, start_time=start_ns, attributes=attrs)
        if not success:
            span.set_status(Status(StatusCode.ERROR))
        span.end(end_time=end_ns)
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def timed(
    event: str,
    *,
    category: str = "pipeline",
    job_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    span_kind: SpanKind = SpanKind.INTERNAL,
) -> Iterator["_TimerHandle"]:
    """Open a span; on exit, record duration + emit a log record.

    On exception, marks the span ERROR and re-raises. Use
    :meth:`_TimerHandle.add` to merge extra metadata that should be
    recorded on close.

    Example::

        with obs.timed("tts_synth", category="tts",
                       metadata={"provider": "chatterbox"}) as t:
            audio = synth(...)
            t.add(metadata={"chars": len(text), "wav_seconds": 12.4})
    """
    start_ns = time.perf_counter_ns()
    handle = _TimerHandle(event=event, category=category, job_id=job_id,
                          metadata=dict(metadata or {}))
    base_attrs = _build_attrs(
        event=event,
        category=category,
        success=True,
        duration_ms=None,
        job_id=job_id,
        metadata=handle._metadata,
    )
    span = tracer().start_span(event, kind=span_kind, attributes=base_attrs)
    try:
        with _trace_use_span(span):
            yield handle
    except BaseException as e:
        # Build final attrs (with whatever the user .add()'d) before
        # marking failure.
        handle._success = False
        handle._error = e
        handle._capture_exc_meta(e)
        record_exception(e, span=span, fatal=True)
        _close_span(span, handle, start_ns, success=False)
        raise
    else:
        _close_span(span, handle, start_ns, success=handle._success)


class _TimerHandle:
    """Mutable holder yielded by :func:`timed`."""

    __slots__ = ("_event", "_category", "_job_id", "_metadata",
                 "_success", "_error")

    def __init__(self, *, event: str, category: str,
                 job_id: Optional[str], metadata: dict) -> None:
        self._event = event
        self._category = category
        self._job_id = job_id
        self._metadata = metadata
        self._success = True
        self._error: Optional[BaseException] = None

    def add(self, *, metadata: Optional[dict[str, Any]] = None) -> None:
        """Merge extra metadata that will be recorded on close."""
        if metadata:
            self._metadata.update(metadata)

    def fail(self, reason: str) -> None:
        """Mark the span as failed without raising. Useful when a
        downstream call returned a non-OK result that we treat as a
        soft-failure (e.g. cloud→local fallback)."""
        self._success = False
        self._metadata.setdefault("fail_reason", reason)

    def _capture_exc_meta(self, exc: BaseException) -> None:
        self._metadata.setdefault(
            "error", f"{type(exc).__name__}: {exc}"[:300]
        )


def _close_span(span, handle: _TimerHandle, start_ns: int, *, success: bool) -> None:
    duration_ms = int((time.perf_counter_ns() - start_ns) / 1_000_000)
    # Audit T1.12 — same channel/slug/render_kind threading as in
    # track(). The context is whatever the call site's render envelope
    # set (or the obs.ctx() block); empty when the span runs outside
    # a render envelope (one-off CLI tools).
    ctx = current_context()
    metric_attrs = {
        "event": handle._event,
        "category": handle._category,
        "success": str(bool(success)).lower(),
    }
    if ctx.channel:
        metric_attrs["channel"] = ctx.channel
    if ctx.slug:
        metric_attrs["slug"] = ctx.slug
    if ctx.render_kind:
        metric_attrs["render_kind"] = ctx.render_kind
    try:
        _counter().add(1, attributes=metric_attrs)
        _histogram().record(duration_ms, attributes=metric_attrs)
    except Exception:  # noqa: BLE001
        pass

    span.set_attribute("ytfactory.duration_ms", duration_ms)
    span.set_attribute("ytfactory.success", success)
    for k, v in handle._metadata.items():
        if v is None:
            continue
        span.set_attribute(
            f"ytfactory.meta.{k}",
            v if isinstance(v, (str, bool, int, float)) else str(v)[:1024],
        )
    if not success:
        # Idempotent on real RecordingSpans (the underlying SDK
        # tracks status internally). Safe on NonRecordingSpan too —
        # ``set_status`` is defined as a no-op on the base class. We
        # used to read ``span.status.status_code`` before calling
        # ``set_status`` to avoid a redundant write, but that READ
        # raises ``AttributeError: 'NonRecordingSpan' object has no
        # attribute 'status'`` when the OTel SDK isn't fully
        # initialised (which the cloud render-worker hits during the
        # ~1s window before instrumentation finishes booting). Four
        # production renders crashed with that AttributeError on
        # 2026-05-12; the read is gone now, the unconditional
        # ``set_status`` covers both cases.
        try:
            span.set_status(Status(StatusCode.ERROR))
        except Exception:  # noqa: BLE001
            pass
    span.end()

    try:
        attrs = _build_attrs(
            event=handle._event,
            category=handle._category,
            success=success,
            duration_ms=duration_ms,
            job_id=handle._job_id,
            metadata=handle._metadata,
        )
        _emit_log(
            event=handle._event,
            category=handle._category,
            success=success,
            duration_ms=duration_ms,
            job_id=handle._job_id,
            metadata=handle._metadata,
            attrs=attrs,
        )
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def _trace_use_span(span):
    """Tiny wrapper around :func:`opentelemetry.trace.use_span`.

    Defined here as a separate context manager so that exceptions
    raised inside the ``with`` block re-propagate to :func:`timed`,
    which adds error metadata before closing the span.
    """
    from opentelemetry import trace as _t
    with _t.use_span(span, end_on_exit=False, record_exception=False,
                     set_status_on_exception=False):
        yield


__all__ = [
    "EVENT_COUNTER_NAME",
    "EVENT_HISTOGRAM_NAME",
    "EVENT_LOG_NAME",
    "emit_span",
    "timed",
    "track",
]
