"""Engine dispatcher — picks short_engine vs long_engine by spec.kind.

Single function: :func:`pick_engine` returns the engine's
``render(spec, script, work_dir, out_path, *, progress_cb=None) → Path``
callable for the given spec. Used by :mod:`pipeline.render.video` (the
public entry) to route every render through the right engine.

Mapping:

* ``RenderKind.SHORT``        → :func:`pipeline.render.short_engine.render_short`
* ``RenderKind.LONG_FORM``    → :func:`pipeline.render.long_engine.render_long`
* ``RenderKind.SPORTS_DOC``   → :func:`pipeline.render.long_engine.render_long`
                                 (``visual_mode=sports_overlay_timeline``)
* ``RenderKind.FOOTAGE_ONLY`` → :func:`pipeline.render.long_engine.render_long`
                                 (``visual_mode=footage_windows``)

All long-shaped kinds go through the long engine; the ``visual_mode``
and other spec flags differentiate WHAT it renders.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from pipeline.render.spec import RenderKind, RenderSpec
from pipeline.render.telemetry_helpers import enum_value, track_event

# Engine functions are imported lazily to keep this module's import
# cheap (``video.py`` imports this at module top).


# Type alias for the engine signature. ``progress_cb`` is keyword-only;
# every engine accepts it but may ignore it (None disables boundary
# events). See pipeline/render/short_engine.py::render_short for the
# semantics.
ProgressCallback = Callable[[str, str], None]
EngineFn = Callable[..., Path]


def pick_engine(spec: RenderSpec) -> EngineFn:
    """Return the engine's render function for ``spec.kind``.

    Engine functions all share the signature
    ``(spec, script, work_dir, out_path, *, progress_cb=None) → Path``.

    Raises :class:`ValueError` for unknown kinds — should be unreachable
    given :class:`RenderKind` is an Enum, but defensive against future
    changes.
    """
    if spec.kind == RenderKind.SHORT:
        from pipeline.render.short_engine import render_short  # noqa: PLC0415
        _emit_engine_pick(spec, render_short)
        return render_short

    # All long-shaped kinds (LONG_FORM / SPORTS_DOC / FOOTAGE_ONLY) go
    # through the long engine. The visual_mode + other spec flags
    # differentiate WHAT the long engine renders.
    if spec.kind in {RenderKind.LONG_FORM, RenderKind.SPORTS_DOC, RenderKind.FOOTAGE_ONLY}:
        from pipeline.render.long_engine import render_long  # noqa: PLC0415
        _emit_engine_pick(spec, render_long)
        return render_long

    raise ValueError(f"engine.pick_engine: unknown kind={spec.kind!r}")


def _emit_engine_pick(spec: RenderSpec, fn: EngineFn) -> None:
    track_event(
        "engine.pick",
        category="pipeline",
        metadata={
            "chosen": getattr(fn, "__name__", str(fn)),
            "kind": enum_value(spec.kind),
            "format": getattr(spec, "format", None),
            "channel": spec.channel,
        },
    )


__all__ = ["pick_engine", "EngineFn", "ProgressCallback"]
