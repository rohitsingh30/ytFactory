"""Per-render context that auto-decorates every span/log inside it.

A render run propagates the same identity across dozens of nested
calls (cast → prompts → image gen × N → tts × N → asr → captions →
compose → upload). Re-passing ``channel="historyrecapped",
slug="aita-001"`` to every ``tlm.timed`` call would make the call sites
unreadable.

Instead we keep a single :class:`RenderContext` in a
:class:`contextvars.ContextVar` and merge it as attributes onto every
span and log record we emit. The renderer pushes once with
:func:`ctx`; everything inside (sync or async) inherits.

Mirrors the approach used by :mod:`pipeline.utils.run_context` for
``RUN_ID`` (the small contextvar pattern) — extended here to the full
render identity.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Iterator, Optional


@dataclass(frozen=True)
class RenderContext:
    """Identity for the current unit of work.

    Every field is optional because the same code paths run in many
    contexts (a render with full identity; a one-off CLI call with
    only ``channel``; a cron with no slug). Only set fields are
    emitted as span attributes.
    """

    channel: Optional[str] = None
    niche: Optional[str] = None
    slug: Optional[str] = None
    job_id: Optional[str] = None
    run_id: Optional[str] = None
    render_kind: Optional[str] = None     # short / long_form / footage_only / sports_doc / cron / route
    render_mode: Optional[str] = None     # laptop / cloud
    user: Optional[str] = None            # signed-in user when known

    def merged(self, **overrides: Any) -> "RenderContext":
        """Return a new context with overrides applied (None preserves)."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean) if clean else self

    def as_attributes(self) -> dict[str, str]:
        """Project the non-None fields as span/log attributes prefixed
        with ``ytfactory.`` so they don't collide with OTel semantic
        conventions or upstream library attrs.
        """
        out: dict[str, str] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if v is None:
                continue
            out[f"ytfactory.{f.name}"] = str(v)
        return out

    def as_dict(self) -> dict[str, Any]:
        """Plain dict for serialisation (Firestore, JSON, log payload)."""
        return {k: v for k, v in asdict(self).items() if v is not None}


_EMPTY = RenderContext()
_CTX: contextvars.ContextVar[RenderContext] = contextvars.ContextVar(
    "ytfactory_render_ctx",
    default=_EMPTY,
)


def current_context() -> RenderContext:
    """Return the active :class:`RenderContext` (defaults to empty)."""
    return _CTX.get()


@contextmanager
def ctx(**fields: Any) -> Iterator[RenderContext]:
    """Push a merged :class:`RenderContext` for the duration of the block.

    Example::

        with ctx(channel="historyrecapped", slug=slug, render_kind="short"):
            run_render(...)

    Unset fields are inherited from the enclosing context (so wrapping
    a long_form render in ``ctx(channel=...)`` and then a sub-stage in
    ``ctx(slug=...)`` keeps both attrs).
    """
    parent = _CTX.get()
    merged = parent.merged(**fields)
    token = _CTX.set(merged)
    try:
        yield merged
    finally:
        _CTX.reset(token)


__all__ = [
    "RenderContext",
    "ctx",
    "current_context",
]
