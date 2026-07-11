"""Shared ffmpeg/ffprobe helpers used by every render engine + plugin.

Both ``short_engine`` and ``long_engine`` plus every plugin under
``visualize/``, ``overlays/``, ``music/``, ``compose/`` import the
canonical helpers (``run_ffmpeg``, ``apply_atempo``, ``probe_duration``)
from ONE place here.

Design notes
------------

Public names (no leading underscore) are the canonical helpers new
code should use. Behaviour: stderr capture + optional timeout (Tier-0
batch-E hardening), ``probe_wav_params`` (Audit T1.15), concat-safe
path escaping (Audit Q2.25).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from pipeline.render.telemetry_helpers import path_size, track_event


def _ffmpeg_output_path(args: list[str], output_path: Path | str | None = None) -> Path | None:
    if output_path is not None:
        return Path(output_path)
    try:
        if args and args[-1] and not str(args[-1]).startswith("-") and str(args[-1]) != "-":
            return Path(args[-1])
    except Exception:  # noqa: BLE001
        pass
    return None


def run_ffmpeg(
    args: list[str],
    *,
    purpose: str = "unspecified",
    timeout: float | None = None,
    output_path: Path | str | None = None,
) -> None:
    """Run ffmpeg with stderr captured.

    Tier-0 batch E (2026-05-14) hardening:
    - stderr is captured so failures surface the actual ffmpeg error
      message in the RuntimeError (pre-fix R-30: stderr went to the
      worker tail buffer and got overwritten by OTel metric dumps,
      leaving us with opaque "ffmpeg failed: ..." messages).
    - Optional timeout (seconds). None = no timeout (default; preserves
      existing behaviour for the multi-minute long-form compose calls).
      Callers that know their command should finish in bounded time
      SHOULD pass a value.

    On non-zero exit, raises RuntimeError that includes the last 1500
    chars of stderr so the worker subprocess tail captured by
    ``cloud/render-worker-v2/entrypoint.py`` actually carries the root
    cause.
    """
    full_cmd = ["ffmpeg", "-y", "-loglevel", "error", *args]
    out_path = _ffmpeg_output_path(args, output_path)
    t0 = time.perf_counter()
    proc: subprocess.CompletedProcess[str] | None = None
    try:
        proc = subprocess.run(
            full_cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        track_event(
            "ffmpeg.call",
            category="ffmpeg",
            success=False,
            duration_ms=duration_ms,
            metadata={
                "purpose": purpose,
                "args": full_cmd,
                "exit_code": -1,
                "stderr_tail": str(e)[-4000:],
                "output_bytes": path_size(out_path),
            },
        )
        raise RuntimeError(
            f"ffmpeg timed out after {timeout}s: {' '.join(args[:6])}…"
        ) from e
    except Exception as e:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - t0) * 1000)
        track_event(
            "ffmpeg.call",
            category="ffmpeg",
            success=False,
            duration_ms=duration_ms,
            metadata={
                "purpose": purpose,
                "args": full_cmd,
                "exit_code": -1,
                "stderr_tail": str(e)[-4000:],
                "output_bytes": path_size(out_path),
            },
        )
        raise

    duration_ms = int((time.perf_counter() - t0) * 1000)
    stderr = proc.stderr or ""
    track_event(
        "ffmpeg.call",
        category="ffmpeg",
        success=proc.returncode == 0,
        duration_ms=duration_ms,
        metadata={
            "purpose": purpose,
            "args": full_cmd,
            "exit_code": proc.returncode,
            "stderr_tail": stderr[-4000:],
            "output_bytes": path_size(out_path),
        },
    )
    if proc.returncode != 0:
        stderr_tail = stderr[-1500:].strip()
        err = RuntimeError(
            f"ffmpeg failed (exit={proc.returncode}): "
            f"{' '.join(args[:6])}…\nstderr:\n{stderr_tail}"
        )
        setattr(err, "returncode", proc.returncode)
        setattr(err, "stderr", stderr)
        setattr(err, "cmd", full_cmd)
        raise err
    return None


def apply_atempo(in_wav: Path, out_wav: Path, factor: float) -> None:
    """Apply an ffmpeg ``atempo`` filter to ``in_wav`` → ``out_wav``."""
    run_ffmpeg(
        ["-i", str(in_wav), "-filter:a", f"atempo={factor}", str(out_wav)],
        purpose="apply_atempo",
    )


def probe_duration(path: Path) -> float:
    """Return the duration in seconds of any media file via ffprobe.

    Delegates to the canonical helper in :mod:`pipeline.probe` so we
    have ONE implementation of duration probing in the codebase.
    """
    from pipeline.probe import probe_duration as _probe_duration  # noqa: PLC0415
    return _probe_duration(path)


__all__ = ["run_ffmpeg", "apply_atempo", "probe_duration"]
