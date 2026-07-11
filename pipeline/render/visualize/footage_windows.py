"""Footage-windows VisualProducer (kathaa / footage_only-style renders).

Reads a shotlist (``<channel>/shotlist/<slug>.json``) declaring source
URLs + in/out timestamps, downloads each clip via yt-dlp, trims +
letterboxes, concats into one continuous video. NO AI image gen —
the channel's USP is the real footage.

Plugin selection: ``spec.visual_mode = FOOTAGE_WINDOWS``. Picked
automatically when the ``kathaa:`` or ``footage_only:`` block
migrates into ``defaults.long`` per the migration script.

The trim/concat ladder runs via
``pipeline.render.shared.footage_only_lib``.
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
from pipeline.render.visualize._fallback import _fallback_to_longform_panels

_logger = logging.getLogger(__name__)


class FootageWindows:
    """Shotlist-driven footage-only visual track.

    Reads the shotlist from ``spec.extra['shotlist_path']`` (or, when
    unset, from the channel's standard
    ``<channel>/shotlist/<slug>.json`` location). Each entry is a
    ``{source_url, in_s, out_s}`` triple. Clips are downloaded +
    trimmed + letterboxed via the ``footage_only_lib`` helpers,
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
            from pipeline.render.shared.footage_only_lib import _build_silent_video  # noqa: PLC0415
            import json  # noqa: PLC0415
            shotlist = json.loads(path.read_text())
            channel = spec.channel
            slug = (spec.extra or {}).get("slug", "footage_windows_render")
            out_path = _build_silent_video(channel, slug, shotlist, work_dir)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("footage_windows: footage_only_lib failed (%s) — "
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
        """Fall back to AI panel slideshow when shotlist is missing.

        Pre-2026-05-15 this returned a solid-color stand-in
        (``ffmpeg color=c=0x0a1626``) which produced 25 minutes of
        deep navy on every cloud long-form render of historyrecapped
        / kathaa channels — same root cause as archival_shotlist's
        fallback (no shotlist authored by the wizard pipeline).

        Surfaced by job 0ffe6dcd (HistoryRecapped Cuban Missile Crisis
        long-form) on 2026-05-15. New behaviour matches archival_shotlist:
        dispatch to ``longform_panels`` (AI panel slideshow) so the
        viewer sees something. See
        ``pipeline/render/visualize/_fallback.py`` for the shared helper
        + recursion guard.
        """
        return _fallback_to_longform_panels(
            spec, timeline, work_dir,
            sentinel_kwarg="_footage_windows_already_falling_back",
            color="0x0a1626",
            label="footage_windows_fallback",
        )


register_plugin("visualize", "footage_windows", FootageWindows())
assert isinstance(FootageWindows(), VisualProducer)


__all__ = ["FootageWindows"]
