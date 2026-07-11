"""Archival shotlist VisualProducer (long_form sleep history default).

Reads a curated shotlist of archive.org / Wikimedia clips, downloads +
trims each, concats into one continuous video for use as long-form
sleep narrator background.

Plugin selection: ``spec.visual_mode = ARCHIVAL_SHOTLIST``. Default
for historyrecapped + cosmosdecoded long-form renders.
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


class ArchivalShotlist:
    """Archive footage cycled as long-form background.

    Reads shotlist from ``spec.extra['shotlist_path']`` and delegates
    to ``pipeline.render.shared.long_form_lib._trim_shotlist_clips`` +
    ``_concat_and_pad``. Falls back to longform_panels when the
    shotlist or helpers are unavailable.
    """

    def produce(
        self,
        spec: Any,
        timeline: Timeline,
        work_dir: Path,
    ) -> VisualTrack:
        shotlist_path = (spec.extra or {}).get("shotlist_path")
        if shotlist_path and Path(shotlist_path).exists():
            try:
                import json  # noqa: PLC0415
                from pipeline.render.shared.long_form_lib import (  # noqa: PLC0415
                    _trim_shotlist_clips, _concat_and_pad,
                )
                shotlist = json.loads(Path(shotlist_path).read_text())
                clips = shotlist.get("clips") or []
                trimmed = _trim_shotlist_clips(
                    clips=clips,
                    cache_dir=work_dir,
                    out_w=spec.output_resolution[0],
                    out_h=spec.output_resolution[1],
                    fps=spec.output_fps,
                )
                duration_s = timeline[-1].end_s if timeline else 0.0
                out_path = work_dir / "archival_shotlist.mp4"
                _concat_and_pad(trimmed, out_path, total_dur_s=duration_s)
                return VisualTrack(
                    video_path=out_path,
                    duration_s=probe_duration(out_path),
                    extras={
                        "source": "archival_shotlist",
                        "n_clips": len(clips),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("archival_shotlist: helper chain failed (%s) — "
                                "falling back to solid color", exc)

        return self._fallback_solid_color(spec, timeline, work_dir)

    def _fallback_solid_color(
        self, spec: Any, timeline: Timeline, work_dir: Path,
    ) -> VisualTrack:
        """Fall back to AI panel slideshow when shotlist is missing.

        Pre-2026-05-15 this returned a solid-color stand-in
        (``ffmpeg color=c=0x141414``) which produced 26 minutes of
        near-black video on every cloud long-form render of
        cosmosdecoded / historyrecapped — channels that pick
        ``visual_mode=archival_shotlist`` by default but that the
        wizard pipeline never authors a shotlist for.

        Surfaced by job ce309c80 (CosmosDecoded "How We Knew Universe
        Expanding" long-form) on 2026-05-15. Audio + chapter cards +
        music + lower-thirds all rendered fine; the visual track
        was 26 min of solid #141414. Unwatchable.

        New behaviour: fall back to ``longform_panels`` (AI panel
        slideshow) which generates a Flux panel per timeline segment.
        Same pattern as the cloud-TTS auto-fallback to local f5 —
        a missing data source MUST resolve to something the viewer
        can see, not a flat color.

        If even longform_panels fails (helper absent / image-gen API
        down / panel gen errors), THIS method is called recursively
        — guard with a sentinel so we don't loop. The recursive
        path falls back to the OLD solid color so the engine still
        produces a video file (better than a hard crash).
        """
        return _fallback_to_longform_panels(
            spec, timeline, work_dir,
            sentinel_kwarg="_archival_shotlist_already_falling_back",
            color="0x141414",
            label="archival_shotlist_fallback",
        )


register_plugin("visualize", "archival_shotlist", ArchivalShotlist())
assert isinstance(ArchivalShotlist(), VisualProducer)


__all__ = ["ArchivalShotlist"]
