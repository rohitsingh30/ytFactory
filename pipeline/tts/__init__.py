"""Stage 4 — TTS providers, split out of pipeline/audio.py.

Subpackages:

  pipeline.tts.kokoro        — Kokoro 82M (Apache 2.0) — default for fast English/Hindi.
  pipeline.tts.chatterbox    — Resemble AI Chatterbox (MIT) — emotional voice clone.
  pipeline.tts.styletts2     — StyleTTS2 (MIT) — long-form prosody. **broken in default venv**.
  pipeline.tts.song          — Suno API + leading-silence trimming for sung music.
  pipeline.tts.text_normalize — currency / years / acronyms / Hindi respellings.

The public router ``synthesize()`` and the public ``normalize_for_tts()``
both stay in :mod:`pipeline.audio`, which is now a thin compatibility
facade re-exporting everything here.
"""
