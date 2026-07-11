"""StyleTTS2 provider — best open-source long-form prosody (MIT).

**STATUS 2026-05-04: BROKEN IN THE DEFAULT VENV.** StyleTTS2 0.1.6
hard-pins ``huggingface_hub<0.20`` / ``librosa<0.11`` / ``networkx<3``
which conflict with everything else in this repo. Wiring kept here so
anyone with a separate venv (or a future StyleTTS2 release) can swap
this provider in via channel YAML; first-use error makes the situation
clear.

Used here as the upgrade path for the historyrecapped long-form sleep
videos. The default Kokoro+atempo pipeline at the top of
``pipeline.audio`` is fully sufficient for most narrations; switch to
StyleTTS2 only when prosody quality is the bottleneck (e.g. a 60-min
sleep video where micro-pauses and rhythm carry the listener through).

Channel `tts_voice` is the path to a 5-15s reference WAV.

Why not the default for short channels: StyleTTS2's PyTorch path is
slower than Kokoro on M2 Max, and the prosody lift is most audible past
~30s of continuous narration. For Shorts the cost isn't worth it;
Kokoro is the pick.
"""
from __future__ import annotations

from pathlib import Path


_STYLETTS2_MODEL = None


def _styletts2_model():
    global _STYLETTS2_MODEL
    if _STYLETTS2_MODEL is None:
        try:
            from styletts2 import tts as _styletts2_tts  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "TTS provider 'styletts2' requires the styletts2 package.\n"
                "  install: .venv/bin/pip install styletts2\n"
                f"  underlying error: {e}"
            ) from e
        _STYLETTS2_MODEL = _styletts2_tts.StyleTTS2()
    return _STYLETTS2_MODEL


def _synth_styletts2(
    text: str,
    ref_audio_path: str,
    out_path: Path,
    speed: float,
    alpha: float = 0.3,
    beta: float = 0.7,
    diffusion_steps: int = 7,
    embedding_scale: float = 1.0,
) -> Path:
    """Long-form-tuned synthesis via StyleTTS2.

    Requires::

        .venv/bin/pip install styletts2

    `alpha`/`beta` blend timbre vs prosody from the reference clip; the
    library's recommended defaults (0.3/0.7) preserve speaker identity
    while leaning on the model's own prosody style — exactly the right
    balance for sleep narration where consistency matters more than
    mimicking every micro-inflection of the ref.
    """
    import soundfile as _sf

    model = _styletts2_model()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    audio = model.inference(
        text,
        target_voice_path=ref_audio_path,
        alpha=alpha,
        beta=beta,
        diffusion_steps=diffusion_steps,
        embedding_scale=embedding_scale,
    )
    # StyleTTS2 returns a numpy array at 24kHz.
    sr = 24000
    _sf.write(out_path, audio, sr)

    if abs(speed - 1.0) > 0.01:
        raw_path = out_path.with_suffix(".raw.wav")
        out_path.rename(raw_path)
        import subprocess as _sp

        cmd = [
            "ffmpeg", "-y", "-i", str(raw_path),
            "-filter:a", f"atempo={speed:.4f}",
            "-ar", str(sr), "-ac", "1",
            str(out_path),
        ]
        _sp.run(cmd, check=True, capture_output=True)
        raw_path.unlink(missing_ok=True)

    return out_path
