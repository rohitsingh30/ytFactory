"""ytFactory TTS-CosyVoice — model registry + singleton.

Single-model service (only ``cosyvoice``). The router shape mirrors
the multi-model ``cloud/tts/handler.py`` so the laptop-side dispatcher
in ``pipeline/tts/cloudrun.py`` can call this service the same way it
calls the main one — only the URL differs.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

SUPPORTED_MODELS: tuple[str, ...] = (
    "cosyvoice",   # multilingual incl Hindi, voice clone (FunAudioLLM)
)


def synthesize(
    *,
    model: str,
    text: str,
    ref_audio_path: str,
    ref_audio_text: str | None,
    out_path: Path,
    speed: float = 1.0,
    seed: int | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Route a synth request to the cosyvoice singleton."""
    if model != "cosyvoice":
        raise ValueError(
            f"unknown model={model!r}; this service supports {SUPPORTED_MODELS!r}. "
            f"Other models live on the main ytfactory-tts service."
        )

    from models.cosyvoice import synth_cosyvoice

    return synth_cosyvoice(
        text=text,
        ref_audio_path=ref_audio_path,
        ref_audio_text=ref_audio_text,
        out_path=out_path,
        speed=speed,
        seed=seed,
    )
