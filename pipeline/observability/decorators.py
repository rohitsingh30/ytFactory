"""Lightweight ``@traced`` decorator for the long-tail modules.

The wrap-and-rename pattern used by the renderer / TTS dispatcher is
ideal for the few hot entry points where we want explicit metadata
shaping. For the long tail (~25 LLM modules, the leaf utilities), a
decorator is faster to apply and makes the diff trivially reviewable.

Usage::

    @traced("rewrite_script", category="llm", capture=["channel", "slug"])
    def rewrite(text: str, *, channel: str, slug: str, ...) -> Script:
        ...

What you get for free:

* One ``rewrite_script`` span per call, durations recorded in the
  ``ytfactory.stage_duration_ms`` histogram.
* The argument values listed in ``capture`` are automatically picked
  up from positional / keyword args via :class:`inspect.Signature` and
  attached as ``ytfactory.meta.<name>`` attrs.
* Exceptions mark the span ERROR + record the exception event.

When you DO want metadata that's only known mid-function (e.g. the
LLM call's input_tokens), call ``obs.timed(...)`` directly inside the
body and add via ``t.add(metadata=...)`` — the decorator and the
inner ``timed`` block compose cleanly (the inner span nests under the
outer one).
"""
from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, Iterable, Optional

from .telemetry import timed


_PRIMITIVE = (str, bool, int, float)


def traced(
    name: Optional[str] = None,
    *,
    category: str = "pipeline",
    capture: Optional[Iterable[str]] = None,
    extra_metadata: Optional[dict[str, Any]] = None,
) -> Callable[[Callable], Callable]:
    """Decorate ``fn`` so each call opens a span.

    Parameters
    ----------
    name
        Span name. Defaults to ``f"{fn.__module__}.{fn.__name__}"``.
    category
        Telemetry category — drives the dashboard's per-category
        rollup. Use ``"llm"``, ``"image"``, ``"tts"``, ``"upload"``,
        ``"render"``, ``"cron"``, ``"http"``, or the default ``"pipeline"``.
    capture
        Names of arguments to extract and attach as
        ``ytfactory.meta.<name>``. Only primitive values are captured;
        non-primitives are silently dropped (otherwise span size would
        balloon on big dict / Path arguments).
    extra_metadata
        Static metadata appended to every call (e.g. ``{"stage": "cast"}``).
    """
    capture_list = list(capture or [])

    def decorator(fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        span_name = name or f"{fn.__module__}.{fn.__name__}"

        # Audit Q2.10 — pre-fix this returned the same sync wrapper for
        # every callable, including ``async def`` ones. Wrapping an
        # awaitable with ``with timed(): return fn(*args, **kwargs)``
        # closed the span the moment the coroutine object was returned
        # (microseconds), recording near-zero latency for the actual
        # async work that ran later when the caller awaited it. No
        # async stages were decorated as of 2026-05-12 so no metric
        # was wrong yet — but the next ``@traced async def`` would
        # silently regress the dashboard's stage_duration_ms histogram.
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                metadata: dict[str, Any] = dict(extra_metadata or {})
                if capture_list:
                    metadata.update(_capture_args(sig, capture_list, args, kwargs))
                with timed(span_name, category=category, metadata=metadata):
                    return await fn(*args, **kwargs)

            async_wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            metadata: dict[str, Any] = dict(extra_metadata or {})
            if capture_list:
                metadata.update(_capture_args(sig, capture_list, args, kwargs))
            with timed(span_name, category=category, metadata=metadata):
                return fn(*args, **kwargs)

        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper

    return decorator


def _capture_args(sig: inspect.Signature, capture_list: list[str],
                  args: tuple, kwargs: dict) -> dict[str, Any]:
    """Bind args against ``sig`` and extract only the primitive values
    listed in ``capture_list``. Non-primitives are coerced to short
    strings so we don't blow span attribute size limits. Binding
    failures (caller passed mismatched shape) silently fall through —
    the span still opens, just without auto-metadata."""
    out: dict[str, Any] = {}
    try:
        bound = sig.bind_partial(*args, **kwargs)
    except (TypeError, ValueError):
        return out
    for arg_name in capture_list:
        if arg_name not in bound.arguments:
            continue
        v = bound.arguments[arg_name]
        if isinstance(v, _PRIMITIVE):
            out[arg_name] = v
        elif v is None:
            continue
        else:
            out[arg_name] = str(v)[:200]
    return out


__all__ = ["traced"]
