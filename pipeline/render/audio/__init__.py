"""Audio plugin family — :class:`pipeline.render.contracts.AudioSynthesizer` impls.

Each module here implements the AudioSynthesizer Protocol. The
short_engine + long_engine pick which one based on
``spec.audio_mode`` + ``spec.voice_provider``:

* ``cloudrun_chatterbox`` (and other ``cloudrun_*``) → :mod:`.tts_single`
  for short, :mod:`.tts_chunked` for long.
* ``f5_tts`` / ``kokoro`` (laptop fallbacks) → same dispatch via the
  same modules — they share a common provider abstraction inside
  :mod:`pipeline.tts`.
* ``audio_mode == song`` → :mod:`.song_suno`.

Adding a new audio backend = add a new module here that registers a
Protocol-conforming impl. Engines + spec don't change.

Module-import side-effect: each plugin module calls
:func:`pipeline.render.contracts.register_plugin` at import time, so
``import pipeline.render.audio`` makes every audio impl discoverable
via ``get_plugin("audio", <name>)``.
"""
from __future__ import annotations

# Eager-import every plugin module so their register_plugin calls run.
from . import tts_single  # noqa: F401
from . import tts_chunked  # noqa: F401
from . import audio_from_fixture  # noqa: F401
