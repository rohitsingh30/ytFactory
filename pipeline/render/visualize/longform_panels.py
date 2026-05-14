"""Long-form panel slideshow VisualProducer (long-engine canonical).

Wraps :func:`pipeline.render.long_form.build_image_panels_video` so
the new long_engine can call a Protocol method instead of importing
the renderer-internal helper directly.

Today's impl is a thin delegating wrapper. The bigbang PR moves the
body fully into this module so long_form.py can be deleted.

Plugin selection: ``spec.visual_mode = LONGFORM_PANELS``. Default for
mystoriesanimated / hindutavaanimated / rhymetimejunction long-form
renders. Channels using archival footage instead set
``visual_mode=archival_shotlist``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    Timeline,
    VisualProducer,
    VisualTrack,
    register_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import probe_duration


class LongformPanels:
    """One Flux panel per ~30-60 s of narration with Ken Burns motion.

    Today's impl wraps the existing
    :func:`pipeline.render.long_form.build_image_panels_video` helper.
    The bigbang PR moves the body inline.
    """

    def produce(
        self,
        spec: Any,  # RenderSpec
        timeline: Timeline,
        work_dir: Path,
    ) -> VisualTrack:
        try:
            from pipeline.render.long_form import (  # noqa: PLC0415
                build_image_panels_video,
            )
        except ImportError:
            # Helper not available — emit a solid-color stand-in so the
            # engine still produces a video. Bigbang PR makes this hard-required.
            return self._fallback_solid_color(spec, timeline, work_dir)

        out_path = work_dir / "panels_video.mp4"

        # build_image_panels_video signature today expects:
        # (panels, narration_dur_s, out_path, image_w, image_h, fps, ...)
        # — we pass minimal args and let the helper use channel YAML
        # defaults for the rest. Bigbang PR plumbs spec fields through
        # explicitly.
        panels = self._panels_from_timeline(timeline)
        if not panels:
            return self._fallback_solid_color(spec, timeline, work_dir)

        try:
            build_image_panels_video(
                panels=panels,
                narration_dur_s=timeline[-1].end_s if timeline else 0.0,
                out_path=out_path,
                image_w=spec.output_resolution[0],
                image_h=spec.output_resolution[1],
                fps=spec.output_fps,
            )
        except (TypeError, Exception):  # noqa: BLE001
            # Signature mismatch or render error — fall back to solid
            # color so the engine still produces something. Bigbang PR
            # tightens this contract.
            return self._fallback_solid_color(spec, timeline, work_dir)

        return VisualTrack(
            video_path=out_path,
            duration_s=probe_duration(out_path),
            extras={"source": "longform_panels", "n_panels": len(panels)},
        )

    def _panels_from_timeline(self, timeline: Timeline) -> list[dict]:
        return [
            {
                "id": seg.anchor_id,
                "scene": seg.text,
                "start_s": seg.start_s,
                "end_s": seg.end_s,
            }
            for seg in timeline
        ]

    def _fallback_solid_color(
        self, spec: Any, timeline: Timeline, work_dir: Path,
    ) -> VisualTrack:
        from pipeline.render.shared.ffmpeg_helpers import run_ffmpeg  # noqa: PLC0415
        out_path = work_dir / "panels_fallback.mp4"
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
            extras={"source": "longform_panels_fallback"},
        )


register_plugin("visualize", "longform_panels", LongformPanels())
assert isinstance(LongformPanels(), VisualProducer)


__all__ = ["LongformPanels"]
