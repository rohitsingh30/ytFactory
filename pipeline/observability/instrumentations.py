"""Idempotent third-party instrumentation toggles.

Cleanly separates the *opt-in* surface area: each helper here
auto-instruments a specific library so its calls produce spans that
nest under whatever span our pipeline already opened. We don't
auto-call any of these on import — pipeline code that wants
auto-spans imports the helper it cares about.

What's wired:

- :func:`instrument_fastapi(app)` — every HTTP route → a span. Used
  by ``web/server.py`` and ``control/server_dev.py`` (and every
  ``cloud/*/server.py`` via :func:`cloud._shared.otel_init.init`).
- :func:`instrument_outbound_http()` — patches ``requests`` /
  ``httpx`` / ``aiohttp`` clients so every outbound call to Cloud Run
  TTS / image services is a child span and carries the
  ``traceparent`` header.
- :func:`instrument_subprocess()` — wraps :mod:`subprocess` so every
  ffmpeg / gcloud / yt-dlp / playwright invocation is a span. We
  implement this ourselves because there is no upstream OTel
  ``subprocess`` instrumentation; we monkey-patch
  :func:`subprocess.run` and :class:`subprocess.Popen.__init__` to
  emit a span around the call.

Each helper is idempotent — calling it twice is safe.
"""
from __future__ import annotations

import logging
import subprocess
import time
from typing import Any

from .otel import init, tracer
from .telemetry import _build_attrs, _close_span, _TimerHandle


_logger = logging.getLogger(__name__)


_STATE: dict[str, bool] = {
    "fastapi": False,
    "outbound": False,
    "subprocess": False,
    "logging_bridge": False,
}


def instrument_fastapi(app: Any) -> None:
    """Attach the OTel ASGI middleware to a FastAPI / Starlette app."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    if not getattr(app, "_ytfactory_otel_instrumented", False):
        # Boot the SDK before we wrap anything (the instrumentor
        # uses the global tracer/meter/logger).
        init()
        FastAPIInstrumentor.instrument_app(app)
        app._ytfactory_otel_instrumented = True


def instrument_outbound_http() -> None:
    """Patch requests / httpx / aiohttp / urllib client modules globally.

    **Audit Q2.68** — pre-fix this only patched requests / httpx /
    aiohttp. ``pipeline/tts/cloudrun.py`` uses ``urllib.request.urlopen``
    directly (per Q2.69 reconciliation, urllib is the right choice
    for the simple POST→JSON shape there); without an instrumentor
    every TTS Cloud Run call broke the trace-propagation chain.
    Now also patch ``urllib`` via OTel's
    ``URLLibInstrumentor``.
    """
    if _STATE["outbound"]:
        return
    init()
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        RequestsInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001
        _logger.warning("requests instrumentation failed: %s", e)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001
        _logger.warning("httpx instrumentation failed: %s", e)
    try:
        from opentelemetry.instrumentation.aiohttp_client import (
            AioHttpClientInstrumentor,
        )
        AioHttpClientInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001
        _logger.warning("aiohttp instrumentation failed: %s", e)
    # Audit Q2.68 — urllib instrumentor for pipeline/tts/cloudrun.py.
    try:
        from opentelemetry.instrumentation.urllib import URLLibInstrumentor
        URLLibInstrumentor().instrument()  # coverage: requires opentelemetry-instrumentation-urllib package which the laptop test env doesn't ship
    except Exception as e:  # noqa: BLE001
        _logger.warning("urllib instrumentation failed: %s", e)
    _STATE["outbound"] = True


# ---- subprocess --------------------------------------------------------


_ORIG_RUN = None
_ORIG_POPEN_INIT = None


def instrument_subprocess() -> None:
    """Wrap :mod:`subprocess` so every external command is a span.

    Span name = ``subprocess`` ; attributes include ``cmd_name``
    (basename of argv[0]), ``argc``, ``returncode``, ``duration_ms``.
    Idempotent.
    """
    global _ORIG_RUN, _ORIG_POPEN_INIT
    if _STATE["subprocess"]:
        return
    init()
    _ORIG_RUN = subprocess.run
    _ORIG_POPEN_INIT = subprocess.Popen.__init__

    def _wrapped_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        cmd_name = _basename_of(cmd)
        with tracer().start_as_current_span(
            "subprocess",
            attributes={
                "ytfactory.cmd_name": cmd_name,
                "ytfactory.argc": _argc_of(cmd),
            },
        ) as span:
            t0 = time.perf_counter()
            try:
                result = _ORIG_RUN(*args, **kwargs)
            except BaseException as e:
                span.set_attribute("ytfactory.error", str(e)[:512])
                raise
            else:
                span.set_attribute("ytfactory.returncode",
                                   getattr(result, "returncode", -1))
                return result
            finally:
                span.set_attribute(
                    "ytfactory.duration_ms",
                    int((time.perf_counter() - t0) * 1000),
                )

    def _wrapped_popen_init(self, *args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        cmd_name = _basename_of(cmd)
        # Popen returns immediately so we can only capture cmd metadata
        # at construction time. Long-running processes get their own
        # spans via the caller's tlm.timed("...") wrapper.
        span = tracer().start_span(
            "subprocess.popen",
            attributes={
                "ytfactory.cmd_name": cmd_name,
                "ytfactory.argc": _argc_of(cmd),
            },
        )
        span.end()
        return _ORIG_POPEN_INIT(self, *args, **kwargs)

    subprocess.run = _wrapped_run  # type: ignore[assignment]
    subprocess.Popen.__init__ = _wrapped_popen_init  # type: ignore[method-assign]
    _STATE["subprocess"] = True


def uninstrument_subprocess() -> None:
    """Restore the original subprocess functions (used by tests)."""
    if not _STATE["subprocess"]:
        return
    if _ORIG_RUN is not None:
        subprocess.run = _ORIG_RUN  # type: ignore[assignment]
    if _ORIG_POPEN_INIT is not None:
        subprocess.Popen.__init__ = _ORIG_POPEN_INIT  # type: ignore[method-assign]
    _STATE["subprocess"] = False


def _basename_of(cmd: Any) -> str:
    """Return the bare command name (no path, no args)."""
    if cmd is None:
        return "?"
    if isinstance(cmd, (list, tuple)):
        if not cmd:
            return "?"
        return _basename_of(cmd[0])
    if isinstance(cmd, (bytes, bytearray)):
        cmd = cmd.decode("utf-8", "replace")
    s = str(cmd)
    # Take only the executable, splitting on shell separators.
    s = s.strip().split()[0] if s.strip() else "?"
    return s.rsplit("/", 1)[-1]


def _argc_of(cmd: Any) -> int:
    if cmd is None:
        return 0
    if isinstance(cmd, (list, tuple)):
        return len(cmd)
    return 1


# ---- bring up the whole stack at once ----------------------------------


def install_all(app: Any | None = None, *, with_subprocess: bool = True) -> None:
    """One-shot helper used by the laptop FastAPI server bootstrap.

    Initialises the SDK and turns on every available instrumentation.
    Idempotent.
    """
    init()
    if app is not None:
        instrument_fastapi(app)
    instrument_outbound_http()
    if with_subprocess:
        instrument_subprocess()


__all__ = [
    "install_all",
    "instrument_fastapi",
    "instrument_outbound_http",
    "instrument_subprocess",
    "uninstrument_subprocess",
]


# Re-export internal helpers so tests can introspect closures.
__test__ = {
    "_TimerHandle": _TimerHandle,
    "_build_attrs": _build_attrs,
    "_close_span": _close_span,
}
