"""ytFactory Cloud Run TTS — model registry + per-model singletons.

Phase 1 (this file): only F5-TTS is registered.
Phase 3 will add: HiggsAudioV2, CosyVoice2, Chatterbox — same pattern,
each in its own ``models/<name>.py`` module, lazy-imported on first
synth call so cold-start only pays for the model actually requested.

The router shape mirrors ``pipeline/audio.py::synthesize`` so callers
on the laptop side (``pipeline/tts/cloudrun.py``) can swap "f5_tts" for
"cloudrun_f5" by changing the provider string only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Registered model names. Each maps to the synth function imported lazily
# inside ``synthesize`` so e.g. boot-time of an F5-only pod doesn't pay
# the chatterbox/torch-extras import cost.
SUPPORTED_MODELS: tuple[str, ...] = ("f5",)


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
    """Route a synth request to the right model singleton.

    `out_path` is where the WAV is written on local disk inside the
    container (e.g. ``/tmp/<id>.wav``). The HTTP layer in ``server.py``
    decides whether to return it inline (≤5 MB) or upload to GCS.
    """
    if model == "f5":
        from models.f5 import synth_f5  # lazy import; non-relative because
                                        # server.py launches us as flat
                                        # modules from /app, not as a package

        if not ref_audio_text:
            raise ValueError(
                "model='f5' requires ref_audio_text (the transcript of the "
                "reference WAV). Same contract as the local f5_tts provider."
            )
        return synth_f5(
            text=text,
            ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text,
            out_path=out_path,
            speed=speed,
            seed=seed,
        )

    # Phase 3 stubs — fail clearly until shipped, never silently fall
    # through to a default. Easier to debug than a NameError.
    if model in ("higgs", "cosyvoice", "chatterbox"):
        raise NotImplementedError(
            f"model={model!r} is registered for Phase 3 but not yet bundled "
            f"in this image. Rebuild with `cloud/tts/models/{model}.py`."
        )

    raise ValueError(
        f"unknown model={model!r}; supported: {SUPPORTED_MODELS!r}"
    )
