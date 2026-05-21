"""Shared cross-plugin fallback: long-form visual track of last resort.

When a long-form visualize plugin (``archival_shotlist`` /
``footage_windows``) can't produce its primary output (no shotlist
authored, helper module missing, network failure during clip
download, …), it dispatches to the ``longform_panels`` plugin (Flux
AI panel slideshow) which works on any timeline shape.

2026-05-15 fail-loud audit
--------------------------

Pre-audit, when EVEN ``longform_panels`` failed, this helper fell
through to a solid-color ffmpeg ``lavfi`` stand-in — that produced
26-min black mp4s on every cosmosdecoded / historyrecapped long-form
render (jobs ce309c80, 0ffe6dcd). Post-audit the solid-color fall-
through RAISES :class:`RenderFailedError` by default; emergency
renders can opt back in with
``YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1``.

The recursion guard sentinel still applies — when the helper is
already in a fallback chain, we go straight to the (now opt-in)
solid color path rather than ping-ponging A → B → A.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    RenderFailedError,
    Timeline,
    VisualTrack,
    get_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import probe_duration, run_ffmpeg

_logger = logging.getLogger(__name__)


def _solid_color_override_enabled() -> bool:
    """True iff the operator opted in to the legacy solid-color
    fallback path via env (``YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1``).

    Default off — see :class:`RenderFailedError`'s docstring for the
    2026-05-15 audit rationale. This single env flag is shared across
    every fail-loud site (``_fallback._solid_color``, plus the
    ``archival_shotlist`` / ``footage_windows`` direct fall-throughs)
    so emergency renders flip ONE variable to ship.
    """
    return os.environ.get(
        "YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def _fallback_to_longform_panels(
    spec: Any,
    timeline: Timeline,
    work_dir: Path,
    *,
    sentinel_kwarg: str,
    color: str = "0x141414",
    label: str = "longform_fallback",
) -> VisualTrack:
    """Try ``longform_panels`` first; fall through to solid color on failure.

    Args:
        spec: RenderSpec — passed through to longform_panels.produce.
        timeline: Aligned Timeline — same.
        work_dir: Per-render scratch directory.
        sentinel_kwarg: A unique-per-caller env-key name. We use it to
            mark the spec.extra dict so longform_panels can't recurse
            back into this caller's primary plugin (would only happen
            if a future longform_panels impl decided to dispatch to
            archival_shotlist on its own failure — defensive guard).
        color: ffmpeg ``color=c=`` hex for the last-resort solid-color
            stand-in. Each caller passes its own debug color so the
            fallback chain is identifiable in post-mortems
            (``0x141414`` = archival, ``0x0a1626`` = footage_windows).
        label: ``VisualTrack.extras['source']`` tag for the
            last-resort path.

    Returns:
        A ``VisualTrack`` — either from longform_panels (preferred)
        or the legacy solid-color stand-in (last resort).
    """
    extra = dict(spec.extra or {})
    if extra.get(sentinel_kwarg):
        _logger.warning(
            "[fallback] %s already in fallback path (sentinel=%s) — "
            "deferring to solid-color stand-in (%s) to break recursion "
            "(will raise unless YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1)",
            label, sentinel_kwarg, color,
        )
        # 2026-05-15 fail-loud — _solid_color raises by default; only
        # the env override path returns a visual track.
        return _solid_color(
            spec, timeline, work_dir, color=color, label=label, cause=None,
        )

    extra[sentinel_kwarg] = True
    try:
        spec.extra = extra
    except Exception:  # noqa: BLE001 — RenderSpec.extra may be read-only on some shapes
        # If the spec is frozen / a typed dataclass, the recursion
        # guard is best-effort; the longform_panels plugin's own
        # exception-swallow guard catches the loop in practice.
        pass

    try:
        plugin = get_plugin("visualize", "longform_panels")
        track = plugin.produce(spec, timeline, work_dir)
        _logger.info(
            "[fallback] %s → longform_panels OK "
            "(video=%s duration=%.2fs)",
            label, track.video_path.name, track.duration_s,
        )
        return track
    except RenderFailedError:
        # Already a fail-loud raise from deeper inside the chain
        # (e.g. longform_panels' own _solid_color call refused).
        # Don't wrap — let the operator see the original site.
        raise
    except Exception as exc:  # noqa: BLE001 — distinguish from RenderFailedError above
        _logger.warning(
            "[fallback] %s → longform_panels also failed (%s) — "
            "deferring to solid-color stand-in (will raise unless "
            "YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1 is set)",
            label, exc,
        )
        # 2026-05-15 fail-loud — _solid_color raises by default; only
        # the env override path returns a visual track.
        return _solid_color(
            spec, timeline, work_dir, color=color, label=label, cause=exc,
        )


def _solid_color(
    spec: Any,
    timeline: Timeline,
    work_dir: Path,
    *,
    color: str,
    label: str,
    cause: BaseException | None = None,
) -> VisualTrack:
    """Last-resort ffmpeg lavfi color stand-in.

    2026-05-15 fail-loud contract
    -----------------------------

    By default this RAISES :class:`RenderFailedError` — never produces
    a solid-color stand-in. The legacy behaviour (26-min black mp4s)
    is only re-enabled when ``YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1``
    is set in the env (operator opt-in for emergency renders).

    Args:
        spec: RenderSpec — used for output resolution + fps.
        timeline: Aligned Timeline — used to size the lavfi color clip.
        work_dir: Per-render scratch directory.
        color: ffmpeg ``color=c=`` hex used in the override path.
        label: ``VisualTrack.extras['source']`` tag for the override path.
        cause: Original exception that triggered this fallback (if any).
            Surfaced via ``raise … from cause`` so the traceback shows
            the underlying ImportError / CloudRunUnavailable / etc.

    Raises:
        RenderFailedError: by default (no env flag set). Pre-fix this
            returned a solid-color mp4 silently — see ``contracts.py``
            ``RenderFailedError`` docstring + ``docs/post-audit-
            2026-05-15.md`` for the audit findings.
    """
    if not _solid_color_override_enabled():
        raise RenderFailedError(
            f"visualize fallback to solid color refused — see traceback "
            f"(label={label!r}, color={color!r}, "
            f"site=pipeline/render/visualize/_fallback.py:_solid_color). "
            f"Set YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1 to override "
            f"for emergency renders. Original cause: {cause!r}"
        ) from cause

    out_path = work_dir / f"{label}.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration_s = timeline[-1].end_s if timeline else 1.0
    w, h = spec.output_resolution
    run_ffmpeg([
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", f"color=c={color}:s={w}x{h}:r={spec.output_fps}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ])
    return VisualTrack(
        video_path=out_path,
        duration_s=probe_duration(out_path),
        extras={"source": label},
    )


__all__ = ["_fallback_to_longform_panels", "_solid_color_override_enabled"]
