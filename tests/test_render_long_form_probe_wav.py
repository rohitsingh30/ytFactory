"""Pin _probe_wav_params + _wav_concat_with_silence against the real
ffprobe / ffmpeg behaviour the cloud TTS providers force us to handle.

2026-05-13 — job 2cb1a44165a6443eaec1843657118baf failed at long-form
TTS-concat with::

    ffmpeg failed: -f lavfi -t 0.4 -i anullsrc=r=24000:cl=unknown
        -c:a pcm_s16le …/_silence.wav

Root cause: cloud Chatterbox emits 24kHz mono WAVs whose RIFF header
has no channel_layout field, so ``ffprobe`` reports
``channel_layout=unknown``. The pre-fix parser blindly forwarded
``unknown`` into ``anullsrc=cl=unknown`` and ffmpeg refused the entire
render.

These tests pin the fix so a future ffprobe / cloud-TTS change can't
re-regress the same shape silently.
"""
from __future__ import annotations

import struct
import subprocess
from pathlib import Path

import pytest

from pipeline.render._legacy.long_form import _probe_wav_params, _wav_concat_with_silence


def _have_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


pytestmark = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg/ffprobe required")


def _write_wav(path: Path, *, sample_rate: int, channels: int, duration_s: float = 0.1) -> None:
    """Write a minimal RIFF/WAVE PCM s16le file. Important: the channel
    layout is NOT set in the header — this triggers the
    ``channel_layout=unknown`` ffprobe output that broke the cloud
    render. (Real ffmpeg files include layout via the WAVEFORMATEXT
    extension; bare PCM-WAV files don't.)
    """
    n_samples = int(sample_rate * duration_s)
    sample_bytes = b"\x00\x00" * channels * n_samples
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    fmt_chunk = struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, 16)
    data_chunk = sample_bytes
    riff_size = 4 + 8 + len(fmt_chunk) + 8 + len(data_chunk)
    payload = (
        b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
        + b"data" + struct.pack("<I", len(data_chunk)) + data_chunk
    )
    path.write_bytes(payload)


def test_probe_wav_params_handles_unknown_layout(tmp_path: Path):
    """The exact shape that broke job 2cb1a441…: a 24kHz mono WAV with
    no channel_layout in the header → ffprobe says
    channel_layout=unknown. Parser must derive ``mono`` from the
    channels=1 line, NOT propagate ``unknown``.
    """
    wav = tmp_path / "chatterbox_24k_mono.wav"
    _write_wav(wav, sample_rate=24000, channels=1)
    rate, layout = _probe_wav_params(wav)
    assert rate == 24000
    assert layout == "mono", (
        f"unknown-layout WAV must derive layout from channels=1, got {layout!r}"
    )


def test_probe_wav_params_handles_unknown_layout_stereo(tmp_path: Path):
    wav = tmp_path / "chatterbox_44k_stereo.wav"
    _write_wav(wav, sample_rate=44100, channels=2)
    rate, layout = _probe_wav_params(wav)
    assert rate == 44100
    assert layout == "stereo"


def test_probe_wav_params_handles_22050_mono(tmp_path: Path):
    """Higgs Audio v2 emits 22050Hz mono — pin it works."""
    wav = tmp_path / "higgs.wav"
    _write_wav(wav, sample_rate=22050, channels=1)
    rate, layout = _probe_wav_params(wav)
    assert rate == 22050
    assert layout == "mono"


def test_probe_wav_params_returns_safe_defaults_on_corrupt_file(tmp_path: Path):
    """If ffprobe fails (corrupt file), parser must return safe
    defaults (44100 Hz mono) — never let downstream
    ``anullsrc=cl=unknown`` crash the render.
    """
    bad = tmp_path / "not_a_wav.wav"
    bad.write_bytes(b"this is not a WAV file at all")
    rate, layout = _probe_wav_params(bad)
    assert rate == 44100
    assert layout == "mono"


def test_wav_concat_with_silence_succeeds_on_unknown_layout(tmp_path: Path):
    """End-to-end: concat two unknown-layout WAVs with a silence
    joiner. Pre-fix this raised RuntimeError on the silence ffmpeg.
    """
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    out = tmp_path / "concat.wav"
    _write_wav(a, sample_rate=24000, channels=1, duration_s=0.5)
    _write_wav(b, sample_rate=24000, channels=1, duration_s=0.5)
    _wav_concat_with_silence([a, b], silence_s=0.4, out_wav=out)
    assert out.exists()
    # Concat output should be ~1.4s (0.5 + 0.4 silence + 0.5).
    rate, layout = _probe_wav_params(out)
    assert rate == 24000
    assert layout == "mono"
