"""AI beat-slideshow VisualProducer (short-engine canonical).

One Flux/SD image per Segment, cross-faded with Ken Burns motion.
Default visualize for short renders on every animated channel
(mystoriesanimated / hindutavaanimated / rhymetimejunction).

Today's impl is a delegating wrapper around ``pipeline.compose``'s
existing per-beat image-gen + Ken Burns chain. The bigbang PR moves
the body fully into this module so shorts.py can be deleted.

Plugin selection: ``spec.visual_mode = AI_BEAT_SLIDESHOW``.
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


class AiBeatSlideshow:
    """One image per beat, Ken Burns motion, cross-faded.

    Today's impl falls back to a solid-color stand-in when the
    pipeline.images dispatcher isn't available — bigbang PR plumbs
    the real Flux cloud call here.
    """

    def produce(
        self,
        spec: Any,  # RenderSpec
        timeline: Timeline,
        work_dir: Path,
    ) -> VisualTrack:
        try:
            from pipeline.images import generate_images_for_beats  # noqa: PLC0415
        except ImportError:
            return self._fallback_solid_color(spec, timeline, work_dir)

        try:
            images = generate_images_for_beats(
                beats=[
                    {"id": s.anchor_id, "text": s.text,
                     "start_s": s.start_s, "end_s": s.end_s}
                    for s in timeline
                ],
                provider=spec.extra.get("image_provider", "cloudrun_flux2_klein"),
                style_prefix=spec.extra.get("image_style_prefix", ""),
                width=spec.output_resolution[0],
                height=spec.output_resolution[1],
                cache_dir=work_dir / "images",
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ai_beat_slideshow: image gen failed (%s) — "
                            "falling back to solid color", exc)
            return self._fallback_solid_color(spec, timeline, work_dir)

        # Stitch the per-beat images into one continuous video using
        # the existing compose helper.
        try:
            from pipeline.compose import build_slideshow_video  # noqa: PLC0415
        except ImportError:
            return self._fallback_solid_color(spec, timeline, work_dir)

        out_path = work_dir / "slideshow.mp4"
        try:
            build_slideshow_video(
                images=images,
                beats=timeline,
                out_path=out_path,
                width=spec.output_resolution[0],
                height=spec.output_resolution[1],
                fps=spec.output_fps,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ai_beat_slideshow: compose failed (%s) — "
                            "falling back to solid color", exc)
            return self._fallback_solid_color(spec, timeline, work_dir)

        return VisualTrack(
            video_path=out_path,
            duration_s=probe_duration(out_path),
            extras={
                "source": "ai_beat_slideshow",
                "n_images": len(images),
                "images": [str(p) for p in images],
            },
        )

    def _fallback_solid_color(
        self, spec: Any, timeline: Timeline, work_dir: Path,
    ) -> VisualTrack:
        out_path = work_dir / "slideshow_fallback.mp4"
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
            extras={"source": "ai_beat_slideshow_fallback"},
        )


register_plugin("visualize", "ai_beat_slideshow", AiBeatSlideshow())
assert isinstance(AiBeatSlideshow(), VisualProducer)


__all__ = ["AiBeatSlideshow"]
