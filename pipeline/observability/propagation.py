"""W3C trace-context propagation (HTTP headers + env vars + Firestore).

Built-in OTel propagation already covers HTTP headers automatically
when the FastAPI / requests / httpx / aiohttp instrumentations are
active. This module fills the gaps:

- ``inject_into_env`` / ``extract_from_env`` — Cloud Run JOBs receive
  config via environment variables, not HTTP headers; we read/write
  ``YTFACTORY_TRACEPARENT`` so the JOB's root span links back to the
  laptop / chat request that started it.

- ``inject_into_dict`` / ``extract_from_dict`` — generic helpers for
  Firestore documents, JSON jobs, scheduler queues, etc. Used by
  ``/api/chat/confirm`` to stamp ``traceparent`` onto the job doc and
  by the render-worker to read it back.

We standardise on the W3C ``traceparent`` (and ``tracestate``) format
because (a) it is the OTel default and (b) Cloud Trace's GCP-native
``X-Cloud-Trace-Context`` propagator interoperates with it.
"""
from __future__ import annotations

import os
from typing import Mapping, MutableMapping, Optional

from opentelemetry import context as otel_context
from opentelemetry.propagate import extract, inject


ENV_VAR = "YTFACTORY_TRACEPARENT"
ENV_STATE = "YTFACTORY_TRACESTATE"


# ---- HTTP / generic dict ----------------------------------------------


def inject_into_dict(carrier: MutableMapping[str, str]) -> None:
    """Write the active span's traceparent into ``carrier`` (in place)."""
    inject(carrier)


def extract_from_dict(carrier: Mapping[str, str]) -> otel_context.Context:
    """Return an OTel :class:`Context` extracted from ``carrier``."""
    return extract(carrier)


# ---- Cloud Run JOB env -------------------------------------------------


def inject_into_env(env: Optional[MutableMapping[str, str]] = None) -> MutableMapping[str, str]:
    """Capture the active trace context as ``YTFACTORY_TRACEPARENT`` (+
    optional ``YTFACTORY_TRACESTATE``) in the supplied mapping.

    Returns the SAME mapping that was passed in (or a fresh ``dict`` if
    none was supplied). Callers can pass the return value straight to
    ``gcloud run jobs execute --update-env-vars``.

    Audit D3.66 — pre-fix this returned ``dict(sink)`` (a copy) when
    ``env`` was None and ``env`` (the original) otherwise, which is
    inconsistent with the docstring's "Returns the same mapping"
    claim AND lied about the typing — annotation said
    ``dict[str, str]`` even when the input was a non-dict
    MutableMapping. Now both branches return the same instance and
    the annotation is widened to ``MutableMapping``.
    """
    sink = env if env is not None else {}
    headers: dict[str, str] = {}
    inject(headers)
    tp = headers.get("traceparent")
    ts = headers.get("tracestate")
    if tp:
        sink[ENV_VAR] = tp
    if ts:
        sink[ENV_STATE] = ts
    return sink


def extract_from_env(
    env: Optional[Mapping[str, str]] = None,
) -> otel_context.Context:
    """Return an OTel :class:`Context` extracted from process env vars.

    Used by Cloud Run JOB workers in ``main()``: attach the returned
    context with :func:`opentelemetry.context.attach` before opening
    the JOB's root span so the span links back to the chat request.
    """
    src = env if env is not None else os.environ
    tp = src.get(ENV_VAR)
    if not tp:
        return otel_context.Context()
    carrier: dict[str, str] = {"traceparent": tp}
    ts = src.get(ENV_STATE)
    if ts:
        carrier["tracestate"] = ts
    return extract(carrier)


__all__ = [
    "ENV_STATE",
    "ENV_VAR",
    "extract_from_dict",
    "extract_from_env",
    "inject_into_dict",
    "inject_into_env",
]
