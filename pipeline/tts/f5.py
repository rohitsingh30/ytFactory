"""F5-TTS-MLX provider — zero-shot voice cloning on Apple Silicon (MIT).

~1.35 GB checkpoint, MLX-native. Channel ``tts_voice`` is the path to a
5-15s reference WAV; ``tts_ref_text`` is its transcript and is required.

Module state preserved verbatim from the original ``pipeline/audio.py``
so callers that mutated it directly continue to work — but please use
:func:`reset_state` (re-exported by :mod:`pipeline.audio` as
``reset_f5_state``) instead of ``audio._F5_MODEL = None`` going forward.

IMPORTANT: do NOT call f5_tts_mlx.generate.generate() per-chunk. Upstream's
generate() instantiates F5TTS.from_pretrained inside the function body
(cfm.py line 131), so every call reloads the 1.35 GB checkpoint and
re-triggers mx.compile of the ODE step fn. On long-form (178 chunks of a
50k-char narration) that turned a ~30s/chunk job into a ~3-5 min/chunk job,
pushing total render time from ~1.5 hr to ~9 hr.

We side-step it: load F5TTS once into a module-level singleton, cache the
loaded ref audio per (path, text) pair, and call f5tts.sample() directly,
replicating the body of upstream generate() (lines 144-195) minus the load.
"""
from __future__ import annotations

from pathlib import Path


_F5_MODEL = None  # F5TTS singleton — survives across synth calls
_F5_REF_CACHE: dict[str, tuple] = {}  # (audio mx.array, ref_audio_duration) keyed by ref_audio_path


def reset_state() -> None:
    """Drop the F5-TTS singleton + clear the ref-audio cache.

    Call this when you're done with TTS and need MLX heap headroom for
    the next stage (e.g. image gen via z_image_turbo). The renderer in
    ``historyrecapped/scripts/render_long_form.py`` invokes this between
    the TTS pass and the image-panel pass — without it, MLX runs out of
    contiguous memory part-way through panel generation.

    Replaces direct mutation of ``audio._F5_MODEL = None`` /
    ``audio._F5_REF_CACHE.clear()`` (still works via the
    ``pipeline.audio`` compat facade, but this API is the supported
    one going forward).
    """
    global _F5_MODEL
    _F5_MODEL = None
    _F5_REF_CACHE.clear()


def _f5_get_model(quantization_bits: int | None = None):
    """Lazy-load + cache the F5TTS model singleton."""
    global _F5_MODEL
    if _F5_MODEL is None:
        try:
            from f5_tts_mlx.cfm import F5TTS  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "TTS provider 'f5_tts' requires the f5-tts-mlx package.\n"
                "  install: .venv/bin/pip install f5-tts-mlx\n"
                f"  underlying error: {e}"
            ) from e
        _F5_MODEL = F5TTS.from_pretrained(
            "lucasnewman/f5-tts-mlx", quantization_bits=quantization_bits
        )
    return _F5_MODEL


def _f5_get_ref(ref_audio_path: str):
    """Load ref audio once per path, RMS-normalize, cache as mx.array."""
    if ref_audio_path in _F5_REF_CACHE:
        return _F5_REF_CACHE[ref_audio_path]
    import mlx.core as mx  # type: ignore
    import soundfile as sf  # type: ignore
    audio_np, sr = sf.read(ref_audio_path)
    if sr != 24_000:
        raise RuntimeError(
            f"f5_tts ref audio must be 24 kHz mono; got sr={sr} for {ref_audio_path}"
        )
    audio = mx.array(audio_np)
    rms = mx.sqrt(mx.mean(mx.square(audio)))
    target_rms = 0.1
    if rms < target_rms:
        audio = audio * target_rms / rms
    duration_s = audio.shape[0] / 24_000
    _F5_REF_CACHE[ref_audio_path] = (audio, duration_s)
    return _F5_REF_CACHE[ref_audio_path]


def _synth_f5_tts(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str,
    out_path: Path,
    speed: float,
    steps: int = 8,
    method: str = "rk4",
    quantization_bits: int | None = None,
) -> Path:
    """Zero-shot voice cloning via F5-TTS-MLX.

    Requires::

        .venv/bin/pip install f5-tts-mlx

    First call fetches the F5-TTS-MLX checkpoint (~1.35 GB) into the HF cache
    AND loads it into memory; subsequent calls reuse the cached model and
    cached ref audio (keyed by ``ref_audio_path``), so per-chunk cost drops
    from ~3-5 min (full reload + recompile) to ~25-35 s (steady-state sample
    only) on M2 Max with the default rk4/steps=8 settings.

    Replicates upstream f5_tts_mlx.generate.generate() body (cfm.py /
    generate.py lines 144-195) without the per-call ``F5TTS.from_pretrained``.
    """
    import datetime
    import re as _re

    import mlx.core as mx  # type: ignore
    import numpy as np  # type: ignore
    import soundfile as sf  # type: ignore
    from f5_tts_mlx.utils import convert_char_to_pinyin  # type: ignore

    out_path.parent.mkdir(parents=True, exist_ok=True)

    f5 = _f5_get_model(quantization_bits=quantization_bits)
    audio, _ = _f5_get_ref(ref_audio_path)

    # estimate_duration replicated from generate.py:104 — the library's
    # generate() calls this when estimate_duration=True. Duration prediction
    # via the model's predictor is the upstream default but the heuristic is
    # what generate() uses, so we mirror it for consistent output length.
    SAMPLE_RATE = 24_000
    HOP_LENGTH = 256
    FRAMES_PER_SEC = SAMPLE_RATE / HOP_LENGTH
    ref_audio_len = audio.shape[0] // HOP_LENGTH
    zh_pause_punc = r"。，、；：？！"
    ref_text_len = len(ref_audio_text.encode("utf-8")) + 3 * len(_re.findall(zh_pause_punc, ref_audio_text))
    gen_text_len = len(text.encode("utf-8")) + 3 * len(_re.findall(zh_pause_punc, text))
    duration_in_frames = ref_audio_len + int(ref_audio_len / ref_text_len * gen_text_len / speed)

    prepped = convert_char_to_pinyin([ref_audio_text + " " + text])
    wave, _ = f5.sample(
        mx.expand_dims(audio, axis=0),
        text=prepped,
        duration=duration_in_frames,
        steps=steps,
        method=method,
        speed=speed,
        cfg_strength=2.0,
        sway_sampling_coef=-1.0,
        seed=None,
    )
    wave = wave[audio.shape[0]:]
    mx.eval(wave)

    sf.write(str(out_path), np.array(wave), SAMPLE_RATE)
    return out_path
