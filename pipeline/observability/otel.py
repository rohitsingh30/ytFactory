"""OTel SDK boot — providers, resource, idempotent init.

Public API:

- :func:`init` — set up TracerProvider, MeterProvider, LoggerProvider
  and attach exporters once. Subsequent calls are no-ops unless
  ``force=True``.
- :func:`init_in_memory` — convenience wrapper for tests.
- :func:`tracer` / :func:`meter` / :func:`logger` — typed accessors.
  Always return a working instrument; if :func:`init` hasn't run we
  call :func:`init` ourselves so that no caller ever has to remember
  the boot step.
- :func:`reset_for_tests` — tear down providers + exporter buffers.
  Used by the test conftest's autouse fixture.
- :func:`is_initialised` — quick check, useful in dashboard routes
  so they can render "telemetry not configured" hints.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from opentelemetry import _logs as _logs_api
from opentelemetry import metrics as _metrics_api
from opentelemetry import trace as _trace_api
from opentelemetry._logs import set_logger_provider
from opentelemetry.metrics import set_meter_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs._internal import LoggingHandler
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import set_tracer_provider

from .exporters import ExporterBundle, build_exporters, resolve_mode


_logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_STATE: dict = {
    "initialised": False,
    "mode": None,
    "bundle": None,
    "tracer_provider": None,
    "meter_provider": None,
    "logger_provider": None,
}


# ---- public API --------------------------------------------------------


def init(
    *,
    service_name: Optional[str] = None,
    service_version: Optional[str] = None,
    mode: Optional[str] = None,
    extra_resource: Optional[dict] = None,
    force: bool = False,
) -> ExporterBundle:
    """Boot the OTel SDK once.

    Call from every entry point (laptop CLI, FastAPI startup, Cloud Run
    server, render-worker JOB ``main()``). Idempotent.

    Parameters
    ----------
    service_name
        Logical service identifier (e.g. ``ytfactory-laptop``,
        ``tts-chatterbox``, ``render-worker-v2``). Falls back to
        ``OTEL_SERVICE_NAME`` then ``K_SERVICE`` then
        ``ytfactory-laptop``.
    service_version
        Build version. Falls back to ``OTEL_SERVICE_VERSION`` then
        ``K_REVISION`` then ``dev``.
    mode
        Override exporter mode; usually unset (resolved from
        ``OTEL_EXPORTER`` env). See :mod:`exporters` for valid values.
    extra_resource
        Extra resource attributes to merge — e.g.
        ``{"deployment.environment": "prod"}``.
    force
        Re-init even if previously initialised. Used by tests.

    Returns
    -------
    ExporterBundle
        The bundle that was wired in. Tests reach into
        ``bundle.span_inmemory`` etc. for assertions.
    """
    with _LOCK:
        if _STATE["initialised"] and not force:
            return _STATE["bundle"]

        chosen_mode = resolve_mode(mode)
        bundle = build_exporters(chosen_mode)
        resource = _build_resource(service_name, service_version, extra_resource)

        tracer_provider = TracerProvider(resource=resource)
        if bundle.span_processor is not None:
            tracer_provider.add_span_processor(bundle.span_processor)
        set_tracer_provider(tracer_provider)

        readers = [bundle.metric_reader] if bundle.metric_reader else []
        meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        set_meter_provider(meter_provider)

        logger_provider = LoggerProvider(resource=resource)
        if bundle.log_processor is not None:
            logger_provider.add_log_record_processor(bundle.log_processor)
        if bundle.shadow_log_processor is not None:
            # Secondary in-process buffer that backs /api/telemetry/*.
            # Runs alongside the primary exporter so the dashboard has
            # a same-process source of recent events without depending
            # on Cloud Logging ingestion. See
            # ``BoundedInMemoryLogRecordExporter`` in exporters.py.
            logger_provider.add_log_record_processor(
                bundle.shadow_log_processor,
            )
        set_logger_provider(logger_provider)

        _STATE.update({
            "initialised": True,
            "mode": chosen_mode,
            "bundle": bundle,
            "tracer_provider": tracer_provider,
            "meter_provider": meter_provider,
            "logger_provider": logger_provider,
        })

        _logger.info(
            "OTel initialised: mode=%s service=%s version=%s",
            chosen_mode, resource.attributes.get(SERVICE_NAME),
            resource.attributes.get(SERVICE_VERSION),
        )
        return bundle


def init_in_memory(**resource_kwargs) -> ExporterBundle:
    """Convenience for tests: force in-memory exporters + reset."""
    return init(mode="inmemory", force=True, **resource_kwargs)


def reset_for_tests() -> None:
    """Tear down the SDK state (called by the conftest autouse fixture).

    Each test starts from a clean slate so span / log / metric
    assertions don't carry across tests.
    """
    with _LOCK:
        tp = _STATE.get("tracer_provider")
        if tp is not None:
            try:
                tp.shutdown()
            except Exception:  # noqa: BLE001
                pass
        mp = _STATE.get("meter_provider")
        if mp is not None:
            try:
                mp.shutdown()
            except Exception:  # noqa: BLE001
                pass
        lp = _STATE.get("logger_provider")
        if lp is not None:
            try:
                lp.shutdown()
            except Exception:  # noqa: BLE001
                pass
        # Reset the global API singletons so new providers stick.
        # The setter machinery lives in the per-API ``_internal`` module
        # (and is exposed on the parent module for trace, but not for
        # metrics / logs as of OTel 1.41).
        from opentelemetry.metrics import _internal as _metrics_internal
        from opentelemetry._logs import _internal as _logs_internal
        _trace_api._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
        _metrics_internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
        _logs_internal._LOGGER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
        _trace_api._TRACER_PROVIDER = None  # type: ignore[attr-defined]
        _metrics_internal._METER_PROVIDER = None  # type: ignore[attr-defined]
        _logs_internal._LOGGER_PROVIDER = None  # type: ignore[attr-defined]
        _STATE.update({
            "initialised": False,
            "mode": None,
            "bundle": None,
            "tracer_provider": None,
            "meter_provider": None,
            "logger_provider": None,
        })

        # Drop any cached counters/histograms — they hold a reference
        # to the now-shutdown MeterProvider and would emit a
        # "shutdown MeterProvider can not provide a Meter" warning on
        # next use. Local import avoids a cycle at module load time.
        from . import telemetry as _t
        _t._INSTRUMENTS.clear()


def is_initialised() -> bool:
    return bool(_STATE.get("initialised"))


def current_bundle() -> Optional[ExporterBundle]:
    """Return the exporter bundle from the last :func:`init` call."""
    return _STATE.get("bundle")


def current_mode() -> Optional[str]:
    return _STATE.get("mode")


# ---- typed accessors ---------------------------------------------------


def tracer(name: str = "ytfactory") -> _trace_api.Tracer:
    """Return a :class:`Tracer`. Auto-init if needed."""
    if not is_initialised():
        init()
    return _trace_api.get_tracer(name)


def meter(name: str = "ytfactory") -> _metrics_api.Meter:
    if not is_initialised():
        init()
    return _metrics_api.get_meter(name)


def logger(name: str = "ytfactory") -> _logs_api.Logger:
    if not is_initialised():
        init()
    return _logs_api.get_logger(name)


# ---- internals ---------------------------------------------------------


def _build_resource(
    service_name: Optional[str],
    service_version: Optional[str],
    extra: Optional[dict],
) -> Resource:
    name = (
        service_name
        or os.environ.get("OTEL_SERVICE_NAME")
        or os.environ.get("K_SERVICE")
        or "ytfactory-laptop"
    )
    version = (
        service_version
        or os.environ.get("OTEL_SERVICE_VERSION")
        or os.environ.get("K_REVISION")
        or "dev"
    )
    attrs = {
        SERVICE_NAME: name,
        SERVICE_VERSION: version,
        "deployment.environment": _resolve_env(),
    }
    if extra:
        attrs.update(extra)
    base = Resource.create(attrs)
    return _maybe_merge_gcp_resource(base)


def _resolve_env() -> str:
    """Best-effort environment label."""
    if os.environ.get("K_SERVICE"):
        return "prod"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return "test"
    return "dev"


def _maybe_merge_gcp_resource(base: Resource) -> Resource:
    """Merge the GCP resource detector's attrs when running on GCP."""
    if not os.environ.get("K_SERVICE"):
        return base
    try:
        from opentelemetry.resourcedetector.gcp_resource_detector import (
            GoogleCloudResourceDetector,
        )
        detected = GoogleCloudResourceDetector(raise_on_error=False).detect()
        return base.merge(detected)
    except Exception as e:  # noqa: BLE001
        _logger.warning("GCP resource detector failed: %s", e)
        return base


# ---- stdlib logging bridge --------------------------------------------


def install_logging_bridge(
    level: int = logging.INFO,
    *,
    logger_names: Optional[list[str]] = None,
) -> None:
    """Route stdlib :mod:`logging` records into the OTel LoggerProvider.

    Once installed, every ``logging.getLogger(...)`` call's records
    flow through the same exporters as :func:`pipeline.observability.track`
    — so the dashboard sees both pipeline events AND ad-hoc warnings
    from third-party libraries (httpx, urllib3, ffmpeg subprocess).
    """
    if not is_initialised():
        init()
    handler = LoggingHandler(level=level, logger_provider=_STATE["logger_provider"])
    targets = logger_names or [""]
    for name in targets:
        logging.getLogger(name).addHandler(handler)


__all__ = [
    "current_bundle",
    "current_mode",
    "init",
    "init_in_memory",
    "install_logging_bridge",
    "is_initialised",
    "logger",
    "meter",
    "reset_for_tests",
    "tracer",
]
