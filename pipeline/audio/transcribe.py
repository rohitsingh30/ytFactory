"""Re-export shim for the flat ``pipeline.transcribe`` module."""
import pipeline.transcribe as _mod
import sys as _sys
_sys.modules[__name__] = _mod
