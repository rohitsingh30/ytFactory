"""Telemetry — structured stage envelopes + events over the stdlib logger (the only logging seam)."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

_LOG = logging.getLogger("ytfactory.pipeline")


class LoggingTelemetry:
    """Emits start/end/failed envelopes around each stage + structured events, to a stdlib logger.

    A Cloud Logging / OTel impl is a drop-in swap behind the same ``Telemetry`` Protocol."""

    def __init__(self, logger: logging.Logger = _LOG) -> None:
        self._log = logger

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        self._log.info("stage.start", extra={"stage": name})
        start = time.monotonic()
        try:
            yield
        except Exception as exc:  # fail-loud: record, then re-raise unchanged
            self._log.error(
                "stage.failed",
                extra={"stage": name, "elapsed_s": round(time.monotonic() - start, 3), "error": repr(exc)},
            )
            raise
        else:
            self._log.info(
                "stage.end",
                extra={"stage": name, "elapsed_s": round(time.monotonic() - start, 3)},
            )

    def event(self, name: str, **meta: Any) -> None:
        # nest caller fields so they can never collide with reserved LogRecord attributes
        self._log.info(name, extra={"event": name, "fields": dict(meta)})
