"""Re-export shim for the flat ``pipeline.asr`` module.

Tests import the package-style path ``pipeline.audio.asr``; the
implementation still lives at ``pipeline/asr.py`` (top-level flat
module). This shim re-exports everything so both forms work.
"""
from pipeline.asr import *  # noqa: F401,F403
from pipeline.asr import __dict__ as _asr_ns  # noqa: F401

import pipeline.asr as _asr_mod
import sys as _sys

_sys.modules[__name__] = _asr_mod
