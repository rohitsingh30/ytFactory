"""F5-only handler — single model, no eviction logic."""
from __future__ import annotations
from pathlib import Path

SUPPORTED_MODELS = ("f5",)


def synthesize(*, model, text, ref_audio_path, ref_audio_text,
               out_path: Path, speed=1.0, seed=None, extra=None):
    if model != "f5":
        raise ValueError(f"this service only supports 'f5'; got {model!r}")
    if not ref_audio_text:
        raise ValueError("model='f5' requires ref_audio_text")
    from models.f5 import synth_f5
    return synth_f5(
        text=text, ref_audio_path=ref_audio_path,
        ref_audio_text=ref_audio_text, out_path=out_path,
        speed=speed, seed=seed,
    )
