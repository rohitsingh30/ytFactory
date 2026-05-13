"""Helpers for marking spans as failed.

Centralised so every callsite uses the same status / event format
(makes the dashboard's "errors" view trivial to filter on).
"""
from __future__ import annotations

import traceback
from typing import Optional

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode


_MAX_TRACE_CHARS = 2048
_MAX_HEAD_CHARS = 512  # keep the entry-point frame so we know where the call came from


def record_exception(
    exc: BaseException,
    *,
    span: Optional[Span] = None,
    fatal: bool = True,
    extra: Optional[dict] = None,
) -> None:
    """Attach ``exc`` to ``span`` (or the current span) and mark it failed.

    - Uses :meth:`Span.record_exception` for the structured event.
    - Sets :class:`StatusCode.ERROR` when ``fatal`` is true (default).
    - Truncates the formatted traceback to ~2 KB (head + tail) so we
      never blow span-attr limits AND so the most useful frames (the
      ones near the actual error) survive truncation.

    Audit D3.71 — pre-fix this kept the FIRST 4096 chars of the
    traceback, which truncated the END — losing the actual exception
    type, message, and the closest frames (the most useful diagnostic
    information). Plus 4 KB is a third of Cloud Trace's typical
    ~12 KB per-span attribute budget, starving every other attr.
    Now: keep ~512 chars of the head (entry-point frame so we know
    where the call originated) plus ~1.5 KB of the tail (the actual
    error + immediate frames) joined by an ellipsis marker.
    """
    if span is None:
        span = trace.get_current_span()
    if span is None or not span.is_recording():
        return

    attrs: dict = {
        "exception.type": type(exc).__name__,
        "exception.message": str(exc)[:1024],
    }
    if extra:
        attrs.update({f"ytfactory.error.{k}": str(v) for k, v in extra.items()})

    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(tb) > _MAX_TRACE_CHARS:
        tail_budget = _MAX_TRACE_CHARS - _MAX_HEAD_CHARS - len("\n...[truncated]...\n")
        head = tb[:_MAX_HEAD_CHARS]
        tail = tb[-tail_budget:] if tail_budget > 0 else ""
        tb = f"{head}\n...[truncated]...\n{tail}"
    attrs["exception.stacktrace"] = tb

    span.record_exception(exc, attributes=attrs)
    if fatal:
        span.set_status(Status(StatusCode.ERROR, str(exc)[:256]))


__all__ = ["record_exception"]
