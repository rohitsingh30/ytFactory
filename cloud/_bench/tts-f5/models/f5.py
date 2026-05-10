"""F5-TTS PyTorch synth — runs on NVIDIA L4 (Cloud Run GPU).

Replicates the local ``pipeline/tts/f5.py`` (MLX, M2 Max) output
character by using the SAME checkpoint + SAME flow-matching params:

| Param              | Local MLX (pipeline/tts/f5.py) | Cloud (this file) |
|--------------------|-------------------------------|-------------------|
| Checkpoint         | lucasnewman/f5-tts-mlx        | SWivid F5TTS_Base |
|                    | (port of F5TTS_Base 1200000)  | (model_1200000)   |
| ODE method         | rk4                           | rk4               |
| Steps / nfe_step   | 8                             | 8                 |
| cfg_strength       | 2.0                           | 2.0               |
| sway_sampling_coef | -1.0                          | -1.0              |
| target_sample_rate | 24000                         | 24000 (model dflt)|

If you change ANY of these values, the cloud and local paths will
produce subtly different voices for the same script. Re-bench against
the local f5_tts output BEFORE shipping any param change.

Singleton model — F5TTS load is ~5-8 s on L4, paid once per cold-start.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_F5_MODEL = None  # F5TTS singleton


def _f5_get_model():
    """Lazy-load + cache the F5TTS PyTorch model on the GPU."""
    global _F5_MODEL
    if _F5_MODEL is None:
        from f5_tts.api import F5TTS  # type: ignore

        logger.info("loading F5-TTS (F5TTS_Base, ode_method=rk4) onto cuda…")
        # F5TTS_Base (NOT _v1_Base) matches lucasnewman/f5-tts-mlx, the
        # port our local pipeline uses. v1_Base is the March 2025
        # retrain — different voice character, would diverge from local.
        _F5_MODEL = F5TTS(
            model="F5TTS_Base",
            ode_method="rk4",  # match local MLX path; default is "euler"
            device="cuda",
        )
        logger.info("F5-TTS loaded.")
    return _F5_MODEL


def reset_state() -> None:
    """Drop singleton. Used by the /admin/reload endpoint when we hot-swap
    a new image without redeploying."""
    global _F5_MODEL
    _F5_MODEL = None


def synth_f5(
    *,
    text: str,
    ref_audio_path: str,
    ref_audio_text: str,
    out_path: Path,
    speed: float = 1.0,
    seed: int | None = None,
) -> Path:
    """Generate one WAV from text + reference voice.

    `seed=None` → upstream `infer()` auto-generates a fresh random seed
    via `random.randint(0, sys.maxsize)`. We do NOT pass `-1` here —
    that's a different lib's sentinel; this lib treats None as "random".
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    f5 = _f5_get_model()
    # The four params below MUST match pipeline/tts/f5.py for output
    # parity with the local MLX path. See docstring table at top of file.
    wav, sr, _ = f5.infer(
        ref_file=ref_audio_path,
        ref_text=ref_audio_text,
        gen_text=text,
        nfe_step=8,                  # local MLX uses steps=8
        cfg_strength=2.0,            # local MLX uses cfg_strength=2.0
        sway_sampling_coef=-1.0,     # local MLX uses sway_sampling_coef=-1.0
        speed=speed,
        seed=seed,
        remove_silence=False,        # caller controls trimming
        file_wave=str(out_path),     # F5TTS writes the WAV for us
    )
    duration_s = (len(wav) / sr) if sr else 0.0
    logger.info(
        "f5 synth done: text_chars=%d, audio_s=%.2f, sr=%d, out=%s",
        len(text), duration_s, sr, out_path,
    )
    return out_path
