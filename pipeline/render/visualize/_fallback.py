"""Shared cross-plugin fallback: long-form visual track of last resort.

When a long-form visualize plugin (``archival_shotlist`` /
``footage_windows``) can't produce its primary output (no shotlist
authored, helper module missing, network failure during clip
download, …), it MUST still return a viewable ``VisualTrack`` —
solid-color stand-ins shipped in production for years (2026-05-15
audit found 26-min black mp4s on every cosmosdecoded long-form
render) and that's a P0 viewer experience bug.

This helper dispatches the failed plugin to the ``longform_panels``
plugin (Flux AI panel slideshow) which works on any timeline shape.
``longform_panels`` itself can fail (image-gen API down, signature
mismatch with the legacy helper) — when that happens, fall through
to the OLD solid-color stand-in so the engine still produces a
video file (better than crashing the render). The recursion guard
prevents A → B → A loops.

Sentinel pattern: callers pass a unique ``sentinel_kwarg`` name so
the recursion check is per-plugin (one plugin failing back to
another doesn't trip the guard for the second plugin).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    Timeline,
    VisualTrack,
    get_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import probe_duration, run_ffmpeg

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
            "going to solid-color stand-in (%s) to break recursion",
            label, sentinel_kwarg, color,
        )
        return _solid_color(spec, timeline, work_dir, color=color, label=label)

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
    except Exception as exc:  # noqa: BLE001 — last-resort fallback MUST not raise
        _logger.warning(
            "[fallback] %s → longform_panels also failed (%s) — "
            "returning solid-color stand-in (%s)",
            label, exc, color,
        )
        return _solid_color(spec, timeline, work_dir, color=color, label=label)


def _solid_color(
    spec: Any,
    timeline: Timeline,
    work_dir: Path,
    *,
    color: str,
    label: str,
) -> VisualTrack:
    """Last-resort ffmpeg lavfi color stand-in. Same shape as the
    pre-2026-05-15 inline impl in each plugin — preserved here so
    a hard image-gen outage still produces a video file (rather than
    crashing the render entirely)."""
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


__all__ = ["_fallback_to_longform_panels"]
