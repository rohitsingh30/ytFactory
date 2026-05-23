"""Shared cross-plugin fallback: long-form visual track of last resort.

When a long-form visualize plugin (``archival_shotlist`` /
``footage_windows``) can't produce its primary output (no shotlist
authored, helper module missing, network failure during clip
download, …), it dispatches to the ``longform_panels`` plugin (Flux
AI panel slideshow) which works on any timeline shape.

Solid-color fallback REMOVED per user direction. When longform_panels
itself fails, this helper now RAISES — there is no last-resort solid-
color stand-in. The recursion guard sentinel still applies to prevent
A → B → A ping-ponging.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    RenderFailedError,
    Timeline,
    VisualTrack,
    get_plugin,
)

_logger = logging.getLogger(__name__)


def _fallback_to_longform_panels(
    spec: Any,
    timeline: Timeline,
    work_dir: Path,
    *,
    sentinel_kwarg: str,
    color: str = "0x141414",
    label: str = "longform_fallback",
) -> VisualTrack:
    """Try ``longform_panels``. Raise on failure — no solid-color path.

    Args:
        spec: RenderSpec — passed through to longform_panels.produce.
        timeline: Aligned Timeline — same.
        work_dir: Per-render scratch directory.
        sentinel_kwarg: A unique-per-caller env-key name used to mark
            the spec.extra dict so longform_panels can't recurse back
            into this caller's primary plugin.
        color: kept for back-compat with callers; unused (no fallback
            color path exists anymore).
        label: kept for back-compat (logging context).

    Returns:
        A ``VisualTrack`` from longform_panels.

    Raises:
        RenderFailedError: when longform_panels fails or a recursion
            sentinel fires. No silent fall-through.
    """
    # `color` parameter kept for caller back-compat. Read it so linters
    # don't flag the kwarg as unused.
    _ = color
    extra = dict(spec.extra or {})
    if extra.get(sentinel_kwarg):
        raise RenderFailedError(
            f"[fallback] {label} already in fallback chain "
            f"(sentinel={sentinel_kwarg!r}) — refusing to recurse. "
            f"site=pipeline/render/visualize/_fallback.py:"
            f"_fallback_to_longform_panels."
        )

    extra[sentinel_kwarg] = True
    try:
        spec.extra = extra
    except Exception:  # noqa: BLE001 — RenderSpec.extra may be read-only on some shapes
        pass

    try:
        plugin = get_plugin("visualize", "longform_panels")
        track = plugin.produce(spec, timeline, work_dir)
    except RenderFailedError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RenderFailedError(
            f"[fallback] {label} → longform_panels also failed ({exc}) — "
            f"no solid-color fallback available. "
            f"site=pipeline/render/visualize/_fallback.py:"
            f"_fallback_to_longform_panels."
        ) from exc
    _logger.info(
        "[fallback] %s → longform_panels OK "
        "(video=%s duration=%.2fs)",
        label, track.video_path.name, track.duration_s,
    )
    return track


__all__ = ["_fallback_to_longform_panels"]
