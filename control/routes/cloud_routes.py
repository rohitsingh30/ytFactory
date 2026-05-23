"""FastAPI routes powering the Cloud admin tab in web-next.

Mounted in :mod:`control.server_dev` (and re-mounted by ``web/server.py``
in production). Endpoints:

    GET  /api/cloud/health    — live probe of every service (no cache)
    GET  /api/cloud/cost?days=30  — last snapshot of per-service spend
    GET  /api/cloud/deploys   — last snapshot of recent builds + prep
    POST /api/cloud/refresh   — force snapshot_all() now, return paths

Auth mirrors :mod:`control.routes.state_routes`:

* On Cloud Run (``K_SERVICE`` set) — IAM upstream is trusted.
* Off Cloud Run (laptop dev) — ``Authorization: Bearer
  $YTFACTORY_AGENT_TOKEN`` required, matching the same env var the
  Next.js proxy / skill clients use.
"""
from __future__ import annotations

import logging
import hmac
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from pipeline.cloud import cost as cost_mod
from pipeline.cloud import deploys as deploys_mod
from pipeline.cloud import health as health_mod
from pipeline.cloud import snapshot as snapshot_mod
from pipeline.cloud import warm as warm_mod
from pipeline.cloud.services import list_services

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_auth(authorization: str | None) -> None:
    """Same shape as control.routes.state_routes._require_auth."""
    if os.environ.get("K_SERVICE"):
        return  # Cloud Run: IAM upstream already verified
    expected = os.environ.get("YTFACTORY_AGENT_TOKEN")
    if not expected:
        # Dev convenience: if the token isn't set, allow (read-only-ish).
        # This matches the dev posture of the Next.js proxy which sends no
        # token in dev. Production runs always set K_SERVICE.
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    # Audit S1.16 — constant-time compare so an attacker can't byte-by-byte
    # discover the token via response-time side channel.
    if not hmac.compare_digest(token, expected):
        raise HTTPException(403, "invalid token")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@router.get("/api/cloud/services")
def cloud_services(authorization: str | None = Header(None)) -> dict[str, Any]:
    _require_auth(authorization)
    out = []
    for s in list_services():
        out.append({
            "short": s.short,
            "name": s.name,
            "kind": s.kind.value,
            "url": s.url,
            "configured": s.configured,
            "is_job": s.is_job,
            "notes": s.notes,
        })
    return {"services": out}


# ---------------------------------------------------------------------------
# Health (live)
# ---------------------------------------------------------------------------


@router.get("/api/cloud/health")
def cloud_health(authorization: str | None = Header(None)) -> dict[str, Any]:
    """Probe every service NOW. 5 s in-process cache.

    Health was uncached previously: every dashboard tile poll fired one
    sweep — N parallel HTTP probes per call, with up to 15 s timeout per
    dead service. With multiple tabs open the work multiplied.

    The 5 s window is shorter than any meaningful health-state change
    (a service either recovers/dies on a Cloud Run scale event, both
    of which take >> 5 s) so the cached payload is effectively
    real-time-equivalent for UX while collapsing N tabs × M tabs of
    polling onto one sweep.
    """
    _require_auth(authorization)

    import time as _t  # noqa: PLC0415

    now = _t.monotonic()
    with _HEALTH_CACHE_LOCK:
        cached = _HEALTH_CACHE.get(None)
        if cached is not None and (now - cached[0]) < _HEALTH_CACHE_TTL_S:
            return cached[1]

    rows = health_mod.sweep()
    payload = {
        "summary": health_mod.summary(rows),
        "rows": [r.__dict__ for r in rows],
    }
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE[None] = (now, payload)
    return payload


# Module-level cache for cloud_health. ``None`` is the only key — the
# probe targets ``list_services()`` which is bound at import time.
import threading as _hth_threading  # noqa: PLC0415

_HEALTH_CACHE_TTL_S = 5.0
_HEALTH_CACHE: dict[None, tuple[float, dict[str, Any]]] = {}
_HEALTH_CACHE_LOCK = _hth_threading.Lock()


# ---------------------------------------------------------------------------
# Cost (snapshot read)
# ---------------------------------------------------------------------------


@router.get("/api/cloud/cost")
def cloud_cost(
    days: int = Query(30, ge=1, le=90),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Read the latest daily cost snapshot.

    The snapshot itself is written by the launchd cron (see
    ``control/com.ytfactory.cloud-snapshot.plist``). The ``days``
    param is for the chart range; the snapshot already covers up to
    30 days so we slice from it client-side.
    """
    _require_auth(authorization)
    snap = snapshot_mod.latest_snapshot("cost")
    if snap is None:
        return {"available": False, "reason": "no cost snapshot yet — run /api/cloud/refresh"}
    snap = dict(snap)
    if days < snap.get("days", 0) and snap.get("points"):
        # Trim points to the requested window.
        from datetime import date, timedelta  # noqa: PLC0415
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        snap["points"] = [p for p in snap["points"] if p["day"] >= cutoff]
        snap["days"] = days
        snap["range_start"] = cutoff
    return snap


# ---------------------------------------------------------------------------
# Deploys (snapshot read)
# ---------------------------------------------------------------------------


@router.get("/api/cloud/deploys")
def cloud_deploys(authorization: str | None = Header(None)) -> dict[str, Any]:
    _require_auth(authorization)
    snap = snapshot_mod.latest_snapshot("deploys")
    if snap is None:
        return {"available": False, "reason": "no deploys snapshot yet — run /api/cloud/refresh"}
    snap = dict(snap)
    snap["available"] = True
    return snap


# ---------------------------------------------------------------------------
# Refresh + warm
# ---------------------------------------------------------------------------


@router.post("/api/cloud/refresh")
def cloud_refresh(
    cost_days: int = Query(30, ge=1, le=90),
    project: str = Query("ytfactory-prod-v3"),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Force snapshot_all() now. Used by the panel's Refresh button."""
    _require_auth(authorization)
    paths = snapshot_mod.snapshot_all(cost_days=cost_days, project=project)
    return {"ok": True, "paths": paths}


@router.post("/api/cloud/warm")
def cloud_warm(
    channel: str | None = Query(None),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Pre-warm cloud GPU containers for the given channel (or default pair)."""
    _require_auth(authorization)
    report = warm_mod.warm_for_channel(channel=channel)
    return warm_mod.to_dict(report)
