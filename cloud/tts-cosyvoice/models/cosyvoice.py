"""CosyVoice 2 — Cloud Run GPU synth wrapper (this image's only model).

Mirrors the wrapper at cloud/tts/models/cosyvoice.py (kept intact in
the main service for v4 reactivation), but is the live code path here.

Zero-shot voice cloning via a 3-10s reference WAV + its transcript.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Container path — Dockerfile clones CosyVoice here and we extend
# sys.path so the package + its Matcha-TTS submodule are importable.
_COSYVOICE_REPO = "/opt/CosyVoice"
if _COSYVOICE_REPO not in sys.path and os.path.isdir(_COSYVOICE_REPO):
    sys.path.insert(0, _COSYVOICE_REPO)
    sys.path.insert(0, f"{_COSYVOICE_REPO}/third_party/Matcha-TTS")

_COSYVOICE_MODEL = None
_MODEL_DIR = "/models/cosyvoice/CosyVoice2-0.5B"


def reset_state() -> None:
    global _COSYVOICE_MODEL
    _COSYVOICE_MODEL = None


def _cosyvoice_model():
    """Lazy-load CosyVoice2 (~3GB VRAM, ~5-8s first hit on L4)."""
    global _COSYVOICE_MODEL
    if _COSYVOICE_MODEL is None:
        from cosyvoice.cli.cosyvoice import CosyVoice2

        logger.info("loading CosyVoice2-0.5B from %s onto cuda…", _MODEL_DIR)
        _COSYVOICE_MODEL = CosyVoice2(
            _MODEL_DIR,
            load_jit=False,
            load_trt=False,
            fp16=True,
        )
        logger.info("CosyVoice2 loaded.")
    return _COSYVOICE_MODEL


def synth_cosyvoice(
    *,
    text: str,
    ref_audio_path: str,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float = 1.0,
    seed: int | None = None,
) -> Path:
    """Zero-shot voice cloning + TTS via CosyVoice 2.

    `ref_audio_text` is the transcript of the reference WAV. Required
    for zero-shot mode (cosyvoice's ``inference_zero_shot`` API).
    """
    import torch
    import torchaudio

    if not ref_audio_text:
        raise ValueError(
            "cosyvoice requires ref_audio_text (transcript of the ref WAV)"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    model = _cosyvoice_model()

    # CosyVoice expects a 16kHz mono prompt; resample if the caller
    # uploaded a different sample rate (Sarah ref is 24 kHz).
    # CosyVoice expects a 16kHz mono prompt as a FILE PATH (its
    # frontend.frontend_zero_shot internally re-loads via
    # torchaudio.load(path)). Passing a tensor directly trips
    # `TypeError: Invalid file: tensor(...)` deep in soundfile_backend.
    # Resample to 16 kHz on disk and hand it the path.
    prompt_speech, prompt_sr = torchaudio.load(ref_audio_path)
    if prompt_sr != 16000:
        prompt_speech = torchaudio.functional.resample(
            prompt_speech, prompt_sr, 16000,
        )
    cosy_prompt_path = Path(ref_audio_path).with_suffix(".cosy16k.wav")
    torchaudio.save(str(cosy_prompt_path), prompt_speech, 16000)

    t0 = time.time()
    chunks = []
    for chunk in model.inference_zero_shot(
        text, ref_audio_text, str(cosy_prompt_path), speed=speed, stream=False,
    ):
        chunks.append(chunk["tts_speech"])
    audio = torch.cat(chunks, dim=-1)
    sr = model.sample_rate

    torchaudio.save(str(out_path), audio, sr)
    duration_s = audio.shape[-1] / sr
    logger.info(
        "cosyvoice synth done: text_chars=%d, audio_s=%.2f, sr=%d, wall=%.1fs",
        len(text), duration_s, sr, time.time() - t0,
    )
    return out_path
