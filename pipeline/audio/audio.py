"""Re-export shim — ``pipeline.audio.audio`` aliases the
``pipeline.audio`` package itself for tests written against the
flat-then-promoted layout where the dispatcher lived at
``pipeline.audio.audio``.
"""
import pipeline.audio as _mod
import sys as _sys
_sys.modules[__name__] = _mod
