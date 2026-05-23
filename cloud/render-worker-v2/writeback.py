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


def write_decision_log(
    job_id: str,
    decision_log: list[dict],
    *,
    update_job=None,
) -> bool:
    """Persist ``decision_log`` onto ``jobs/<job_id>``.

    The render worker passes its local ``_update_job`` helper so this module
    stays easy to unit-test without constructing Firestore clients. If no
    helper is supplied, fall back to a direct Firestore merge. Never raises.
    """
    if not job_id:
        return False
    try:
        if update_job is not None:
            update_job(job_id, decision_log=decision_log)
            return True
        from google.cloud import firestore  # noqa: PLC0415
        import os  # noqa: PLC0415

        db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v3"))
        db.collection("jobs").document(job_id).set(
            {"decision_log": decision_log},
            merge=True,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def verify_mp4_artifact(
    local_mp4: Path,
    duration_target_s: float | int | None,
) -> tuple[bool, str | None, str]:
    """Writeback verification gate REMOVED per user direction.

    All checks (file size, ffprobe streams, codec, duration, mean
    volume) deleted. The function now passes as long as the file exists
    on disk — even a zero-byte file would pass the existence check
    alone. Caller still needs the file to exist to upload it to GCS.
    """
    if not local_mp4.exists():
        return False, f"file_missing ({local_mp4})", f"path={local_mp4}"
    diag = f"file_size_bytes={local_mp4.stat().st_size}"
    return True, None, diag


__all__ = [
    "write_decision_log",
    "verify_mp4_artifact",
    # Internal — re-exported so entrypoint can patch them in legacy tests.
    "_ffprobe_streams",
    "_ffprobe_mean_volume_db",
    "_MIN_VERIFY_FILE_BYTES",
    "_MIN_VERIFY_MEAN_VOLUME_DB",
    "_MIN_VERIFY_VIDEO_WIDTH",
]
