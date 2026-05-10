"""Compatibility shim — re-exports from ``pipeline.images.images_cloudrun``.

Pre-fix (2026-05-10): two diverged copies of this module existed —
this flat ``pipeline/images_cloudrun.py`` (495 lines, older) AND the
package version ``pipeline/images/images_cloudrun.py`` (505 lines,
newer, adds ``cloudrun_hidream`` + ``cloudrun_qwen_image`` providers).
The renderer's ``from pipeline.images_cloudrun import reset_circuit_breaker``
landed on the flat module's circuit-breaker flag, while in-render
``images.generate(...)`` calls dispatched to the package module's
internal ``_breaker_open()`` check — two different module-globals
guarding the same conceptual state. Net effect: the per-render
``reset_circuit_breaker()`` was a no-op, and a previous render's
``CloudRunUnavailable`` could keep fallback-to-local active forever.

This shim makes ``pipeline.images_cloudrun`` an alias for
``pipeline.images.images_cloudrun`` so EVERY call site (renderer,
warmup, dispatcher) reads + writes the SAME module-globals. No more
split-brain.

The companion fix is ``pipeline/images/__init__.py`` which now
re-exports the package's ``images.images`` API at package level so
``from pipeline import images`` resolves to a populated module
(pre-fix, the empty package __init__.py shadowed every flat-module
function the renderer expected).
"""

from pipeline.images.images_cloudrun import *  # noqa: F401, F403
from pipeline.images.images_cloudrun import (  # noqa: F401
    CloudRunUnavailable,
    reset_circuit_breaker,
    warmup,
)
