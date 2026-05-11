"""Helpers for marking spans as failed.

Centralised so every callsite uses the same status / event format
(makes the dashboard's "errors" view trivial to filter on).
"""
from __future__ import annotations

import traceback
from typing import Optional

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode


_MAX_TRACE_CHARS = 4096


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
    - Truncates the formatted traceback to 4 KB so we never blow span
      size limits on deep stacks.
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
        tb = tb[: _MAX_TRACE_CHARS - 3] + "..."
    attrs["exception.stacktrace"] = tb

    span.record_exception(exc, attributes=attrs)
    if fatal:
        span.set_status(Status(StatusCode.ERROR, str(exc)[:256]))


__all__ = ["record_exception"]
