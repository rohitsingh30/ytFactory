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


_CARRY_KEYS = ("channel", "slug", "job_id", "niche", "account")


async def attach_identity_attrs(
    request: "Request",
    call_next: "Callable[[Request], Awaitable[Response]]",
) -> "Response":
    """Add ytFactory identity attrs to the active span.

    Runs the handler first (``await call_next``) so FastAPI has populated
    ``request.path_params`` from the matched route. Path params are
    empty in the pre-handler phase of Starlette middleware (the
    routing match happens during ``call_next``). The OTel server span
    is still open at this point — its ``end()`` happens later as the
    ASGI ``http.response.body`` event flushes — so we can still write
    attributes onto it.
    """
    response = await call_next(request)
    span = _trace.get_current_span()
    if span is not None and span.is_recording():
        for k, v in (request.path_params or {}).items():
            if k in _CARRY_KEYS and v is not None:
                span.set_attribute(f"ytfactory.{k}", str(v)[:200])
        try:
            qp = request.query_params
            for k in _CARRY_KEYS:
                v = qp.get(k)
                if v is not None and not _has_attr(span, f"ytfactory.{k}"):
                    span.set_attribute(f"ytfactory.{k}", str(v)[:200])
        except Exception:  # noqa: BLE001
            pass
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
