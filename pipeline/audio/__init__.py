"""Stage 4 — TTS dispatcher + backward-compatibility facade.

This module **lives at** ``pipeline/audio/__init__.py`` (the package
form), not ``pipeline/audio.py`` (the flat form). Both must NOT
coexist in the repo — see ``docs/case_insensitive_package_shadowing.md``
for the APFS shadowing pitfall the audio facade hit on 2026-05-10.
The submodule shims ``pipeline/audio/{asr,align,beats,transcribe,
audio}.py`` re-export the underlying flat modules so callers using
either ``from pipeline.audio import audio`` or
``from pipeline import audio`` resolve to the same code.

This module is a **compatibility facade** for what used to be a single
1,873-line file. The actual provider implementations now live under
:mod:`pipeline.tts`:

* :mod:`pipeline.tts.kokoro`        — Kokoro 82M (Apache 2.0)
* :mod:`pipeline.tts.chatterbox`    — Chatterbox (MIT, emotional clone)
* :mod:`pipeline.tts.styletts2`     — StyleTTS2 (MIT, long-form prosody)
* :mod:`pipeline.tts.song`          — Suno API + song trim helpers
* :mod:`pipeline.tts.text_normalize` — currency/year/acronym/Hindi normalisation

The split was driven by audio.py becoming unmaintainable. This file
re-exports every public + every documented-private symbol so existing
callers continue to work unchanged.
"""
from __future__ import annotations

from pathlib import Path

from pipeline import observability as obs
from pipeline import telemetry as tlm

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
    _RE_AD_BC_YEAR,
    _RE_AD_BC_POST_YEAR,
    _RE_VERDICT_ACRONYM,
    _TEENS,
    _TENS,
    _apply_hindi_respellings,
    _spell_ad_bc,
    _spell_ad_bc_post,
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
# Re-exported so tests can patch.object(audio, '_synth_cloudrun_*').
from pipeline.tts.cloudrun import (  # noqa: E402, F401
    CloudRunUnavailable,
    _synth_cloudrun_chatterbox,
    _synth_cloudrun_indicf5,
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
    narration_prosody: list[dict] | None = None,
) -> Path:
    """Generate speech audio. Returns path to the .wav file.

    Public TTS dispatcher — every channel YAML's ``tts_provider``
    funnels through here, so wrapping it once gives every TTS lane a
    span. ``CloudRunUnavailable`` from a ``cloudrun_*`` provider is
    propagated; the local-fallback path inside
    :mod:`pipeline.tts.cloudrun` records its own ``tts_fallback``
    event so the dashboard can split clean cloud success from
    fallback-rescued runs.

    See :func:`_synthesize_impl` for the per-provider routing.
    """
    chars = len(text or "")

    # Resolve voice ID → on-disk path BEFORE any provider sees it.
    resolved_voice = voice
    resolved_transcript = ref_audio_text
    if voice:
        try:
            from pipeline.voice.voice_catalog import resolve_voice as _resolve  # noqa: PLC0415
            from pathlib import Path as _Path  # noqa: PLC0415
            project_root = _Path(__file__).resolve().parents[2]
            wav_path, catalog_transcript = _resolve(voice, project_root)
            if wav_path is not None:
                resolved_voice = str(wav_path)
            # Catalog transcript wins over caller-provided ref_audio_text
            # ONLY when caller didn't pass one.
            if catalog_transcript and not ref_audio_text:
                resolved_transcript = catalog_transcript
        except Exception as exc:  # noqa: BLE001 — best-effort
            import logging as _logging  # noqa: PLC0415
            _logging.getLogger(__name__).warning(
                "voice resolution failed for %r (%s) — provider will "
                "see the unresolved value", voice, exc,
            )

    metadata = {
        "provider": provider,
        "language": language,
        "speed": speed,
        "chars": chars,
        "voice": str(resolved_voice)[:200] if resolved_voice else None,
        "voice_input": str(voice)[:200] if voice and voice != resolved_voice else None,
        "has_ref_text": bool(resolved_transcript),
        "has_prosody": bool(narration_prosody),
        "out_path": str(out_path),
    }
    with obs.timed("tts_synth", category="tts", metadata=metadata) as t:
        result = _synthesize_impl(
            text,
            voice=resolved_voice,
            out_path=out_path,
            speed=speed,
            provider=provider,
            ref_audio_text=resolved_transcript,
            modulation=modulation,
            pronunciation_dict=pronunciation_dict,
            language=language,
            narration_prosody=narration_prosody,
        )
        try:
            import soundfile as _sf  # noqa: PLC0415
            info = _sf.info(str(result))
            t.add(metadata={
                "wav_seconds": round(info.frames / info.samplerate, 2),
                "samplerate": info.samplerate,
            })
        except Exception:  # noqa: BLE001
            try:
                t.add(metadata={"wav_bytes": result.stat().st_size})
            except Exception:  # noqa: BLE001
                pass
        return result


def _synthesize_impl(
    text: str,
    voice: str,
    out_path: Path,
    speed: float = 1.0,
    provider: str = "kokoro",
    ref_audio_text: str | None = None,
    modulation: dict | None = None,
    pronunciation_dict: dict | None = None,
    language: str = "en",
    narration_prosody: list[dict] | None = None,
) -> Path:
    """Generate speech audio. Returns path to the .wav file.

    For ``provider='kokoro'``, ``voice`` is a Kokoro voice id.
    For ``provider='chatterbox'`` / ``'styletts2'``, ``voice`` is a
    ref-WAV path; ``ref_audio_text`` is not required.

    Numbers and currency in ``text`` are expanded to spoken words before
    synthesis (Kokoro reads "$2000" as digit-by-digit otherwise).

    For Indic providers (``cloudrun_indicf5``) Hindi respellings are
    skipped — those models were trained on raw Devanagari and respelling
    actively hurts pronunciation.

    ``narration_prosody`` (chunked cloudrun providers only): per-sentence
    speed + post-pause hints from the LLM rewriter.
    """
    import logging  # noqa: PLC0415
    logger = logging.getLogger("pipeline.audio.audio")

    _INDIC_PROVIDERS = {"cloudrun_indicf5"}
    text = normalize_for_tts(
        text,
        skip_hindi_respellings=(provider in _INDIC_PROVIDERS),
    )
    text = apply_pronunciation_overrides(text, pronunciation_dict)

    _REF_TEXT_REQUIRED = {"cloudrun_indicf5"}
    if provider in _REF_TEXT_REQUIRED and not ref_audio_text:
        raise ValueError(
            f"{provider} provider requires `ref_audio_text` "
            f"(transcript of the reference audio at `voice`)"
        )

    if provider == "kokoro":
        return _synth_kokoro(
            text, voice=voice, out_path=out_path, speed=speed,
            modulation=modulation,
        )
    if provider == "chatterbox":
        return _synth_chatterbox(
            text,
            ref_audio_path=voice,
            out_path=out_path,
            speed=speed,
        )
    if provider == "styletts2":
        return _synth_styletts2(
            text,
            ref_audio_path=voice,
            out_path=out_path,
            speed=speed,
        )
    if provider == "cloudrun_chatterbox":
        kwargs = {
            "text": text, "ref_audio_path": voice,
            "ref_audio_text": ref_audio_text,
            "out_path": out_path, "speed": speed,
        }
        if narration_prosody is not None:
            kwargs["narration_prosody"] = narration_prosody
        return _synth_cloudrun_chatterbox(**kwargs)
    if provider == "cloudrun_indicf5":
        kwargs = {
            "text": text, "ref_audio_path": voice,
            "ref_audio_text": ref_audio_text,
            "out_path": out_path, "speed": speed,
        }
        if narration_prosody is not None:
            kwargs["narration_prosody"] = narration_prosody
        return _synth_cloudrun_indicf5(**kwargs)
    raise ValueError(
        f"unknown TTS provider {provider!r} "
        "(cloud choices: cloudrun_chatterbox, cloudrun_indicf5. "
        "Local choices: kokoro, chatterbox, styletts2.)"
    )
