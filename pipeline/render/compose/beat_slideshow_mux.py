"""Short engine FinalMux — beat-slideshow shape.

Stacks visual track + audio + music + every overlay into the final
mp4. Layout-agnostic — doesn't care which OverlayProducer impls
contributed which OverlayElements.

Defaults match shorts.py's historical mux: 1080×1920 (9:16), 30 fps,
H.264, AAC 192k. spec.output_resolution + spec.output_fps override.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    AudioResult,
    FinalMux,
    OverlayElement,
    VisualTrack,
    register_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import run_ffmpeg


class BeatSlideshowMux:
    """Mux for the short engine.

    ffmpeg invocation: visuals + audio + music mixed at
    ``spec.music.mix_default`` + each overlay composited on top by
    ``(layer, start_s)``. Output codec: H.264 (libx264) + AAC 192k,
    yuv420p pixel format, fps from spec.output_fps.
    """

    def mux(
        self,
        visuals: VisualTrack,
        audio: AudioResult,
        overlays: list[OverlayElement],
        music: Path,
        spec: Any,  # RenderSpec
        out_path: Path,
    ) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Build the input list. Overlays are sorted by (layer, start_s)
        # so the ffmpeg overlay chain stacks them in the expected order.
        sorted_overlays = sorted(overlays, key=lambda o: (o.layer, o.start_s))

        cmd: list[str] = [
            "-i", str(visuals.video_path),
            "-i", str(audio.narration_path),
            "-i", str(music),
        ]
        for ov in sorted_overlays:
            cmd.extend(["-i", str(ov.asset_path)])

        # Filter chain — visual is [0:v], narration [1:a], music [2:a],
        # overlays start at [3:v]/[4:v]/...
        # Audio mix: narration at full + music ducked to spec.music.mix_default.
        mix_factor = spec.music.mix_default
        filter_parts = [
            # Mix narration (full volume) + music (ducked).
            f"[1:a]volume=1.0[narr]",
            f"[2:a]volume={mix_factor:.3f}[bed]",
            f"[narr][bed]amix=inputs=2:duration=first[aout]",
        ]

        # Stack overlays: [0:v] → [v0]; [v0]+[3:v] overlay → [v1]; etc.
        cur_label = "0:v"
        for i, ov in enumerate(sorted_overlays):
            in_label = f"{i + 3}:v"
            out_label = f"v{i + 1}"
            x_y = self._overlay_xy(ov, spec)
            enable = f"between(t,{ov.start_s:.3f},{ov.end_s:.3f})"
            filter_parts.append(
                f"[{cur_label}][{in_label}]"
                f"overlay=x={x_y[0]}:y={x_y[1]}:enable='{enable}'"
                f"[{out_label}]"
            )
            cur_label = out_label

        # Final video pad — add fps + format.
        # 2026-05-15 — pad visual to audio.duration_s so the video stream
        # doesn't end early when visual_track.duration_s < audio.duration_s.
        # Pre-fix the AITA Short (job d3d5b40b) had visual=26s + audio=40.5s →
        # 14.5s of audio with no video. ffmpeg cannot extend a video past
        # its source duration without an explicit pad filter; ``-t {audio.duration_s}``
        # CAPS the output to that length but doesn't EXTEND visuals.
        # ``tpad=stop_mode=clone:stop_duration=N`` clones the last frame
        # for the audio overrun (cleaner than a black hold). Computed
        # delta is max(0, audio - visual); when visual >= audio,
        # stop_duration=0 is a no-op.
        w, h = spec.output_resolution
        visual_dur = max(0.001, visuals.duration_s)
        pad_seconds = max(0.0, audio.duration_s - visual_dur)
        filter_parts.append(
            f"[{cur_label}]scale={w}:{h}:flags=lanczos,fps={spec.output_fps},"
            f"tpad=stop_mode=clone:stop_duration={pad_seconds:.3f},"
            f"format=yuv420p[vout]"
        )

        cmd.extend([
            "-filter_complex", ";".join(filter_parts),
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-threads", "3",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{audio.duration_s:.3f}",
            str(out_path),
        ])
        run_ffmpeg(cmd)
        return out_path

    def _overlay_xy(self, ov: OverlayElement, spec: Any) -> tuple[str, str]:
        """Compute ffmpeg overlay (x, y) expressions for an OverlayElement.

        If ``ov.region`` is set, use it. Otherwise default by layer:

        - Caption layers (40) → bottom-centered with margin.
        - Lower-third layer (20) → bottom-left with margin.
        - Chapter-card layer (30) → full-frame center.
        - Foreground footage layer (10) → full-frame center.
        """
        if ov.region is not None:
            x, y, _, _ = ov.region
            return (str(x), str(y))
        if ov.layer == 40:  # captions
            return ("(W-w)/2", "H-h-200")  # 200px from bottom (chin clearance)
        if ov.layer == 20:  # lower-third
            return ("48", "H-h-48")
        # chapter cards / foreground footage / default → centered
        return ("(W-w)/2", "(H-h)/2")


register_plugin("compose", "beat_slideshow", BeatSlideshowMux())
assert isinstance(BeatSlideshowMux(), FinalMux)


__all__ = ["BeatSlideshowMux"]
