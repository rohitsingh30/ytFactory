"""Re-export shim for the flat ``pipeline.paths`` module.

The canonical path layout module historically lived at
``pipeline/paths.py``. After the package promotion in commit
5831a6a some callers (mainly tests) started importing from
``pipeline.schemas.paths``. This shim makes both forms equivalent.
"""
import pipeline.paths as _mod
import sys as _sys
_sys.modules[__name__] = _mod
