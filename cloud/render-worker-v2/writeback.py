"""Writeback verifier — post-render sanity checks before GCS upload.

Extracted from ``entrypoint.py`` 2026-05-23 (task #18) as the first
clean seam of the god-file split. The verifier is self-contained —
ffprobe + ffmpeg shells, no Firestore / no GCS dependencies — so
isolating it removes ~180 lines from the 3132-line entrypoint without
touching the orchestration logic.

Public API: ``verify_mp4_artifact(local_mp4, duration_target_s) ->
(passed, failure_reason, diagnostic)``. ``entrypoint.py`` re-exports
the function name unchanged for back-compat with existing tests.

Gate philosophy (P3.7, Q65, ADR-023): the verifier is SANITY ONLY.
Upstream length gates own duration enforcement; the only kill paths
here are "the renderer produced something structurally broken"
(missing file, corrupt mp4, no streams, silent audio).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


# Minimum file size threshold — the empty-blob mp4s that prompted this
# check came in at ~11.8 KB. 100 KB is well below any real ~4s render
# (which clocks ~250 KB+ even at heavy compression) so this rules out
# placeholder shapes without false-positiving on short test renders.
_MIN_VERIFY_FILE_BYTES = 100_000

# Minimum mean_volume dBFS. Audio that loudnorm'd to broadcast level
# clocks around -23 dB. Silent / muted tracks read as -91 dB. -50 dB
# is a safe floor that rejects pure-silence renders without rejecting
# legitimately-quiet narration over a music bed.
_MIN_VERIFY_MEAN_VOLUME_DB = -50.0

# Minimum width — anything below 540 px is sub-540p (the lowest legit
# Short resolution we ever ship) and indicates a stub or downscaled
# placeholder.
_MIN_VERIFY_VIDEO_WIDTH = 540


def _ffprobe_streams(local_mp4: Path) -> dict:
    """Run ffprobe -show_format -show_streams -json on a local mp4 and
    return the parsed dict. Raises ``RuntimeError`` on ffprobe failure
    so the verify path can route the error into a clean failure write."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_format", "-show_streams",
            "-of", "json", str(local_mp4),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe exit={proc.returncode}: {proc.stderr[:500]}"
        )
    return json.loads(proc.stdout or "{}")


def _ffprobe_mean_volume_db(local_mp4: Path) -> float | None:
    """Return the mean_volume (RMS dBFS) reported by ffmpeg's
    volumedetect filter. ``None`` if the filter didn't emit a reading
    (no audio stream, ffmpeg failure, or output without the expected
    ``mean_volume:`` marker)."""
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats",
            "-i", str(local_mp4),
            "-af", "volumedetect", "-vn", "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=60,
    )
    out = (proc.stderr or "") + (proc.stdout or "")
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", out)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def verify_mp4_artifact(
    local_mp4: Path,
    duration_target_s: float | int | None,
) -> tuple[bool, str | None, str]:
    """Verify the locally-produced mp4 meets shippability gates.

    Returns ``(passed, failure_reason, diagnostic)`` — see the
    docstring on ``entrypoint._verify_mp4_artifact`` for the contract.
    """
    diag_parts: list[str] = []

    # Check 1: file size.
    if not local_mp4.exists():
        return False, f"file_missing ({local_mp4})", f"path={local_mp4}"
    size = local_mp4.stat().st_size
    diag_parts.append(f"file_size_bytes={size}")
    if size < _MIN_VERIFY_FILE_BYTES:
        return False, f"file_size ({size} bytes)", "\n".join(diag_parts)

    # Check 2 + 3 + 4: ffprobe streams.
    try:
        probe = _ffprobe_streams(local_mp4)
    except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        diag_parts.append(f"ffprobe_error={e}")
        return False, f"ffprobe_failed ({e})", "\n".join(diag_parts)

    diag_parts.append(f"ffprobe_json={json.dumps(probe)[:2000]}")

    fmt = probe.get("format") or {}
    streams = probe.get("streams") or []

    # Duration sanity (P3.7).
    try:
        actual_dur = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        actual_dur = 0.0
    diag_parts.append(
        f"duration_s={actual_dur:.3f} target={duration_target_s}"
    )
    if actual_dur <= 0:
        return (
            False,
            f"duration ({actual_dur:.2f}s — mp4 reports zero/missing duration)",
            "\n".join(diag_parts),
        )

    # Video stream check.
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    h264_wide = [
        s for s in video_streams
        if (s.get("codec_name") == "h264"
            and int(s.get("width") or 0) >= _MIN_VERIFY_VIDEO_WIDTH)
    ]
    if not h264_wide:
        widths = [s.get("width") for s in video_streams]
        codecs = [s.get("codec_name") for s in video_streams]
        diag_parts.append(f"video_widths={widths} video_codecs={codecs}")
        return (
            False,
            f"video_stream (no h264 ≥{_MIN_VERIFY_VIDEO_WIDTH}px; "
            f"codecs={codecs} widths={widths})",
            "\n".join(diag_parts),
        )

    # Audio stream presence.
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio_streams:
        return False, "audio_stream (no audio stream)", "\n".join(diag_parts)

    # Mean volume.
    try:
        mean_vol = _ffprobe_mean_volume_db(local_mp4)
    except subprocess.TimeoutExpired as e:
        diag_parts.append(f"volumedetect_timeout={e}")
        return False, f"volumedetect_timeout ({e})", "\n".join(diag_parts)
    if mean_vol is None:
        return False, "mean_volume (could not read)", "\n".join(diag_parts)
    diag_parts.append(f"mean_volume_db={mean_vol:.2f}")
    if mean_vol < _MIN_VERIFY_MEAN_VOLUME_DB:
        return (
            False,
            f"mean_volume ({mean_vol:.2f} dB < {_MIN_VERIFY_MEAN_VOLUME_DB} dB)",
            "\n".join(diag_parts),
        )

    return True, None, "\n".join(diag_parts)


__all__ = [
    "verify_mp4_artifact",
    # Internal — re-exported so entrypoint can patch them in legacy tests.
    "_ffprobe_streams",
    "_ffprobe_mean_volume_db",
    "_MIN_VERIFY_FILE_BYTES",
    "_MIN_VERIFY_MEAN_VOLUME_DB",
    "_MIN_VERIFY_VIDEO_WIDTH",
]
