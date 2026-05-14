"""Footage-windows VisualProducer (kathaa / footage_only-style renders).

Reads a shotlist (``<channel>/shotlist/<slug>.json``) declaring source
URLs + in/out timestamps, downloads each clip via yt-dlp, trims +
letterboxes, concats into one continuous video. NO AI image gen —
the channel's USP is the real footage.

Plugin selection: ``spec.visual_mode = FOOTAGE_WINDOWS``. Picked
automatically when the legacy ``kathaa:`` or ``footage_only:`` block
migrates into ``defaults.long`` per the migration script.

Today's impl wraps :mod:`pipeline.render.footage_only` orchestration
(yt-dlp + ffmpeg trim ladder); bigbang PR moves it inline.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    Timeline,
    VisualProducer,
    VisualTrack,
    register_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import probe_duration, run_ffmpeg

_logger = logging.getLogger(__name__)


class FootageWindows:
    """Shotlist-driven footage-only visual track.

    Reads the shotlist from ``spec.extra['shotlist_path']`` (or, when
    unset, from the channel's standard
    ``<channel>/shotlist/<slug>.json`` location). Each entry is a
    ``{source_url, in_s, out_s}`` triple. Clips are downloaded +
    trimmed + letterboxed via the legacy ``footage_only`` helpers,
    then concatenated.

    Falls back to a solid-color stand-in if the shotlist is missing
    or the footage module isn't importable.
    """

    def produce(
        self,
        spec: Any,
        timeline: Timeline,
        work_dir: Path,
    ) -> VisualTrack:
        shotlist_path = (spec.extra or {}).get("shotlist_path")
        if not shotlist_path:
            _logger.warning("footage_windows: no shotlist_path in spec.extra "
                            "— falling back to solid color")
            return self._fallback_solid_color(spec, timeline, work_dir)

        path = Path(shotlist_path)
        if not path.exists():
            _logger.warning("footage_windows: shotlist not found at %s — "
                            "falling back to solid color", path)
            return self._fallback_solid_color(spec, timeline, work_dir)

        try:
            from pipeline.render.footage_only import _build_silent_video  # noqa: PLC0415
            import json  # noqa: PLC0415
            shotlist = json.loads(path.read_text())
            channel = spec.channel
            slug = (spec.extra or {}).get("slug", "footage_windows_render")
            out_path = _build_silent_video(channel, slug, shotlist, work_dir)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("footage_windows: legacy footage_only failed (%s) — "
                            "falling back to solid color", exc)
            return self._fallback_solid_color(spec, timeline, work_dir)

        return VisualTrack(
            video_path=out_path,
            duration_s=probe_duration(out_path),
            extras={"source": "footage_windows", "shotlist_path": str(path)},
        )

    def _fallback_solid_color(
        self, spec: Any, timeline: Timeline, work_dir: Path,
    ) -> VisualTrack:
        out_path = work_dir / "footage_windows_fallback.mp4"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        duration_s = timeline[-1].end_s if timeline else 1.0
        w, h = spec.output_resolution
        run_ffmpeg([
            "-f", "lavfi", "-t", f"{duration_s:.3f}",
            "-i", f"color=c=0x0a1626:s={w}x{h}:r={spec.output_fps}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            str(out_path),
        ])
        return VisualTrack(
            video_path=out_path,
            duration_s=probe_duration(out_path),
            extras={"source": "footage_windows_fallback"},
        )


register_plugin("visualize", "footage_windows", FootageWindows())
assert isinstance(FootageWindows(), VisualProducer)


__all__ = ["FootageWindows"]
