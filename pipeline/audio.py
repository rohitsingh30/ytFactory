"""Stage 4 — TTS dispatcher + backward-compatibility facade.

This module is a **compatibility facade** for what used to be a single
1,873-line file. The actual provider implementations now live under
:mod:`pipeline.tts`:

* :mod:`pipeline.tts.kokoro`        — Kokoro 82M (Apache 2.0)
* :mod:`pipeline.tts.f5`            — F5-TTS-MLX (MIT, voice clone)
* :mod:`pipeline.tts.chatterbox`    — Chatterbox (MIT, emotional clone)
* :mod:`pipeline.tts.styletts2`     — StyleTTS2 (MIT, long-form prosody)
* :mod:`pipeline.tts.parler`        — AI4Bharat Indic Parler-TTS
* :mod:`pipeline.tts.song`          — Suno API + song trim helpers
* :mod:`pipeline.tts.text_normalize` — currency/year/acronym/Hindi normalisation

The split was driven by audio.py becoming unmaintainable. This file
re-exports every public + every documented-private symbol so existing
callers continue to work unchanged:

* :mod:`scripts.historyrecapped.render_long_form`
* :mod:`scripts.historyrecapped.render_footage_only`
* :mod:`scripts.historyrecapped.kokoro_voice_samples`
* :mod:`historyrecapped.scripts.regen_audio_caps`
* :mod:`web.server`
* :mod:`tests.test_audio_tts_providers`

In particular:

* ``audio._synth_kokoro``, ``_synth_f5_tts``, ``_synth_chatterbox``,
  ``_synth_styletts2``, ``_synth_indic_parler`` — re-exported into this
  module's globals so :func:`unittest.mock.patch.object(audio, ...)`
  in :mod:`tests.test_audio_tts_providers` continues to intercept the
  real dispatch path. The ``synthesize`` function below dispatches
  via bare-name lookup (which resolves through this module's
  ``__dict__``) so a patched binding is honoured.
* ``audio._F5_MODEL`` / ``audio._F5_REF_CACHE`` — both re-exported, but
  **direct mutation is deprecated**. Use :func:`reset_f5_state`
  instead. The historyrecapped long-form renderer was migrated to the
  new API in the same commit that created this facade. Direct mutation
  through this module would only update the local re-export, NOT the
  underlying singletons in :mod:`pipeline.tts.f5` — which is why the
  old pattern needs the explicit reset API.
* ``audio._kokoro`` — the Kokoro model accessor (used by
  ``historyrecapped/scripts/kokoro_voice_samples.py:18``).
"""
from __future__ import annotations

from pathlib import Path

# ---- text normalisation (no torch deps) -----------------------------------
# Public API:
from pipeline.tts.text_normalize import (
    apply_pronunciation_overrides,
    normalize_for_tts,
)
# Private helpers — re-exported for any callers that grep for them:
from pipeline.tts.text_normalize import (  # noqa: F401
    _ACRONYM_PHRASES,
    _AITA_ACRONYMS_CI,
    _DEVANAGARI_PROBE,
    _HINDI_NUMERAL_RESPELLINGS,
    _HINDI_TATSAMA_RESPELLINGS,
    _ONES,
    _ORDINAL_ONES,
    _ORDINAL_TENS,
    _RE_ALLCAPS_WORD,
    _RE_ANY_CASE_ACRONYM,
    _RE_CURRENCY,
    _RE_DAY_MONTH,
    _RE_INTEGER,
    _RE_MONTH_DAY,
    _RE_PROFANITY_ASSHOLE,
    _RE_VERDICT_ACRONYM,
    _TEENS,
    _TENS,
    _apply_hindi_respellings,
    _expand_any_case_acronym,
    _expand_bare_int,
    _expand_currency,
    _expand_day_month,
    _expand_month_day,
    _expand_year,
    _int_to_words,
    _lowercase_emphatic_caps,
    _ordinal_to_words,
    _strip_verdict_acronym_sentences,
    _sub_profanity_asshole,
    _two_digits_to_words,
)

# ---- Kokoro (Apache 2.0 — fast English/Hindi/multilingual) ----------------
# Public API:
from pipeline.tts.kokoro import lang_for_voice  # noqa: F401
# Private helpers — re-exported (kokoro_voice_samples.py imports _kokoro;
# tests patch _synth_kokoro):
from pipeline.tts.kokoro import (  # noqa: F401
    _DEFAULT_MODULATION,
    _KOKORO,
    _MAX_SILENCE_S,
    _PARAGRAPH_PAUSE_S,
    _PARAGRAPH_SPLIT_RE,
    _SENTENCE_PAUSE_S,
    _SENTENCE_SPLIT_RE,
    _VOICE_PREFIX_LANG,
    _download_if_missing,
    _kokoro,
    _modulate_sentence_speed,
    _split_into_sentences,
    _synth_kokoro,
    _trim_trailing_silence,
    _word_count,
)

# ---- F5-TTS-MLX (MIT — voice clone, Apple Silicon) ------------------------
# Re-export so monkeypatch tests work AND legacy mutation patterns
# (deprecated) still find the names. See the module docstring above for
# the migration guidance.
from pipeline.tts import f5 as _f5_mod  # noqa: F401
from pipeline.tts.f5 import (  # noqa: F401
    _f5_get_model,
    _f5_get_ref,
    _synth_f5_tts,
)
# Backward-compatible alias — `audio._synth_f` was used in some older
# external scripts as a short name for `_synth_f5_tts`.
_synth_f = _synth_f5_tts


def reset_f5_state() -> None:
    """Drop the F5-TTS singleton + clear the ref-audio cache.

    Replaces direct mutation of ``audio._F5_MODEL = None`` /
    ``audio._F5_REF_CACHE.clear()`` (which now only affect the local
    re-export, not the underlying singletons in :mod:`pipeline.tts.f5`).

    Long-form renderers call this between the TTS pass and the image-gen
    pass so MLX has contiguous heap headroom for z_image_turbo. See
    ``historyrecapped/scripts/render_long_form.py``.
    """
    _f5_mod.reset_state()


def __getattr__(name: str):
    """Lazy proxy for the F5 module-state globals.

    ``_F5_MODEL`` and ``_F5_REF_CACHE`` are looked up on the *underlying*
    :mod:`pipeline.tts.f5` module so a fresh value (e.g. after the
    singleton was loaded by a synth call) is observed by any reader that
    does ``audio._F5_MODEL`` / ``audio._F5_REF_CACHE`` — not the stale
    None / empty-dict snapshot taken at facade-import time.

    Direct *writes* (``audio._F5_MODEL = None``) still only set the
    facade's local attribute and do NOT propagate to the underlying
    module — use :func:`reset_f5_state` instead. We keep the read-side
    proxy because there's no harm in serving fresh values to callers
    that just want to inspect the singleton.
    """
    if name == "_F5_MODEL":
        return _f5_mod._F5_MODEL
    if name == "_F5_REF_CACHE":
        return _f5_mod._F5_REF_CACHE
    raise AttributeError(f"module 'pipeline.audio' has no attribute {name!r}")


# ---- Chatterbox (MIT — Resemble AI emotional voice clone) -----------------
from pipeline.tts.chatterbox import (  # noqa: E402, F401
    _CHATTERBOX_MODEL,
    _chatterbox_model,
    _synth_chatterbox,
)

# ---- StyleTTS2 (MIT — long-form prosody; broken in default venv) ----------
from pipeline.tts.styletts2 import (  # noqa: E402, F401
    _STYLETTS2_MODEL,
    _styletts2_model,
    _synth_styletts2,
)

# ---- Indic Parler-TTS (Apache 2.0; broken in default venv) ----------------
from pipeline.tts.parler import (  # noqa: E402, F401
    _INDIC_PARLER_DEFAULT_DESCRIPTION,
    _INDIC_PARLER_DESC_TOKENIZER,
    _INDIC_PARLER_MODEL,
    _INDIC_PARLER_TOKENIZER,
    _indic_parler_model,
    _synth_indic_parler,
)

# ---- Sung music (Suno wrapper + trim helpers) -----------------------------
from pipeline.tts.song import (  # noqa: E402, F401
    _SUNOAPI_BASE,
    _SUNOAPI_GENERATE,
    _SUNOAPI_RECORD,
    _detect_leading_silence_s,
    synth_via_sunoapi,
    trim_song_for_short,
)

# ---- Cloud Run GPU providers (asia-southeast1) ----------------------------
# Re-exported for the same reason as the local providers above —
# tests/test_audio_tts_providers.py uses patch.object(audio, '_synth_cloudrun_f5')
# so the dispatcher must look up the name via this module's __dict__.
from pipeline.tts.cloudrun import (  # noqa: E402, F401
    CloudRunUnavailable,
    _synth_cloudrun_chatterbox,
    _synth_cloudrun_cosyvoice,
    _synth_cloudrun_f5,
    _synth_cloudrun_higgs,
    _synth_cloudrun_indicf5,
    _synth_cloudrun_indicparler,
)


# ---- Public dispatcher ----------------------------------------------------
#
# This is the single entry point every channel calls. Dispatches by
# `provider` to the matching ``_synth_*`` function in this module's
# globals. Names like `_synth_kokoro` are looked up via the module
# `__dict__` at call time, so unittest.mock.patch.object(audio, ...) in
# tests/test_audio_tts_providers.py correctly intercepts.

def synthesize(
    text: str,
    voice: str,
    out_path: Path,
    speed: float = 1.0,
    provider: str = "kokoro",
    ref_audio_text: str | None = None,
    modulation: dict | None = None,
    pronunciation_dict: dict | None = None,
    language: str = "en",
) -> Path:
    """Generate speech audio. Returns path to the .wav file.

    For ``provider='kokoro'``, ``voice`` is a Kokoro voice id.
    For ``provider='f5_tts'``, ``voice`` is the path to a 5–15s
    reference clip, and ``ref_audio_text`` is required.
    For ``provider='chatterbox'`` / ``'styletts2'``, ``voice`` is a
    ref-WAV path; ``ref_audio_text`` is not required.
    For ``provider='indic_parler'``, ``voice`` is a natural-language
    description of the desired speaker (NOT a path).

    Numbers and currency in ``text`` are expanded to spoken words before
    synthesis (Kokoro reads "$2000" as digit-by-digit otherwise). The
    caller should pass the SAME normalised string to source-text alignment
    downstream so beat captions match what was actually spoken — see
    ``normalize_for_tts``.

    ``modulation`` (Kokoro path only): per-paragraph speed-factor knobs.
    See ``_DEFAULT_MODULATION`` for the keys; channel YAML's
    ``tts_modulation`` block overrides individual factors. Pass
    ``{"enabled": False}`` to disable modulation entirely. Without
    modulation each paragraph runs at the base ``speed``; with it, the
    hook reads slightly slower for clarity, the closer reads slower
    for impact, exclamation-heavy paragraphs read faster, and ellipsis-
    trailing paragraphs read slower for the hanging-beat effect.
    """
    text = normalize_for_tts(text)
    # Pronunciation overrides apply ONLY to the TTS-input string.
    # Captions and ASR-source-text alignment must continue to see the
    # original (unrespelled) text upstream — synthesize is the only
    # path that should ever consume the respelled form.
    text = apply_pronunciation_overrides(text, pronunciation_dict)
    if provider == "kokoro":
        return _synth_kokoro(
            text, voice=voice, out_path=out_path, speed=speed,
            modulation=modulation,
        )
    if provider == "f5_tts":
        if not ref_audio_text:
            raise ValueError(
                "f5_tts provider requires `ref_audio_text` (the transcript of "
                "the reference audio at `voice`)"
            )
        return _synth_f5_tts(
            text,
            ref_audio_path=voice,
            ref_audio_text=ref_audio_text,
            out_path=out_path,
            speed=speed,
        )
    if provider == "chatterbox":
        # `voice` = path to a 5-15s reference WAV. ref_audio_text not
        # required — Chatterbox conditions on audio embeddings only.
        return _synth_chatterbox(
            text,
            ref_audio_path=voice,
            out_path=out_path,
            speed=speed,
        )
    if provider == "styletts2":
        # `voice` = path to a 5-15s reference WAV.
        return _synth_styletts2(
            text,
            ref_audio_path=voice,
            out_path=out_path,
            speed=speed,
        )
    if provider == "indic_parler":
        # `voice` = natural-language description of the target speaker
        # (NOT a ref-WAV path). Empty → falls back to the default Sneha
        # description in `_synth_indic_parler`.
        return _synth_indic_parler(
            text,
            description=voice,
            out_path=out_path,
            speed=speed,
        )
    if provider == "cloudrun_f5":
        if not ref_audio_text:
            raise ValueError(
                "cloudrun_f5 provider requires `ref_audio_text` (the "
                "transcript of the reference audio at `voice`)"
            )
        return _synth_cloudrun_f5(
            text, ref_audio_path=voice, ref_audio_text=ref_audio_text,
            out_path=out_path, speed=speed,
        )
    if provider == "cloudrun_higgs":
        # Higgs Audio v2 — multilingual emotional voice clone.
        # ref_audio_text optional (Higgs handles either path).
        return _synth_cloudrun_higgs(
            text, ref_audio_path=voice, ref_audio_text=ref_audio_text,
            out_path=out_path, speed=speed,
        )
    if provider == "cloudrun_cosyvoice":
        # CosyVoice 2 — multilingual incl Hindi. ref_audio_text REQUIRED
        # (zero-shot mode needs the ref transcript).
        if not ref_audio_text:
            raise ValueError(
                "cloudrun_cosyvoice provider requires `ref_audio_text` "
                "(transcript of the reference audio at `voice`)"
            )
        return _synth_cloudrun_cosyvoice(
            text, ref_audio_path=voice, ref_audio_text=ref_audio_text,
            out_path=out_path, speed=speed,
        )
    if provider == "cloudrun_chatterbox":
        # Chatterbox via cloud — same contract as local (ref_audio_text
        # ignored). Useful for parity benchmarks.
        return _synth_cloudrun_chatterbox(
            text, ref_audio_path=voice, ref_audio_text=ref_audio_text,
            out_path=out_path, speed=speed,
        )
    if provider == "cloudrun_indicparler":
        # Indic Parler-TTS — Hindi/multi-lingual Indic, description-driven.
        # `voice` here can be ignored (description is the voice spec); we
        # pass it through to the cloud client which falls back to a sane
        # default if no description is supplied.
        return _synth_cloudrun_indicparler(
            text, ref_audio_path=voice or None, ref_audio_text=ref_audio_text,
            out_path=out_path, speed=speed,
        )
    if provider == "cloudrun_indicf5":
        # AI4Bharat IndicF5 — F5-TTS architecture trained on 1417h of
        # Indian speech, 11 Indic languages including Hindi. WAV-clone
        # style; ref_audio_text REQUIRED for prosody anchoring.
        if not ref_audio_text:
            raise ValueError(
                "cloudrun_indicf5 provider requires `ref_audio_text` "
                "(transcript of the reference audio at `voice`)"
            )
        return _synth_cloudrun_indicf5(
            text, ref_audio_path=voice, ref_audio_text=ref_audio_text,
            out_path=out_path, speed=speed,
        )
    raise ValueError(
        f"unknown TTS provider {provider!r} "
        "(choices: kokoro, f5_tts, chatterbox, styletts2, indic_parler, "
        "cloudrun_f5, cloudrun_higgs, cloudrun_cosyvoice, "
        "cloudrun_chatterbox, cloudrun_indicparler, cloudrun_indicf5)"
    )
