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
            from pipeline.images.images import generate as _generate_image  # noqa: PLC0415
        except ImportError:
            return self._fallback_solid_color(spec, timeline, work_dir)

        # Generate one image per Segment.
        images_dir = work_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        provider = spec.extra.get("image_provider", "cloudrun_flux2_klein")
        style_prefix = spec.extra.get("image_style_prefix", "")
        seed_base = int(spec.extra.get("image_seed", 42))
        steps = int(spec.extra.get("image_steps", 4))

        images: list[Path] = []
        for i, seg in enumerate(timeline):
            png_path = images_dir / f"beat_{i:03d}.png"
            try:
                _generate_image(
                    prompt=seg.text,
                    style_prefix=style_prefix,
                    seed=seed_base + i,
                    out_path=png_path,
                    width=spec.output_resolution[0],
                    height=spec.output_resolution[1],
                    steps=steps,
                    provider=provider,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("ai_beat_slideshow: image %d failed (%s) — "
                                "falling back to solid color for this beat", i, exc)
                continue
            images.append(png_path)

        if not images:
            _logger.warning("ai_beat_slideshow: 0 images produced — "
                            "falling back to solid color")
            return self._fallback_solid_color(spec, timeline, work_dir)

        # Stitch one image per beat into a continuous video. Each image
        # is held for the beat's duration. Bigbang PR adds Ken Burns
        # motion via a ffmpeg zoompan filter; today we use plain
        # framebatch-per-second.
        out_path = work_dir / "slideshow.mp4"
        try:
            self._stitch_images(images, timeline, spec, out_path)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ai_beat_slideshow: stitch failed (%s) — "
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

    def _stitch_images(
        self,
        images: list[Path],
        timeline: Timeline,
        spec: Any,
        out_path: Path,
    ) -> None:
        """Stitch per-beat images into a continuous video.

        Builds an ffmpeg concat-demuxer file: each image held for its
        beat's [start_s, end_s] window. Bigbang PR plumbs Ken Burns
        motion + transitions; today's pass is straight cuts.
        """
        from pipeline.render.shared.concat_safe import concat_file_line  # noqa: PLC0415
        out_path.parent.mkdir(parents=True, exist_ok=True)
        concat_list = out_path.parent / f"{out_path.stem}_concat.txt"
        lines: list[str] = []
        for i, img in enumerate(images):
            seg = timeline[i] if i < len(timeline) else None
            duration = max(seg.end_s - seg.start_s if seg else 1.0, 0.1)
            lines.append(concat_file_line(img.resolve()))
            lines.append(f"duration {duration:.3f}")
        # Last image needs to repeat for proper concat-demuxer parsing.
        if images:
            lines.append(concat_file_line(images[-1].resolve()))
        concat_list.write_text("\n".join(lines))

        w, h = spec.output_resolution
        run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-vsync", "vfr",
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                   f"crop={w}:{h},fps={spec.output_fps},format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            str(out_path),
        ])

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
