"""Indic Parler-TTS provider — AI4Bharat / HF-audio multilingual (Apache 2.0).

**STATUS 2026-05-04: BROKEN IN THE DEFAULT VENV.** parler-tts is
unmaintained against transformers ≥ 4.49 (which diffusers 0.38
requires for image-gen); its Config API uses the removed
PreTrainedConfig attribute. Wiring kept here so anyone with a separate
venv can use it; first-use error makes the situation clear.

DIFFERENT INTERFACE from the cloning providers: Parler-TTS is
description-conditioned, not voice-cloned. Channel ``tts_voice`` is
interpreted as a NATURAL-LANGUAGE DESCRIPTION of the target voice —
e.g. "A female speaker delivers a slightly expressive and animated
speech with a moderate speed and pitch." The library has named voices
(Rohit, Divya, Sneha, etc.) — name them in the description and the
model conditions on that identity.

Reference: https://huggingface.co/ai4bharat/indic-parler-tts (gated;
accept the license once before first run).

Free, commercial-safe, and the only credible open-source path for
Hindi narration on hindutavaanimated.
"""
from __future__ import annotations

from pathlib import Path


_INDIC_PARLER_MODEL = None
_INDIC_PARLER_TOKENIZER = None
_INDIC_PARLER_DESC_TOKENIZER = None


def _indic_parler_model():
    global _INDIC_PARLER_MODEL, _INDIC_PARLER_TOKENIZER, _INDIC_PARLER_DESC_TOKENIZER
    if _INDIC_PARLER_MODEL is None:
        try:
            from parler_tts import ParlerTTSForConditionalGeneration  # type: ignore
            from transformers import AutoTokenizer  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "TTS provider 'indic_parler' requires the parler-tts package.\n"
                "  install: .venv/bin/pip install git+https://github.com/huggingface/parler-tts.git\n"
                f"  underlying error: {e}"
            ) from e
        import torch  # type: ignore

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        repo = "ai4bharat/indic-parler-tts"
        try:
            _INDIC_PARLER_MODEL = ParlerTTSForConditionalGeneration.from_pretrained(
                repo
            ).to(device)
        except OSError as e:
            if "gated" in str(e).lower() or "403" in str(e):
                raise RuntimeError(
                    "TTS provider 'indic_parler' needs HuggingFace gated-repo access.\n"
                    f"  1. Visit https://huggingface.co/{repo}\n"
                    "  2. Click 'Agree and access repository' (one-time, free)\n"
                    "  3. Ensure your HF token is logged in: huggingface-cli login\n"
                    f"  underlying error: {e}"
                ) from e
            raise
        _INDIC_PARLER_TOKENIZER = AutoTokenizer.from_pretrained(repo)
        # Parler uses a SEPARATE tokenizer for the description prompt
        # (the description encoder is a t5-class model, the prompt
        # encoder is the model's own tokenizer).
        _INDIC_PARLER_DESC_TOKENIZER = AutoTokenizer.from_pretrained(
            _INDIC_PARLER_MODEL.config.text_encoder._name_or_path
        )
    return _INDIC_PARLER_MODEL, _INDIC_PARLER_TOKENIZER, _INDIC_PARLER_DESC_TOKENIZER


_INDIC_PARLER_DEFAULT_DESCRIPTION = (
    "Sneha speaks in a calm, gentle, expressive Hindi storytelling tone "
    "with a moderate speed and warm pitch. The recording is of very high "
    "quality, with the speaker's voice sounding clear and very close up, "
    "no background noise."
)


def _synth_indic_parler(
    text: str,
    description: str,
    out_path: Path,
    speed: float,
) -> Path:
    """Hindi (and other Indic-language) synthesis via Indic Parler-TTS.

    Requires::

        .venv/bin/pip install git+https://github.com/huggingface/parler-tts.git

    `description` is a natural-language description of the desired voice
    (speaker name, emotion, pace, recording quality). The channel YAML
    surfaces this via `tts_voice` — e.g. set `tts_voice: "Sneha speaks
    calmly..."` to name the speaker. Empty/None falls back to the
    default Sneha description above.
    """
    import soundfile as _sf
    import torch  # type: ignore

    model, tok, desc_tok = _indic_parler_model()
    desc_text = description or _INDIC_PARLER_DEFAULT_DESCRIPTION

    device = next(model.parameters()).device
    desc_inputs = desc_tok(desc_text, return_tensors="pt").to(device)
    prompt_inputs = tok(text, return_tensors="pt").to(device)

    with torch.no_grad():
        gen = model.generate(
            input_ids=desc_inputs.input_ids,
            attention_mask=desc_inputs.attention_mask,
            prompt_input_ids=prompt_inputs.input_ids,
            prompt_attention_mask=prompt_inputs.attention_mask,
        )
    audio = gen.cpu().numpy().squeeze()
    sr = model.config.sampling_rate

    out_path.parent.mkdir(parents=True, exist_ok=True)
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
