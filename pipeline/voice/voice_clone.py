"""Re-export shim for the flat ``pipeline.voice_clone`` module.

Tests import the package-style path ``pipeline.voice.voice_clone``;
the implementation still lives at ``pipeline/voice_clone.py``. This
shim makes both forms work.
"""
import pipeline.voice_clone as _mod
import sys as _sys
_sys.modules[__name__] = _mod
