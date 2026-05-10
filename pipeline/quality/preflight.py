"""Re-export shim for the flat ``pipeline.preflight`` module."""
import pipeline.preflight as _mod
import sys as _sys
_sys.modules[__name__] = _mod
