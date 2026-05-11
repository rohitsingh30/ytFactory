"""Per-render circuit breaker for the cinematic editor.

Mirrors the :mod:`pipeline.images.images_cloudrun` pattern: the first
:class:`CloudRunUnavailable` raised in a render trips a module-global
flag → all subsequent ``cloudrun.execute_edit`` calls in the same
process skip cloud entirely and route to the laptop executor.

Renderer entry points (``pipeline.render.shorts.make_short`` etc.) call
:func:`reset_circuit_breaker` at the top of every render so a previous
render's cloud failure never carries over.

Why a breaker for editing, when each render only invokes the editor 1
time? Because the optional 8th orchestrator stage runs editing-agent
on EVERY render — so across a batch of 10 Shorts we'd otherwise pay
``CLOUDRUN_EDITING_AGENT_TIMEOUT`` (default 600 s) ten times in a row
on a cloud outage. With the breaker, the first failure trips and the
remaining nine skip cloud and run on the laptop ffmpeg path
(~10-30 s each).
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)


class CloudRunUnavailable(RuntimeError):
    """Raised when the editing cloud service can't satisfy the request
    and the caller should fall back to the local executor (or
    hard-error if ``CLOUDRUN_EDITING_AGENT_DISABLE_FALLBACK=1``)."""


_BREAKER_LOCK = threading.Lock()
_CLOUD_DISABLED_THIS_RENDER: bool = False
_BREAKER_REASON: str | None = None


def reset_circuit_breaker() -> None:
    """Clear the per-render breaker. Call at the top of every render
    entry point so a previous render's cloud failure doesn't carry
    over to the next."""
    global _CLOUD_DISABLED_THIS_RENDER, _BREAKER_REASON
    with _BREAKER_LOCK:
        if _CLOUD_DISABLED_THIS_RENDER:
            logger.info(
                "cloudrun editing-agent circuit breaker reset (was tripped: %s)",
                _BREAKER_REASON,
            )
        _CLOUD_DISABLED_THIS_RENDER = False
        _BREAKER_REASON = None


def trip_breaker(reason: str) -> None:
    global _CLOUD_DISABLED_THIS_RENDER, _BREAKER_REASON
    with _BREAKER_LOCK:
        if not _CLOUD_DISABLED_THIS_RENDER:
            logger.warning(
                "cloudrun editing-agent circuit breaker TRIPPED — falling back "
                "to local ffmpeg for the rest of this render (reason: %s)",
                reason,
            )
            _CLOUD_DISABLED_THIS_RENDER = True
            _BREAKER_REASON = reason


def breaker_open() -> bool:
    with _BREAKER_LOCK:
        return _CLOUD_DISABLED_THIS_RENDER


def fallback_disabled() -> bool:
    """Hard-error mode for canary testing — set
    ``CLOUDRUN_EDITING_AGENT_DISABLE_FALLBACK=1`` to surface cloud bugs
    instead of silently degrading to local."""
    return os.environ.get(
        "CLOUDRUN_EDITING_AGENT_DISABLE_FALLBACK", "",
    ).strip() in ("1", "true")
