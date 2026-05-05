"""Stage 4 — TTS providers, split out of pipeline/audio.py.

Subpackages:

  pipeline.tts.kokoro        — Kokoro 82M (Apache 2.0) — default for fast English/Hindi.
  pipeline.tts.f5            — F5-TTS-MLX (MIT) — voice-cloned narration on Apple Silicon.
  pipeline.tts.chatterbox    — Resemble AI Chatterbox (MIT) — emotional voice clone.
  pipeline.tts.styletts2     — StyleTTS2 (MIT) — long-form prosody. **broken in default venv**.
  pipeline.tts.parler        — AI4Bharat Indic Parler-TTS (Apache 2.0) — Hindi description-conditioned.
                                 **broken in default venv** (transformers ≥4.49 incompat).
  pipeline.tts.song          — Suno API + leading-silence trimming for sung music.
  pipeline.tts.text_normalize — currency / years / acronyms / Hindi respellings.

The public router ``synthesize()`` and the public ``normalize_for_tts()``
both stay in :mod:`pipeline.audio`, which is now a thin compatibility
facade re-exporting everything here. Existing callers
(``historyrecapped/scripts/render_long_form.py``, ``web/server.py``,
``tests/test_audio_tts_providers.py``, ``historyrecapped/scripts/regen_audio_caps.py``,
``historyrecapped/scripts/kokoro_voice_samples.py``) keep working
unchanged.

This split was driven by 1,873-line ``pipeline/audio.py`` becoming
unmaintainable — all five providers each carrying their own loader +
synth function + module-state singletons in one file. Each provider is
now self-contained; the broken-in-this-venv ones (styletts2, parler)
fail with the same clear errors at first call as before.
"""
