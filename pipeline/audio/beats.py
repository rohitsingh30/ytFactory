"""Re-export shim for the flat ``pipeline.beats`` module."""
import pipeline.beats as _mod
import sys as _sys
_sys.modules[__name__] = _mod
