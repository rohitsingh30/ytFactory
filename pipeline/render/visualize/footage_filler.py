"""Footage-filler VisualProducer (paired with overlay_timeline).

Cycles b-roll clips for the full narration duration as the always-on
background. Foreground anchored cuts come from the
:mod:`pipeline.render.overlays.anchored_footage` overlay producer
(layer 10) — separation of concerns per the contracts.py layer
convention.

Plugin selection: ``spec.visual_mode = FOOTAGE_FILLER``. Activated
together with ``spec.overlay_timeline = True`` for the sports_doc
shape.
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


class FootageFiller:
    """B-roll cycle background for overlay-timeline renders.

    Today's impl falls back to a tinted-solid-color stand-in. Bigbang
    PR plumbs the real b-roll cycle (lifted from
    sports_doc.py::_build_filler_video).
    """

    def produce(
        self,
        spec: Any,
        timeline: Timeline,
        work_dir: Path,
    ) -> VisualTrack:
        out_path = work_dir / "footage_filler.mp4"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        duration_s = timeline[-1].end_s if timeline else 1.0
        w, h = spec.output_resolution

        # Today: solid-color fallback. Bigbang: real b-roll cycle.
        run_ffmpeg([
            "-f", "lavfi", "-t", f"{duration_s:.3f}",
            "-i", f"color=c={spec.filler.bg_color}:s={w}x{h}:r={spec.output_fps}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            str(out_path),
        ])

        return VisualTrack(
            video_path=out_path,
            duration_s=probe_duration(out_path),
            extras={"source": "footage_filler"},
        )


register_plugin("visualize", "footage_filler", FootageFiller())
assert isinstance(FootageFiller(), VisualProducer)


__all__ = ["FootageFiller"]
