"""Archival shotlist VisualProducer (long_form sleep history default).

Reads a curated shotlist of archive.org / Wikimedia clips, downloads +
trims each, concats into one continuous video for use as long-form
sleep narrator background. Same workflow as historyrecapped's
existing long_form pipeline.

Plugin selection: ``spec.visual_mode = ARCHIVAL_SHOTLIST``. Default
for historyrecapped + cosmosdecoded long-form renders.

Today's impl is a delegating wrapper. Bigbang PR moves the body
inline.
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


class ArchivalShotlist:
    """Archive footage cycled as long-form background.

    Reads shotlist from ``spec.extra['shotlist_path']`` and delegates
    to ``pipeline.render.long_form._trim_shotlist_clips`` +
    ``_concat_and_pad`` (the existing helpers). Falls back to solid
    color when the shotlist or helpers are unavailable.
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
                from pipeline.render.long_form import (  # noqa: PLC0415
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
        out_path = work_dir / "archival_shotlist_fallback.mp4"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        duration_s = timeline[-1].end_s if timeline else 1.0
        w, h = spec.output_resolution
        run_ffmpeg([
            "-f", "lavfi", "-t", f"{duration_s:.3f}",
            "-i", f"color=c=0x141414:s={w}x{h}:r={spec.output_fps}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            str(out_path),
        ])
        return VisualTrack(
            video_path=out_path,
            duration_s=probe_duration(out_path),
            extras={"source": "archival_shotlist_fallback"},
        )


register_plugin("visualize", "archival_shotlist", ArchivalShotlist())
assert isinstance(ArchivalShotlist(), VisualProducer)


__all__ = ["ArchivalShotlist"]
