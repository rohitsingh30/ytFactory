"""Shared trim+letterbox helper for every renderer + plugin.

Pre-2026-05-14 lived as ``_trim_clip_letterbox`` inside
``pipeline/render/long_form.py``; sports_doc.py imported it from
there. Promoted here as part of the 4-renderer-to-2-engine
consolidation (plan.md).

Function body is byte-equivalent to the long_form.py original — same
three-tier short-circuit (exact-match stream-copy → aspect-match plain
scale → full split+gblur+overlay chain), same warm-firelight
``grade_filter`` semantics.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline.render.shared.ffmpeg_helpers import run_ffmpeg


def trim_clip_letterbox(
    src: Path, in_s: float, out_s: float, out_path: Path,
    out_w: int = 1920, out_h: int = 1080, fps: int = 30,
    grade_filter: str | None = None,
) -> None:
    """Trim [in_s, out_s] from src, scale to fit ``out_w`` × ``out_h``
    with blurred letterbox.

    For 4:3 sources (640×480, 320×240) this gives a centered scaled-up
    image with a blurred copy of the same frame filling the side bars —
    same aesthetic as the Shorts blurred-letterbox filter, just sideways.

    If ``grade_filter`` is set, it's appended after the overlay step —
    this is where the warm-firelight color grade lives (see
    ``docs/channel-learnings/historyrecapped/long_form_visual_signature.md``).
    Single ffmpeg pass: grade applies to the composited frame so both
    foreground subject and blurred letterbox bars share the same warm
    tone — keeps the lantern-lit feel consistent across letterboxed
    4:3 archival sources.

    Three-tier short-circuit (in order, first match wins):

    1. **Exact match + no grade** → ``-c:v copy`` stream-copy. Fastest;
       no re-encode at all.
    2. **Aspect match (within 1%) + no grade**, any source resolution
       → plain ``scale + setsar=1`` re-encode. Skips the
       ``split→gblur sigma=22→overlay`` chain entirely.
    3. **Otherwise** (4:3 source, grade requested, or aspect mismatch)
       → full split+gblur+overlay chain. Required for letterboxing
       4:3 archival into 16:9 and for warm-firelight grading.
    """
    duration = max(0.1, out_s - in_s)
    src_w: int | None = None
    src_h: int | None = None
    try:
        # Tier-0 batch E (2026-05-14): cap ffprobe at 30s so a hung
        # probe (corrupt source / network mount stall) can't block
        # the entire trim_clip_letterbox call indefinitely. R-19 in
        # the audit catalogue. Typical local probe is <100ms.
        probe = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "default=noprint_wrappers=1:nokey=1", str(src),
        ], timeout=30).decode().strip().splitlines()
        src_w, src_h = int(probe[0]), int(probe[1])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            ValueError, IndexError):
        pass  # fall through to full chain below

    if grade_filter is None and src_w and src_h:
        # Tier 1: exact match → stream copy (fastest).
        if src_w == out_w and src_h == out_h:
            print(f"[trim] aspect-match {src_w}x{src_h} == {out_w}x{out_h} — stream-copy")
            run_ffmpeg([
                "-ss", f"{in_s}", "-t", f"{duration}", "-i", str(src),
                "-an", "-c:v", "copy", str(out_path),
            ], purpose="trim_letterbox_stream_copy")
            return
        # Tier 2: aspect match within 1% but different resolution → plain scale.
        # Skip the split+gblur+overlay chain entirely — it's a no-op when the
        # source already covers the output canvas.
        src_ratio = src_w / src_h
        out_ratio = out_w / out_h
        aspect_match = abs(src_ratio - out_ratio) / out_ratio < 0.01
        if aspect_match:
            print(f"[trim] aspect-match {src_w}x{src_h} ~ {out_w}x{out_h} — plain scale (no gblur)")
            vf = f"scale={out_w}:{out_h}:flags=lanczos,setsar=1,fps={fps},format=yuv420p"
            run_ffmpeg([
                "-ss", f"{in_s}", "-t", f"{duration}", "-i", str(src),
                "-vf", vf, "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-threads", "3",
                "-pix_fmt", "yuv420p",
                str(out_path),
            ], purpose="trim_letterbox_plain_scale")
            return

    grade_tail = f",{grade_filter}" if grade_filter else ""
    vf = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},gblur=sigma=22[bg2];"
        f"[fg]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[fg2];"
        f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2,fps={fps}{grade_tail},format=yuv420p"
    )
    run_ffmpeg([
        "-ss", f"{in_s}", "-t", f"{duration}", "-i", str(src),
        "-filter_complex", vf,
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-threads", "3",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ], purpose="trim_letterbox_blur")


__all__ = ["trim_clip_letterbox"]
