"""Per-request middleware that decorates the OTel HTTP span with
ytFactory-specific identity attrs.

The OTel FastAPI auto-instrumentation already gives us one span per
request (named after the route template) with HTTP semantic-convention
attrs (method, route, status_code). What it doesn't know is which
channel/slug/job_id the request is for — so the dashboard's
"requests for slug=aita-001" view would be impossible to answer
without extra wiring.

This middleware reads the standard route params + query params + a
short list of body keys and merges them onto the active span as
``ytfactory.channel`` / ``ytfactory.slug`` / ``ytfactory.job_id``.
We keep it cheap (no body re-serialisation) — body extraction is
opt-in per route via path-segments only.

Importing this module is safe even in non-HTTP contexts (Cloud Run
JOB workers, laptop pipeline subprocesses): ``fastapi`` is imported
lazily so the module load doesn't pull it in. Without ``fastapi``
installed, :func:`install` raises a clear error if anyone tries to
mount the middleware (no-one does in worker contexts).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

from opentelemetry import trace as _trace

if TYPE_CHECKING:  # type-only — never imported at runtime in worker contexts
    from fastapi import Request, Response


# Audit Q2.11 — pre-fix this set was missing ``render_kind``,
# ``render_mode``, ``run_id``, and ``user``. The dashboard's
# trace-filter UI lets the operator slice by these keys, but for
# HTTP spans (web routes, auto-instrumented FastAPI handlers) the
# attribute was never set → those slices returned empty results
# even when the same trace had the attr on a non-HTTP child span.
# Adding them here means the FastAPI middleware promotes them onto
# the HTTP server span at request time, matching the per-render
# envelope's set in :func:`pipeline.observability.context.render_envelope`.
_CARRY_KEYS = (
    "channel",
    "slug",
    "job_id",
    "niche",
    "account",
    "render_kind",
    "render_mode",
    "run_id",
    "user",
)


async def attach_identity_attrs(
    request: "Request",
    call_next: "Callable[[Request], Awaitable[Response]]",
) -> "Response":
    """Add ytFactory identity attrs to the active span.

    **Audit Q2.12** — pre-fix this attached attrs ONLY after
    ``call_next`` returned. Path params come from FastAPI route
    matching, which only populates them during ``call_next``, so we
    DO need to wait for that. BUT on streaming/SSE/file responses,
    the OTel auto-instrumentor often calls ``end()`` on the server
    span BEFORE the response object propagates back through the
    middleware chain — and span.set_attribute is a silent no-op
    after ``end()``. The attrs never landed on the span for those
    routes.

    Fix: do TWO passes. (1) Pre-handler, attach query-param attrs
    immediately — those don't need the route to be matched, and the
    span is guaranteed to still be open. (2) Post-handler, attach
    path_params (the only attrs that genuinely need ``call_next``
    to populate them) inside ``is_recording()`` guard so a
    span-already-ended path doesn't crash.
    """
    span = _trace.get_current_span()
    # Pass 1 — query-param attrs (available pre-handler).
    if span is not None and span.is_recording():
        try:
            qp = request.query_params
            for k in _CARRY_KEYS:
                v = qp.get(k)
                if v is not None:
                    span.set_attribute(f"ytfactory.{k}", str(v)[:200])
        except Exception:  # noqa: BLE001
            pass
    response = await call_next(request)
    # Pass 2 — path-param attrs (require route match). is_recording
    # is False if the span already ended (streaming response case);
    # we silently skip rather than no-oping the set_attribute calls.
    span = _trace.get_current_span()
    if span is not None and span.is_recording():
        for k, v in (request.path_params or {}).items():
            if k in _CARRY_KEYS and v is not None and not _has_attr(span, f"ytfactory.{k}"):
                span.set_attribute(f"ytfactory.{k}", str(v)[:200])
    return response


def _has_attr(span, key: str) -> bool:
    """Cheap probe — Span doesn't expose attributes via API in stable
    OTel; rely on the in-memory span's attributes dict if present
    (best-effort)."""
    try:
        attrs = getattr(span, "_attributes", None) or getattr(span, "attributes", None)
        if attrs is None:
            return False
        return key in attrs
    except Exception:  # noqa: BLE001
        return False


def install(app) -> None:
    """Mount :func:`attach_identity_attrs` on a FastAPI app. Idempotent."""
    if getattr(app, "_ytfactory_identity_mw", False):
        return
    app.middleware("http")(attach_identity_attrs)
    app._ytfactory_identity_mw = True


__all__ = ["attach_identity_attrs", "install"]
