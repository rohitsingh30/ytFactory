"""Helpers for the four render entry points.

All four orchestrators (``shorts``, ``long_form``, ``footage_only``,
``sports_doc``) want the same boilerplate at the top of their main
function:

  * Push a :class:`RenderContext` so every nested telemetry call
    inherits ``channel`` / ``slug`` / ``render_kind`` / etc.
  * Open one parent span ``render.<kind>`` so Cloud Trace's waterfall
    has a single root for the whole render.
  * Auto-detect ``render_mode`` from the runtime environment
    (``cloud`` when ``K_SERVICE`` is set, ``laptop`` otherwise).
  * Pull ``job_id`` / ``run_id`` from the standard env vars without
    every caller having to remember the var names.

This module exists to keep that boilerplate from repeating in four
places (and drifting) — the contract in :func:`render_envelope` is
the single source of truth for "how a render integrates with the
observability layer".
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .context import ctx
from .telemetry import _TimerHandle, timed


JOB_ID_ENV = "YTFACTORY_JOB_ID"
RUN_ID_ENV = "YTFACTORY_RUN_ID"


def _resolve_render_mode() -> str:
    return "cloud" if os.environ.get("K_SERVICE") else "laptop"


@contextmanager
def render_envelope(
    *,
    channel: str,
    slug: str,
    render_kind: str,
    niche: Optional[str] = None,
    render_mode: Optional[str] = None,
    job_id: Optional[str] = None,
    run_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Iterator[_TimerHandle]:
    """Composite ``ctx + timed`` for a render entry point.

    Yields the inner timed handle so the caller can ``.add()`` more
    metadata as the render progresses (e.g. final video duration,
    upload URL, error category).

    Example::

        with obs.render_envelope(channel=channel_path.stem,
                                 slug=slug,
                                 render_kind="short") as render:
            ...                # all existing renderer code
            render.add(metadata={"final_seconds": 58.4})
    """
    job_id = job_id or os.environ.get(JOB_ID_ENV) or None
    run_id = run_id or os.environ.get(RUN_ID_ENV) or None
    mode = render_mode or _resolve_render_mode()
    md = {"channel": channel, "slug": slug, "render_kind": render_kind}
    if metadata:
        md.update(metadata)

    with ctx(
        channel=channel,
        slug=slug,
        niche=niche,
        render_kind=render_kind,
        render_mode=mode,
        job_id=job_id,
        run_id=run_id,
    ):
        with timed(
            f"render.{render_kind}",
            category="render",
            job_id=job_id,
            metadata=md,
        ) as t:
            yield t


__all__ = ["render_envelope", "JOB_ID_ENV", "RUN_ID_ENV"]
