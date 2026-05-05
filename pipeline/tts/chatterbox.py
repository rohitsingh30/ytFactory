"""Chatterbox provider — Resemble AI emotional voice cloning (MIT).

Channel ``tts_voice`` is the path to a 5-15s reference WAV (same convention
as f5_tts). ``ref_audio_text`` is NOT required by Chatterbox.

~3 GB checkpoint download on first use; MPS-accelerated on Apple Silicon.
Beat ElevenLabs in 2026 blind A/B (63.75% preference) and has built-in
emotion-exaggeration control — the right fit for AITA-style emotional
storytelling on mystoriesanimated.

Output watermark: Chatterbox embeds Resemble's perceptually-inaudible
Perth watermark for AI-detection traceability. It does not affect
listening quality or YouTube monetization.
"""
from __future__ import annotations

from pathlib import Path


_CHATTERBOX_MODEL = None  # lazy global, lives across synth calls


def _chatterbox_model():
    """Lazy-load + cache the Chatterbox model singleton.

    First call: ~3 GB checkpoint download into the HF cache. Subsequent
    calls reuse the in-memory model — costs ~6 GB of RAM, fine on M2 Max.
    """
    global _CHATTERBOX_MODEL
    if _CHATTERBOX_MODEL is None:
        try:
            from chatterbox.tts import ChatterboxTTS  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "TTS provider 'chatterbox' requires the chatterbox-tts package.\n"
                "  install: .venv/bin/pip install chatterbox-tts\n"
                f"  underlying error: {e}"
            ) from e
        # Apple Silicon → MPS; falls back to CPU on other hardware.
        # The lib's from_pretrained handles device routing internally.
        import torch  # type: ignore

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _CHATTERBOX_MODEL = ChatterboxTTS.from_pretrained(device=device)
    return _CHATTERBOX_MODEL


def _synth_chatterbox(
    text: str,
    ref_audio_path: str,
    out_path: Path,
    speed: float,
    exaggeration: float = 0.5,
    cfg_weight: float = 0.5,
) -> Path:
    """Zero-shot voice cloning via Chatterbox.

    Requires::

        .venv/bin/pip install chatterbox-tts

    `ref_audio_path` should be a 5-15s WAV of the target voice. `speed`
    is mapped to the post-synth atempo factor — Chatterbox's generator
    runs at native rate; we ffmpeg-stretch the output to match the
    channel's `tts_speed`. `exaggeration` (0.0-1.0) controls emotion
    intensity — 0.5 is the library default and the right neutral for
    AITA conversational; bump toward 0.7 for high-drama stories.
    """
    import numpy as _np
    import soundfile as _sf

    model = _chatterbox_model()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wav = model.generate(
        text,
        audio_prompt_path=ref_audio_path,
        exaggeration=exaggeration,
        cfg_weight=cfg_weight,
    )
    # Chatterbox returns a torch.Tensor at model.sr (24kHz typically).
    sr = int(model.sr)
    audio_np = wav.detach().cpu().numpy().squeeze()

    # Apply speed via post-synth atempo if requested. atempo accepts
    # 0.5-2.0 in a single pass; outside that range it'd need chaining,
    # but channel tts_speed is always within 0.85-1.2 in practice.
    if abs(speed - 1.0) > 0.01:
        raw_path = out_path.with_suffix(".raw.wav")
        _sf.write(raw_path, audio_np, sr)
        import subprocess as _sp

        cmd = [
            "ffmpeg", "-y", "-i", str(raw_path),
            "-filter:a", f"atempo={speed:.4f}",
            "-ar", str(sr), "-ac", "1",
            str(out_path),
        ]
        _sp.run(cmd, check=True, capture_output=True)
        raw_path.unlink(missing_ok=True)
    else:
        _sf.write(out_path, audio_np, sr)

    return out_path
