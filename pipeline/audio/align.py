"""Re-export shim for the flat ``pipeline.align`` module."""
import pipeline.align as _mod
import sys as _sys
_sys.modules[__name__] = _mod
