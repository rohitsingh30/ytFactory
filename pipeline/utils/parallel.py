"""Re-export shim for the flat ``pipeline.parallel`` module."""
import pipeline.parallel as _mod
import sys as _sys
_sys.modules[__name__] = _mod
