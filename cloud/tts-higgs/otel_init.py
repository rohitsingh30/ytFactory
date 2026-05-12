"""Shared OTel init for every Cloud Run service in ``cloud/``.

Why this exists separately from ``pipeline.observability``: each
``cloud/<service>/`` directory ships its OWN tiny container image and
must NOT pull the full ``pipeline/`` package (massive transitive
dependency tree). This module is small, dependency-light, and copied
into each container image's Dockerfile.

Usage in any ``cloud/<service>/server.py``::

    from _shared.otel_init import init, instrument_fastapi

    init("tts-chatterbox")               # call at module import
    app = FastAPI(...)
    instrument_fastapi(app)              # call after FastAPI creation

Usage in a Cloud Run JOB (``cloud/render-worker-v2/entrypoint.py``)::

    from _shared.otel_init import init, attach_traceparent_from_env

    init("render-worker-v2")
    attach_traceparent_from_env()        # link to chat request that
                                         # started this JOB
    # ... run the work ...
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional


_logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_STATE: dict = {"initialised": False}


def init(
    service_name: str,
    *,
    service_version: Optional[str] = None,
    extra_resource: Optional[dict] = None,
) -> None:
    """Boot the OTel SDK with GCP exporters. Idempotent.

    On Cloud Run we always export to GCP — Cloud Trace for spans,
    Cloud Monitoring for metrics, Cloud Logging via stdout JSON
    (Cloud Run's logging agent picks up structured stdout).

    Off Cloud Run (local dev / pytest) we still set up the SDK with
    a no-op metric reader so callers' span and log code paths stay
    exercised — but skip the metric exporter that would otherwise try
    to authenticate against the real Cloud Monitoring API at
    construction time.
    """
    with _LOCK:
        if _STATE["initialised"]:
            return
        _STATE["initialised"] = True

    on_cloud_run = bool(
        os.environ.get("K_SERVICE")
        or os.environ.get("CLOUD_RUN_JOB")
        or os.environ.get("CLOUD_RUN_EXECUTION")
    )

    try:
        from opentelemetry import _logs as _logs_api
        from opentelemetry import metrics as _metrics_api
        from opentelemetry import trace as _trace_api
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.metrics import set_meter_provider
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import (
            BatchLogRecordProcessor,
            ConsoleLogRecordExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            PeriodicExportingMetricReader,
        )
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )
        from opentelemetry.trace import set_tracer_provider

        version = (
            service_version
            or os.environ.get("OTEL_SERVICE_VERSION")
            or os.environ.get("K_REVISION")
            or "unknown"
        )
        attrs = {
            SERVICE_NAME: service_name,
            SERVICE_VERSION: version,
            "deployment.environment": "prod" if on_cloud_run else "dev",
        }
        if extra_resource:
            attrs.update(extra_resource)
        resource = Resource.create(attrs)
        # Best-effort merge with the GCP detector (adds gcp.* attrs).
        if on_cloud_run:
            try:
                from opentelemetry.resourcedetector.gcp_resource_detector import (
                    GoogleCloudResourceDetector,
                )
                detected = GoogleCloudResourceDetector(raise_on_error=False).detect()
                resource = resource.merge(detected)
            except Exception:  # noqa: BLE001
                pass

        # Per-process collision-breaker attrs MUST be applied LAST so
        # they're authoritative even if a future GCP detector starts
        # emitting `service.instance.id`. Without these, every Cloud
        # Run JOB execution + every spawned subprocess maps to the
        # SAME `generic_node` Cloud Monitoring resource (all-empty
        # labels) and CreateTimeSeries returns 400 "Points must be
        # written in order" every export interval. Setting
        # `service.instance.id` flips the mapping to `generic_task`
        # with a unique `task_id` per process. We also re-assert
        # `service.name` + `service.version` here because the GCP
        # detector's `Resource.merge` semantics let detected attrs
        # override base attrs — without re-asserting, `service.name`
        # falls back to the detector's `"unknown_service"` default,
        # which then propagates as the `generic_task.job` label and
        # breaks per-service grouping. See
        # `pipeline/observability/otel.py::_cloud_run_identity_attrs`
        # for the full mapping spec — kept in lock-step here because
        # `cloud/<svc>/` cannot import `pipeline/`.
        if on_cloud_run:
            instance_parts = [  # coverage: cloud-only branch — pinned by tests/test_otel_init.py source-grep + canonical runtime via tests/test_obs_otel_init.py::TestCloudRunIdentityAttrs
                os.environ.get("CLOUD_RUN_EXECUTION")
                or os.environ.get("K_REVISION")
                or "unknown",
                os.environ.get("CLOUD_RUN_TASK_INDEX") or "0",
                str(os.getpid()),
            ]
            identity_attrs = {  # coverage: cloud-only branch — pinned by tests/test_otel_init.py source-grep + canonical runtime via tests/test_obs_otel_init.py::TestCloudRunIdentityAttrs
                SERVICE_NAME: service_name,
                SERVICE_VERSION: version,
                "service.instance.id": "-".join(instance_parts),
                "service.namespace": (
                    os.environ.get("K_SERVICE")
                    or os.environ.get("CLOUD_RUN_JOB")
                    or service_name
                ),
                "cloud.region": (
                    os.environ.get("GOOGLE_CLOUD_REGION")
                    or os.environ.get("CLOUD_RUN_REGION")
                    or "asia-southeast1"
                ),
            }
            resource = resource.merge(Resource.create(identity_attrs))  # coverage: cloud-only branch — pinned by tests/test_otel_init.py source-grep + canonical runtime via tests/test_obs_otel_init.py::TestCloudRunIdentityAttrs

        # Span exporter: GCP Cloud Trace on Cloud Run, console
        # everywhere else (avoids authenticating against Cloud Trace
        # in pytest / dev environments).
        tracer_provider = TracerProvider(resource=resource)
        if on_cloud_run:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
            project = os.environ.get("GOOGLE_CLOUD_PROJECT")
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    CloudTraceSpanExporter(project_id=project),
                ),
            )
        else:
            tracer_provider.add_span_processor(
                BatchSpanProcessor(ConsoleSpanExporter()),
            )
        set_tracer_provider(tracer_provider)

        # Metric reader: only on Cloud Run. The cloud-monitoring
        # exporter's constructor performs an eager API call to register
        # custom metric descriptors, which fails (and bricks the test
        # suite) without ADC. Off cloud we skip the reader entirely;
        # spans + logs still flow.
        readers = []
        if on_cloud_run:
            try:
                from opentelemetry.exporter.cloud_monitoring import (
                    CloudMonitoringMetricsExporter,
                )
                project = os.environ.get("GOOGLE_CLOUD_PROJECT")
                # ``add_unique_identifier=True`` appends an 8-hex
                # per-process suffix to every metric. Without it, every
                # Cloud Run JOB task / replica / service-revision writes
                # against the same ``(metric_type, generic_node{global,
                # "", ""})`` tuple — Cloud Monitoring then 400s with
                # ``Points must be written in order`` whenever a fresh
                # writer's start_time is older than the most recent
                # write. Pre-fix this rejected ~100% of metric flushes
                # from render-worker-v2 and (via stderr spam) poisoned
                # ``_format_subprocess_failure``'s log-tail extraction,
                # masking every renderer subprocess error. See the
                # 2026-05-13 cron-failure post-mortem.
                readers.append(
                    PeriodicExportingMetricReader(
                        CloudMonitoringMetricsExporter(
                            project_id=project,
                            add_unique_identifier=True,
                        ),
                        export_interval_millis=60_000,
                    ),
                )
            except Exception as e:  # noqa: BLE001
                _logger.warning("Cloud Monitoring exporter unavailable: %s", e)

        meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        set_meter_provider(meter_provider)

        logger_provider = LoggerProvider(resource=resource)
        # Cloud Run captures structured stdout into Cloud Logging
        # natively; the platform promotes any line whose JSON contains
        # a top-level "severity" key into a ``jsonPayload`` entry. The
        # custom exporter writes one line per record in Cloud-Run-shape
        # so every ``ytfactory.*`` field is independently queryable
        # — what the dashboard's cross-service Cloud Logging reader
        # filters on. (Pre-2026-05-12 we used ConsoleLogRecordExporter
        # whose Python ``__repr__``-ish output indexed only as
        # ``textPayload`` and was not queryable.)
        try:
            from cloud_run_json_exporter import (
                CloudRunStructuredJsonLogExporter,
            )
            log_exporter = CloudRunStructuredJsonLogExporter(
                project_id=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning(
                "CloudRunStructuredJsonLogExporter unavailable, "
                "falling back to ConsoleLogRecordExporter (events will "
                "not be queryable as jsonPayload): %s", e,
            )
            log_exporter = ConsoleLogRecordExporter()
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(log_exporter),
        )
        set_logger_provider(logger_provider)

        _logger.info(
            "OTel initialised: service=%s version=%s cloud_run=%s",
            service_name, version, on_cloud_run,
        )
    except Exception as e:  # noqa: BLE001
        # Service must boot even when OTel deps are missing.
        _logger.warning("OTel init failed (continuing without): %s", e)


def instrument_fastapi(app) -> None:
    """Attach OTel ASGI middleware to a FastAPI app. Idempotent."""
    if getattr(app, "_ytfactory_otel", False):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        app._ytfactory_otel = True
    except Exception as e:  # noqa: BLE001
        _logger.warning("FastAPI instrumentation failed: %s", e)


def instrument_outbound_http() -> None:
    """Instrument requests / httpx clients used inside the service."""
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


def attach_traceparent_from_env() -> None:
    """Used by Cloud Run JOBs whose entrypoint reads the chat-request's
    traceparent from the ``YTFACTORY_TRACEPARENT`` env var (set by the
    control plane when scheduling the JOB).

    Activates the extracted context globally so the JOB's root span
    nests under the chat-request's trace.
    """
    tp = os.environ.get("YTFACTORY_TRACEPARENT")
    if not tp:
        return
    try:
        from opentelemetry import context as otel_context
        from opentelemetry.propagate import extract
        carrier = {"traceparent": tp}
        ts = os.environ.get("YTFACTORY_TRACESTATE")
        if ts:
            carrier["tracestate"] = ts
        ctx = extract(carrier)
        otel_context.attach(ctx)
    except Exception as e:  # noqa: BLE001
        _logger.warning("traceparent attach failed: %s", e)


__all__ = [
    "attach_traceparent_from_env",
    "init",
    "instrument_fastapi",
    "instrument_outbound_http",
]
