"""Tolerance-band assertion helpers for engine integration goldens.

The engine goldens (tests/render/test_short_engine_golden.py and
tests/render/test_long_engine_golden.py) run the full engine pipeline
end-to-end with REAL plugins (cloud TTS / cloud whisper / Flux when
available; deterministic fixture variants in CI). The output mp4 is
compared against expected metadata using TOLERANCE BANDS, not exact
byte hashes — see plan.md Part D4 for the rationale.

Why tolerance bands not exact hashes
-------------------------------------

Cloud-driven plugins (cloudrun_chatterbox / cloudrun_whisper / Flux)
are deterministic with pinned model versions, but cloud services can
get redeployed and outputs drift slightly across model updates. Exact
byte-hash assertions would break the goldens every time a service
updates, leading to test-disabling pressure. Tolerance bands stay
stable across minor model drift while still catching real regressions
(structural issues, audio mix bugs, overlay placement bugs).

The four assertion families
---------------------------

1. **Structural** — exact match. Codecs, sample rate, resolution,
   stream count. These cannot drift across model updates.
2. **Duration** — within ±1% (default). Catches mux-truncation bugs
   while tolerating cloud-TTS chunk-timing jitter.
3. **Audio loudness** — within ±0.5 dB LUFS (default). Catches
   compose-stage mix bugs (forgotten music bus, double-applied
   loudnorm, etc).
4. **Visual structure** — frame at expected timestamp has non-uniform
   pixels in the expected overlay region. Catches "overlay missing
   from output" without requiring pixel-perfect matches.

Usage
-----

::

    from tests.render.golden_assertions import (
        assert_structural,
        assert_duration_within_pct,
        assert_lufs_within_db,
        assert_overlay_burned,
    )

    assert_structural(out_mp4,
                      codec_video="h264",
                      codec_audio="aac",
                      sample_rate=24000,
                      resolution=(1080, 1920))
    assert_duration_within_pct(out_mp4, expected_s=60.0, pct=1.0)
    assert_lufs_within_db(out_mp4, expected_lufs=-16.0, db=0.5)
    assert_overlay_burned(out_mp4, t_s=10.5,
                          region=(440, 1500, 200, 80))
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# ffprobe helpers
# ---------------------------------------------------------------------------


def _ffprobe_json(path: Path, *args: str) -> dict:
    """Run ffprobe with -show_format -show_streams and return the JSON."""
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        *args,
        str(path),
    ]).decode()
    return json.loads(out)


def _video_stream(path: Path) -> dict | None:
    data = _ffprobe_json(path)
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return None


def _audio_stream(path: Path) -> dict | None:
    data = _ffprobe_json(path)
    for s in data.get("streams", []):
        if s.get("codec_type") == "audio":
            return s
    return None


def _format_duration(path: Path) -> float:
    data = _ffprobe_json(path)
    fmt = data.get("format") or {}
    return float(fmt.get("duration") or 0.0)


# ---------------------------------------------------------------------------
# 1. Structural assertions
# ---------------------------------------------------------------------------


def assert_structural(
    path: Path,
    *,
    codec_video: str = "h264",
    codec_audio: str = "aac",
    sample_rate: int = 24000,
    resolution: tuple[int, int] = (1080, 1920),
) -> None:
    """Assert exact-match structural properties.

    Raises ``AssertionError`` with a focused message on mismatch.
    Pre-2026-05-14 these were checked inline in test files; centralising
    them keeps every golden's structural check consistent.
    """
    if not path.exists():
        raise AssertionError(f"output mp4 missing at {path}")
    if path.stat().st_size == 0:
        raise AssertionError(f"output mp4 is empty at {path}")

    v = _video_stream(path)
    if v is None:
        raise AssertionError(f"no video stream in {path}")
    if v.get("codec_name") != codec_video:
        raise AssertionError(
            f"video codec {v.get('codec_name')!r} != expected {codec_video!r}"
        )
    actual_res = (int(v.get("width") or 0), int(v.get("height") or 0))
    if actual_res != resolution:
        raise AssertionError(
            f"resolution {actual_res} != expected {resolution}"
        )

    a = _audio_stream(path)
    if a is None:
        raise AssertionError(f"no audio stream in {path}")
    if a.get("codec_name") != codec_audio:
        raise AssertionError(
            f"audio codec {a.get('codec_name')!r} != expected {codec_audio!r}"
        )
    actual_sr = int(a.get("sample_rate") or 0)
    if actual_sr != sample_rate:
        raise AssertionError(
            f"sample_rate {actual_sr} != expected {sample_rate}"
        )


# ---------------------------------------------------------------------------
# 2. Duration tolerance
# ---------------------------------------------------------------------------


def assert_duration_within_pct(
    path: Path,
    *,
    expected_s: float,
    pct: float = 1.0,
) -> None:
    """Assert ffprobe duration is within ``pct`` percent of ``expected_s``.

    ``pct`` is a percentage (1.0 = 1%, not 0.01). Tolerance covers
    cloud-TTS chunk-timing jitter that doesn't indicate a real
    regression.
    """
    actual = _format_duration(path)
    if expected_s <= 0:
        raise AssertionError(f"expected_s must be positive (got {expected_s})")
    drift = abs(actual - expected_s) / expected_s * 100.0
    if drift > pct:
        raise AssertionError(
            f"duration drifted {drift:.2f}% from {expected_s}s "
            f"(actual {actual:.3f}s, tolerance ±{pct}%)"
        )


# ---------------------------------------------------------------------------
# 3. Audio loudness tolerance
# ---------------------------------------------------------------------------


_LUFS_RE = re.compile(r"Input Integrated:\s*([-\d.]+)\s*LUFS", re.IGNORECASE)


def measure_lufs(path: Path) -> float:
    """Return the integrated loudness in LUFS via ffmpeg's ``loudnorm`` filter.

    Two-pass loudnorm reports a "summary" line; we parse the integrated
    value. Raises ``AssertionError`` if the measurement can't be parsed
    (typically means ffmpeg / ebur128 is missing).
    """
    proc = subprocess.run([
        "ffmpeg", "-nostats", "-hide_banner",
        "-i", str(path),
        "-af", "loudnorm=print_format=summary",
        "-f", "null", "-",
    ], capture_output=True, text=True)
    # loudnorm prints to stderr.
    text = proc.stderr or ""
    m = _LUFS_RE.search(text)
    if not m:
        raise AssertionError(
            f"ffmpeg loudnorm did not report Input Integrated for {path}.\n"
            f"stderr tail:\n{text[-500:]}"
        )
    return float(m.group(1))


def assert_lufs_within_db(
    path: Path,
    *,
    expected_lufs: float,
    db: float = 0.5,
) -> None:
    """Assert measured LUFS is within ``db`` dB of ``expected_lufs``.

    Default tolerance ±0.5 dB matches the EBU R128 broadcast spec
    delta and gives plenty of headroom for cloud-TTS variance while
    still catching mix-stage regressions (forgotten music bus,
    double-applied loudnorm).
    """
    actual = measure_lufs(path)
    if math.isinf(actual) or math.isnan(actual):
        raise AssertionError(
            f"loudnorm returned non-finite value ({actual}) for {path} — "
            "audio may be silent or measurement failed"
        )
    delta = abs(actual - expected_lufs)
    if delta > db:
        raise AssertionError(
            f"LUFS drifted {delta:.2f} dB from {expected_lufs} "
            f"(actual {actual:.2f}, tolerance ±{db} dB)"
        )


# ---------------------------------------------------------------------------
# 4. Overlay-burn structural check
# ---------------------------------------------------------------------------


def extract_frame(path: Path, t_s: float, *, out_png: Path | None = None) -> Path:
    """Extract a single frame from ``path`` at ``t_s`` to a PNG."""
    out = out_png or (path.parent / f".frame_{int(t_s * 1000):08d}.png")
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{t_s:.3f}",
        "-i", str(path),
        "-frames:v", "1",
        str(out),
    ], check=True, capture_output=True)
    return out


def pixel_variance(png_path: Path, region: tuple[int, int, int, int] | None = None) -> float:
    """Return the per-channel pixel variance of ``png_path``.

    If ``region`` is set (``(x, y, w, h)`` in pixels), only that crop
    is measured. Returns 0.0 for a uniform region (no overlay), >> 0
    for a region with composited content.

    Heavy: opens the PNG via PIL — but PIL is already a dep of
    pipeline.compose so no new install needed.
    """
    from PIL import Image  # noqa: PLC0415
    img = Image.open(png_path).convert("RGB")
    if region is not None:
        x, y, w, h = region
        img = img.crop((x, y, x + w, y + h))
    pixels = list(img.getdata())
    if not pixels:
        return 0.0
    # Per-channel variance, averaged.
    n = len(pixels)
    means = [sum(p[c] for p in pixels) / n for c in range(3)]
    var = [sum((p[c] - means[c]) ** 2 for p in pixels) / n for c in range(3)]
    return sum(var) / 3


def assert_overlay_burned(
    path: Path,
    *,
    t_s: float,
    region: tuple[int, int, int, int],
    min_variance: float = 100.0,
) -> None:
    """Assert that the frame at ``t_s`` has non-uniform pixels in
    ``region`` (i.e. an overlay was composited there).

    Uniform regions have variance ≈ 0; text overlays / chapter cards
    typically have variance > 1000. ``min_variance=100`` is a generous
    threshold that catches "no overlay" while tolerating low-contrast
    overlay styles.
    """
    frame = extract_frame(path, t_s)
    try:
        var = pixel_variance(frame, region=region)
    finally:
        # Cleanup the temp frame.
        if frame.name.startswith(".frame_"):
            frame.unlink(missing_ok=True)
    if var < min_variance:
        raise AssertionError(
            f"expected overlay at t={t_s}s region={region} "
            f"but pixel variance {var:.2f} < threshold {min_variance} "
            f"(region appears uniform — overlay missing or wrong region)"
        )


# ---------------------------------------------------------------------------
# I-frame PSNR (currently unused but documented for future bigbang work)
# ---------------------------------------------------------------------------


def measure_psnr(reference_png: Path, actual_png: Path) -> float:
    """Compute PSNR between two PNGs via ffmpeg's psnr filter.

    Reserved for the future "compare against reference frame" mode
    where we have a stable reference PNG bundled with the test
    fixture. Today's goldens don't use this — they use
    ``assert_overlay_burned`` (presence-only) instead — but the
    helper is here because the bigbang PR will expand the goldens
    to include reference-PNG comparisons for compose stages whose
    output IS pixel-stable (overlay positioning, layer stacking).
    """
    proc = subprocess.run([
        "ffmpeg", "-i", str(reference_png), "-i", str(actual_png),
        "-lavfi", "psnr=stats_file=-",
        "-f", "null", "-",
    ], capture_output=True, text=True)
    # psnr filter prints "average:" line we can parse.
    m = re.search(r"average:([\d.]+)", proc.stderr or "", re.IGNORECASE)
    if not m:
        raise AssertionError(
            f"ffmpeg psnr did not report average for {actual_png}.\n"
            f"stderr tail:\n{(proc.stderr or '')[-500:]}"
        )
    return float(m.group(1))


__all__ = [
    "assert_structural",
    "assert_duration_within_pct",
    "assert_lufs_within_db",
    "assert_overlay_burned",
    "measure_lufs",
    "measure_psnr",
    "extract_frame",
    "pixel_variance",
]
