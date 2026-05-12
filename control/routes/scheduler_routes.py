"""POST /api/scheduler/tick — fired by Cloud Scheduler every 30 min.

Auth: same Bearer YTFACTORY_AGENT_TOKEN the agent uses for /api/agent/lease.
Returns whatever scheduler.tick() returns. Never long-running — enqueues
a task and returns immediately; the actual render happens on the agent.
"""
from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Header, HTTPException

from control.core import scheduler

router = APIRouter()


def _require_auth(authorization: str | None) -> None:
    # When running on Cloud Run (K_SERVICE is set by the runtime), trust
    # IAM — the request only reached us because Google Frontend already
    # validated the caller's OIDC token against the run.invoker binding.
    # Cloud Scheduler invokes us with --oidc-service-account-email so its
    # OIDC token consumes the Authorization header; we can't ALSO require
    # an app-level Bearer here.
    if os.environ.get("K_SERVICE"):
        return
    expected = os.environ.get("YTFACTORY_AGENT_TOKEN")
    if not expected:
        # Fail closed in dev/laptop — never let scheduler tick run without auth.
        raise HTTPException(503, "scheduler unauthenticated: YTFACTORY_AGENT_TOKEN not set")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    # Audit S1.16 — constant-time compare so an attacker can't byte-by-byte
    # discover the token via response-time side channel.
    if not hmac.compare_digest(token, expected):
        raise HTTPException(403, "invalid token")


@router.post("/api/scheduler/tick")
async def scheduler_tick(authorization: str | None = Header(None)) -> dict:
    """Cloud Scheduler hits this every 30 min for 24/7 round-robin production."""
    _require_auth(authorization)
    return scheduler.tick()


@router.get("/api/scheduler/state")
async def scheduler_state(authorization: str | None = Header(None)) -> dict:
    """Read-only view of scheduler state for the operator UI."""
    _require_auth(authorization)
    return scheduler._read_state()
