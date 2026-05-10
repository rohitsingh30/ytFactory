"""Re-export shim for the flat ``pipeline.probe`` module."""
import pipeline.probe as _mod
import sys as _sys
_sys.modules[__name__] = _mod
