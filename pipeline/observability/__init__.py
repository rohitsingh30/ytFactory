"""ytFactory observability layer — OpenTelemetry on top of GCP.

Single source of truth for telemetry. Every laptop pipeline stage,
HTTP route, cron job, Cloud Run service, and subprocess invocation
flows through this package and ends up as a span / log / metric in
Cloud Trace + Cloud Logging + Cloud Monitoring.

Public surface (everything else is implementation detail):

- :func:`init` — idempotent SDK init. Picks exporter from ``OTEL_EXPORTER``
  (``gcp`` / ``otlp`` / ``console`` / ``inmemory`` / ``none``).
- :func:`tracer` / :func:`meter` / :func:`logger` — typed accessors,
  always return a working instrument even before :func:`init` runs
  (no-op providers).
- :func:`track` — emit a discrete event as a log record + counter.
- :func:`timed` — context manager that opens a span, records duration
  in a histogram, and captures exceptions.
- :func:`ctx` — context manager that pushes a :class:`RenderContext`
  (channel / niche / slug / job_id / run_id / render_kind / render_mode)
  onto the call stack so every nested span/log inherits the same attrs.
- :func:`record_exception` — attach an exception to the current span
  and mark it failed.

The shim at :mod:`pipeline.telemetry` keeps the legacy
``track / track_stage_done / timed / read_events / percentile`` surface
working without pipeline-wide churn.

Why one package: every existing call site that wants to emit telemetry
should ``from pipeline import observability as obs`` and use
``obs.timed(...)`` / ``obs.track(...)``. New code skips the shim.
"""
from __future__ import annotations

from .context import (
    RenderContext,
    current_context,
    ctx,
)
from .decorators import traced
from .errors import record_exception
from .otel import (
    current_bundle,
    current_mode,
    init,
    init_in_memory,
    install_logging_bridge,
    is_initialised,
    logger,
    meter,
    reset_for_tests,
    tracer,
)
from .http_middleware import (
    attach_identity_attrs,
    install as install_http_identity_middleware,
)
from .instrumentations import (
    install_all,
    instrument_fastapi,
    instrument_outbound_http,
    instrument_subprocess,
    uninstrument_subprocess,
)
from .render_helpers import (
    JOB_ID_ENV,
    RUN_ID_ENV,
    render_envelope,
)
from .telemetry import (
    EVENT_HISTOGRAM_NAME,
    EVENT_LOG_NAME,
    EVENT_COUNTER_NAME,
    emit_span,
    subscribe,
    timed,
    track,
    unsubscribe,
)
from .bodies import (
    TEL_BODY_MAX_CHARS,
    bodies_enabled,
    hash_full,
    preview,
    redact_secrets,
    track_io,
)

__all__ = [
    "EVENT_COUNTER_NAME",
    "EVENT_HISTOGRAM_NAME",
    "EVENT_LOG_NAME",
    "JOB_ID_ENV",
    "RUN_ID_ENV",
    "RenderContext",
    "TEL_BODY_MAX_CHARS",
    "bodies_enabled",
    "ctx",
    "current_bundle",
    "current_context",
    "current_mode",
    "emit_span",
    "hash_full",
    "init",
    "init_in_memory",
    "install_all",
    "install_http_identity_middleware",
    "install_logging_bridge",
    "instrument_fastapi",
    "instrument_outbound_http",
    "instrument_subprocess",
    "is_initialised",
    "logger",
    "meter",
    "preview",
    "record_exception",
    "redact_secrets",
    "render_envelope",
    "reset_for_tests",
    "subscribe",
    "timed",
    "tracer",
    "track",
    "track_io",
    "traced",
    "uninstrument_subprocess",
    "unsubscribe",
]
