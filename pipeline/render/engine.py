"""Engine dispatcher — picks short_engine vs long_engine by spec.kind.

Single function: :func:`pick_engine` returns the engine's
``render(spec, script, work_dir, out_path) → Path`` callable for the
given spec. Used by :mod:`pipeline.render.video` (the public entry)
to route every render through the right engine.

Today's mapping:

* ``RenderKind.SHORT``        → :func:`pipeline.render.short_engine.render_short`
* ``RenderKind.LONG_FORM``    → :func:`pipeline.render.long_engine.render_long`
* ``RenderKind.SPORTS_DOC``   → :func:`pipeline.render.long_engine.render_long`
                                 (sports_doc kind is collapsing into long
                                 with ``visual_mode=overlay_timeline`` per
                                 the consolidation plan; bigbang PR removes
                                 the SPORTS_DOC enum value)
* ``RenderKind.FOOTAGE_ONLY`` → :func:`pipeline.render.long_engine.render_long`
                                 (same — collapses into long with
                                 ``visual_mode=footage_windows``)

Note: the ``SPORTS_DOC`` and ``FOOTAGE_ONLY`` mapping rows are
TRANSITIONAL. They keep the old enum values working through the
plugin layer until the bigbang PR collapses ``RenderKind`` to
``{SHORT, LONG}``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from pipeline.render.spec import RenderKind, RenderSpec

# Engine functions are imported lazily to keep this module's import
# cheap (``video.py`` imports this at module top).


# Type alias for the engine signature.
EngineFn = Callable[[RenderSpec, dict[str, Any], Path, Path], Path]


def pick_engine(spec: RenderSpec) -> EngineFn:
    """Return the engine's render function for ``spec.kind``.

    Engine functions all share the signature
    ``(spec, script, work_dir, out_path) → Path``.

    Raises :class:`ValueError` for unknown kinds — should be unreachable
    given :class:`RenderKind` is an Enum, but defensive against future
    changes.
    """
    if spec.kind == RenderKind.SHORT:
        from pipeline.render.short_engine import render_short  # noqa: PLC0415
        return render_short

    # All long-shaped kinds (LONG_FORM / SPORTS_DOC / FOOTAGE_ONLY) go
    # through the long engine. The visual_mode + overlay_timeline +
    # other spec flags differentiate WHAT the long engine renders.
    # Bigbang PR collapses these three to a single LONG enum value.
    if spec.kind in {RenderKind.LONG_FORM, RenderKind.SPORTS_DOC, RenderKind.FOOTAGE_ONLY}:
        from pipeline.render.long_engine import render_long  # noqa: PLC0415
        return render_long

    raise ValueError(f"engine.pick_engine: unknown kind={spec.kind!r}")


__all__ = ["pick_engine", "EngineFn"]
