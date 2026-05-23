"""Short engine FinalMux — beat-slideshow shape.

Stacks visual track + audio + music + every overlay into the final
mp4. Layout-agnostic — doesn't care which OverlayProducer impls
contributed which OverlayElements.

Defaults match shorts.py's historical mux: 1080×1920 (9:16), 30 fps,
H.264, AAC 192k. spec.output_resolution + spec.output_fps override.
"""
from __future__ import annotations

import time
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
from pipeline.render.telemetry_helpers import emit_json_artifact, path_size, track_event


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

        # 2026-05-17 split overlays by format. ASS overlays burn via
        # ffmpeg's ``subtitles=`` filter (single pass, scales to
        # thousands of events). Image overlays use the ffmpeg
        # ``overlay=`` filter chain (one per element).
        #
        # Why the split: the WordCaptionPngs producer (post-fix)
        # returns ONE ASS OverlayElement for 100+ word events instead
        # of 100+ PNG OverlayElements. Pre-fix the 114-overlay chain
        # silently failed past ~50 overlays (render 75ac2667 — 37min
        # compose, ZERO captions in output).
        ass_overlays = [
            o for o in overlays
            if (o.extras or {}).get("format") == "ass"
        ]
        image_overlays = [
            o for o in overlays
            if (o.extras or {}).get("format") != "ass"
        ]
        sorted_overlays = sorted(image_overlays, key=lambda o: (o.layer, o.start_s))

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

        # Final video pad — fit visual track to audio.duration_s.
        #
        # 2026-05-15 — pre-fix the AITA Short (job d3d5b40b) had
        # visual=26s + audio=40.5s → 14.5s of audio with no video.
        # First fix: tpad=stop_mode=clone froze the last frame for the
        # overrun (cleaner than a black hold, but still a static tail).
        #
        # 2026-05-18 (round 7) — replaced tpad-clone with setpts stretch.
        # After the image_to_static_clip frame-cap fix in compose.py,
        # the AI beat slideshow is now exactly ``sum(beat_durations) +
        # XFADE_per_beat`` ≈ 27s for a 13-preliminary-beat AITA Short.
        # Audio is 41s. tpad-clone would freeze 14s of the last image
        # (beat_012 static tail) — viewer registers it as a frozen-frame
        # bug. setpts smoothly stretches the slideshow PTS so all 13
        # beats spread across the entire audio duration. The per-image
        # zoom plays ~1.5× slower but stays continuous; no frozen tail.
        #
        # When visual_dur >= audio.duration_s (longer authored scripts
        # or post-ASR re-stitch), stretch_factor=1.0 and setpts is a
        # no-op — final -t cap trims any visual overrun.
        #
        # 2026-05-17 ASS subtitle burn — after the scale/fps/setpts
        # chain, append ``subtitles=<path>`` for each ASS overlay so
        # libass renders the word-by-word captions in a single pass on
        # top of all image overlays + per-image zoom. Up to thousands
        # of events; no filter_complex blowup.
        w, h = spec.output_resolution
        visual_dur = max(0.001, visuals.duration_s)
        stretch_factor = max(1.0, audio.duration_s / visual_dur)
        post_chain = (
            f"setpts=PTS*{stretch_factor:.6f},"
            f"scale={w}:{h}:flags=lanczos,fps={spec.output_fps}"
        )
        for ov in ass_overlays:
            # ASS path must be ffmpeg-filter-safe (escape colons + backslashes).
            ass_path = str(ov.asset_path).replace("\\", "\\\\").replace(":", "\\:")
            post_chain += f",subtitles='{ass_path}'"
        post_chain += ",format=yuv420p"
        filter_parts.append(f"[{cur_label}]{post_chain}[vout]")

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
        purpose = self._compose_purpose()
        full_args = ["ffmpeg", "-y", "-loglevel", "error", *cmd]
        t0 = time.perf_counter()
        proc = None
        success = False
        stderr = ""
        exit_code = -1
        try:
            proc = run_ffmpeg(cmd, purpose=purpose, output_path=out_path)
            exit_code = int(getattr(proc, "returncode", 0) or 0)
            success = exit_code == 0
            stderr = getattr(proc, "stderr", "") or ""
            return out_path
        except Exception as exc:  # noqa: BLE001
            stderr = getattr(exc, "stderr", "") or str(exc)
            exit_code = int(getattr(exc, "returncode", -1) or -1)
            raise
        finally:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            output_bytes = path_size(out_path)
            metadata = {
                "purpose": purpose,
                "inputs": [
                    str(visuals.video_path),
                    str(audio.narration_path),
                    str(music),
                    *[str(o.asset_path) for o in sorted_overlays],
                    *[str(o.asset_path) for o in ass_overlays],
                ],
                "output_path": str(out_path),
                "output_bytes": output_bytes,
                "duration_ms": duration_ms,
            }
            track_event(
                "compose.run",
                category="pipeline",
                success=success,
                duration_ms=duration_ms,
                metadata=metadata,
            )
            emit_json_artifact(
                "compose",
                {
                    "args": full_args,
                    "output": str(out_path),
                    "output_bytes": output_bytes,
                    "exit_code": exit_code,
                    "stderr_tail": stderr[-2000:],
                },
            )

    def _compose_purpose(self) -> str:
        return str(getattr(self, "_telemetry_purpose", "beat_slideshow_mux"))

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
