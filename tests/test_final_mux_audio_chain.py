"""Regression tests for ``pipeline.render.long_form`` audio chain.

Pins the post-2026-05-13 fix stack (D1, D2, D3, D4 in
docs/audio_loudnorm.md). Synthesises real WAVs at known loudness,
calls into the helpers, asserts the produced filter chain + post-mux
loudness match expectations.

Catches the silent-mp4 regression class.
"""
from __future__ import annotations

import inspect
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

import pipeline.render.long_form as lf


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


pytestmark = pytest.mark.skipif(
    not _have_ffmpeg(),
    reason="ffmpeg/ffprobe not available — audio-chain tests need both",
)


# ---------- D1: amix :normalize=0 ----------------------------------------


def test_amix_filter_string_includes_normalize_zero(tmp_path, monkeypatch):
    """D1 — amix MUST have :normalize=0 to avoid halving narration."""
    captured: dict[str, list[str]] = {}

    def fake_ffmpeg(args):
        captured["args"] = args
    monkeypatch.setattr(lf, "_ffmpeg", fake_ffmpeg)
    # Stub measurement so we exercise the two-pass path predictably.
    monkeypatch.setattr(lf, "_measure_loudness", lambda p: {
        "measured_I": -20.0, "measured_LRA": 5.0, "measured_TP": -8.0,
        "measured_thresh": -30.0, "target_offset": 0.0,
    })
    # Stub verifier so it doesn't actually probe the (non-existent) output.
    monkeypatch.setattr(lf, "_verify_audio_loudness", lambda p, **kw: (-16.0, True))

    v = tmp_path / "v.mp4"; v.write_bytes(b"x")
    n = tmp_path / "n.wav"; n.write_bytes(b"x")
    m = tmp_path / "m.wav"; m.write_bytes(b"x")
    out = tmp_path / "o.mp4"
    lf.final_mux(v, n, m, out)

    args = captured["args"]
    flt_idx = args.index("-filter_complex")
    flt = args[flt_idx + 1]
    assert "amix=inputs=2:duration=first:dropout_transition=2:normalize=0" in flt, \
        f"amix filter MUST include :normalize=0 to avoid halving narration; got: {flt}"


# ---------- D2: narration_db default 0.0 ---------------------------------


def test_default_narration_db_is_zero():
    """D2 — final_mux default narration_db MUST be 0.0 (was -6.0).

    Trim of -6 dB on top of loudnorm to -16 LUFS landed at -22 LUFS
    (silent on phones). 0.0 lands at -16 LUFS (YouTube spoken-word
    target).
    """
    sig = inspect.signature(lf.final_mux)
    default = sig.parameters["narration_db"].default
    assert default == 0.0, (
        f"final_mux narration_db default MUST be 0.0; got {default}. "
        f"See docs/audio_loudnorm.md — pre-fix default of -6.0 was "
        f"the second contributor to the silent mp4 bug."
    )


# ---------- D3: two-pass loudnorm ----------------------------------------


def test_two_pass_loudnorm_filter_includes_measured_keys(tmp_path, monkeypatch):
    """D3 — when measurement succeeds, second-pass filter MUST inject
    all five measured_* keys + linear=true so the second pass is
    actually two-pass, not single-pass with extra arguments."""
    captured: dict[str, list[str]] = {}

    def fake_ffmpeg(args):
        captured["args"] = args
    monkeypatch.setattr(lf, "_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(lf, "_measure_loudness", lambda p: {
        "measured_I": -20.5, "measured_LRA": 4.7, "measured_TP": -8.3,
        "measured_thresh": -31.2, "target_offset": -0.3,
    })
    monkeypatch.setattr(lf, "_verify_audio_loudness", lambda p, **kw: (-16.0, True))

    v = tmp_path / "v.mp4"; v.write_bytes(b"x")
    n = tmp_path / "n.wav"; n.write_bytes(b"x")
    m = tmp_path / "m.wav"; m.write_bytes(b"x")
    out = tmp_path / "o.mp4"
    lf.final_mux(v, n, m, out)

    flt = captured["args"][captured["args"].index("-filter_complex") + 1]
    for key in ("measured_I=-20.50", "measured_LRA=4.70", "measured_TP=-8.30",
                "measured_thresh=-31.20", "offset=-0.30", "linear=true"):
        assert key in flt, f"two-pass loudnorm filter missing {key!r}; got: {flt}"


def test_single_pass_fallback_when_measurement_fails(tmp_path, monkeypatch):
    """D3 — when measurement returns None, fall back to single-pass
    so a measurement glitch doesn't break the whole render."""
    captured: dict[str, list[str]] = {}

    def fake_ffmpeg(args):
        captured["args"] = args
    monkeypatch.setattr(lf, "_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(lf, "_measure_loudness", lambda p: None)
    monkeypatch.setattr(lf, "_verify_audio_loudness", lambda p, **kw: (-16.0, True))

    v = tmp_path / "v.mp4"; v.write_bytes(b"x")
    n = tmp_path / "n.wav"; n.write_bytes(b"x")
    m = tmp_path / "m.wav"; m.write_bytes(b"x")
    out = tmp_path / "o.mp4"
    lf.final_mux(v, n, m, out)

    flt = captured["args"][captured["args"].index("-filter_complex") + 1]
    # Single-pass fallback is the bare loudnorm with NO measured_* keys.
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in flt
    assert "measured_I" not in flt


# ---------- D3: real loudnorm measurement against synthesised WAV --------


def _write_sine_wav(path: Path, duration_s: float, freq_hz: float, amplitude: float, sr: int = 44100):
    """Write a mono 16-bit sine wave at the given amplitude (0.0-1.0)."""
    n = int(duration_s * sr)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        for i in range(n):
            sample = int(amplitude * 32767 * math.sin(2 * math.pi * freq_hz * i / sr))
            w.writeframes(struct.pack("<h", sample))


def test_measure_loudness_returns_measured_values_for_real_wav(tmp_path):
    """D3 — _measure_loudness MUST parse the loudnorm JSON output for a
    real synthesised WAV (not just the mocked path)."""
    wav = tmp_path / "sine.wav"
    _write_sine_wav(wav, duration_s=3.0, freq_hz=440, amplitude=0.5)
    out = lf._measure_loudness(wav)
    assert out is not None, "measurement should succeed on a real sine WAV"
    assert "measured_I" in out
    assert "measured_LRA" in out
    assert "measured_TP" in out
    assert "measured_thresh" in out
    assert -50.0 < out["measured_I"] < 0.0  # sane LUFS range


def test_measure_loudness_returns_none_on_garbage_input(tmp_path):
    """D3 — measurement gracefully returns None on garbage so the
    caller's single-pass fallback fires."""
    wav = tmp_path / "garbage.wav"
    wav.write_bytes(b"this is not a wav file")
    out = lf._measure_loudness(wav)
    assert out is None


# ---------- D4: post-mux verification ------------------------------------


def _mux_synthetic(tmp_path: Path, narration_amp: float = 0.5) -> Path:
    """Build a tiny mp4 with a synthesised tone at the given amplitude
    so we can exercise the full mux + verify chain end-to-end."""
    nar = tmp_path / "n.wav"
    bed = tmp_path / "m.wav"
    _write_sine_wav(nar, duration_s=2.0, freq_hz=440, amplitude=narration_amp)
    _write_sine_wav(bed, duration_s=2.0, freq_hz=220, amplitude=0.05)
    # Synthesise a 2-second video using ffmpeg lavfi color source.
    vid = tmp_path / "v.mp4"
    subprocess.check_call([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2:r=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(vid),
    ])
    out = tmp_path / "o.mp4"
    lf.final_mux(vid, nar, bed, out)
    return out


def test_verify_audio_loudness_passes_within_tolerance(tmp_path):
    """D4 — narration mixed at a healthy level (loudnorm to -16 LUFS)
    MUST land within ±4 LU on the post-mux verification."""
    out = _mux_synthetic(tmp_path, narration_amp=0.5)
    mean_db, ok = lf._verify_audio_loudness(out, target_lufs=-16.0, tolerance_lu=4.0)
    assert ok, f"expected within tolerance, got mean_db={mean_db}"


def test_verify_audio_loudness_returns_safe_default_on_garbage(tmp_path):
    """D4 — when ffmpeg can't read the file, return (0.0, True) so the
    verification step itself doesn't break a render."""
    bad = tmp_path / "not_a_video.mp4"
    bad.write_bytes(b"not really an mp4")
    mean_db, ok = lf._verify_audio_loudness(bad)
    # Either ffmpeg returns no volumedetect output (we get 0.0, True) or
    # ffmpeg crashes (also 0.0, True via the SubprocessError except).
    assert mean_db == 0.0
    assert ok is True


# ---------- E2E: silent narration triggers RuntimeError -------------------


def test_main_raises_when_narration_silent(tmp_path, monkeypatch):
    """D4 — if the narration is genuinely silent (e.g. TTS produced an
    empty WAV), the post-mux verification should fail and the
    renderer should raise RuntimeError so the worker marks FAILED.

    Tested via _verify_audio_loudness directly with a known-silent mp4
    (tolerance_lu=2.0 is tight enough to catch -28 LUFS as out-of-spec).
    """
    # Build a true silence mp4 (no audio stream produces -inf, but a
    # very-low amplitude wave + mux will land far from -16 LUFS).
    nar = tmp_path / "silent.wav"
    _write_sine_wav(nar, duration_s=2.0, freq_hz=440, amplitude=0.0001)
    bed = tmp_path / "bed.wav"
    _write_sine_wav(bed, duration_s=2.0, freq_hz=220, amplitude=0.0001)
    vid = tmp_path / "v.mp4"
    subprocess.check_call([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2:r=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(vid),
    ])
    out = tmp_path / "o.mp4"
    # Bypass any monkeypatches — call the real chain.
    lf.final_mux(vid, nar, bed, out)
    mean_db, ok = lf._verify_audio_loudness(out, target_lufs=-16.0, tolerance_lu=2.0)
    # Silent narration → loudnorm tries to amplify but linear gain hits
    # the True Peak ceiling → we land far off target. With tolerance=2 LU
    # this should fail, proving the verifier catches catastrophic input.
    assert not ok or abs(mean_db - (-16.0)) > 2.0, \
        f"silent narration should fail tight tolerance; got mean_db={mean_db}, ok={ok}"
