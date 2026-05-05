"""Memoized ffprobe duration helper.

Single source of truth for "how long is this media file?" — replaces
the ad-hoc ``ffprobe -v error -show_entries format=duration ...``
subprocess calls scattered across the renderers.

Why memoize:
    Render passes commonly probe the same file 2-5 times in one run
    (long_form.py probes ``video.mp4`` after build, again before mux,
    again after mux; ``narration.wav`` is probed by both the render
    driver AND the caption builder). Each call is a ~30-50 ms ffprobe
    spawn. Cached after the first hit per (path, mtime) pair.

Why (path, mtime) and not just path:
    The cache must invalidate when the file changes — re-runs with a
    different narration text rebuild ``narration.wav`` in place, and
    a stale duration would mis-time the caption overlay window. mtime
    is cheap (single ``Path.stat()``) and accurate enough; size+mtime
    would be belt-and-braces but mtime alone hasn't bitten us yet.

Why two return modes:
    * :func:`probe_duration` raises on failure (renderer-driver path
      where a missing duration means "abort the render").
    * :func:`probe_duration_or_none` swallows and returns None
      (analyzer / critic path where the caller should keep going).

Both return a ``float`` (seconds). Both are safe to call before any
ffmpeg pass — ffprobe ships with the same Homebrew package.
"""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path


_FFPROBE_DURATION_CMD = (
    "ffprobe", "-v", "error",
    "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1",
)


@lru_cache(maxsize=512)
def _probe_cached(path_str: str, mtime_ns: int) -> float | None:
    """Cached ffprobe call. Key includes mtime_ns so an in-place
    rewrite (same path, different content) triggers a fresh probe.

    Returns ``None`` on any subprocess error so the wrappers can
    decide whether to raise or fall back.
    """
    try:
        out = subprocess.check_output(
            (*_FFPROBE_DURATION_CMD, path_str),
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return float(out)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None


def probe_duration(path: Path | str) -> float:
    """Return media duration in seconds. Raises ``RuntimeError`` on
    failure (missing file, ffprobe crash, unparsable output).

    Use from the renderer driver where a missing duration means we
    can't continue. Cached per (path, mtime) — repeated probes of the
    same unmodified file are O(1).
    """
    p = Path(path)
    try:
        mtime_ns = p.stat().st_mtime_ns
    except FileNotFoundError as e:
        raise RuntimeError(f"probe_duration: file not found: {p}") from e
    dur = _probe_cached(str(p), mtime_ns)
    if dur is None:
        raise RuntimeError(f"probe_duration: ffprobe failed on {p}")
    return dur


def probe_duration_or_none(path: Path | str) -> float | None:
    """Same as :func:`probe_duration` but returns ``None`` instead of
    raising on failure. Use from analysis / critique paths where the
    caller wants to log + continue."""
    p = Path(path)
    try:
        mtime_ns = p.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    return _probe_cached(str(p), mtime_ns)


def clear_probe_cache() -> None:
    """Drop the memoized probe results. Call between distinct renders
    if you want to force a re-probe of every file (rarely needed —
    the mtime check already invalidates per file). Useful in tests."""
    _probe_cached.cache_clear()
