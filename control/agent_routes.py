"""Endpoints used exclusively by the laptop agent.

Three verbs:
- POST /agent/heartbeat — periodic resource snapshot, no work returned.
- POST /agent/lease    — long-poll: claim one matching task, or wait.
- POST /agent/ack/{id} — confirm task done/failed.

All require Authorization: Bearer <YTFACTORY_AGENT_TOKEN>.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Path

from control.auth import require_agent
from control.queue import Queue, get_queue
from control.schema import (
    AckRequest,
    AckResponse,
    AgentResources,
    HeartbeatRequest,
    HeartbeatResponse,
    LeaseRequest,
    LeaseResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", dependencies=[Depends(require_agent)])

# Most recent heartbeat per agent_id. Lives in process; lost on restart, which
# is fine — the next heartbeat re-populates it within 15s.
_LAST_SEEN: dict[str, tuple[float, AgentResources]] = {}


def _q() -> Queue:
    # Fetch on each call so tests can swap the backend per-request.
    return get_queue()


@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(req: HeartbeatRequest) -> HeartbeatResponse:
    _LAST_SEEN[req.resources.agent_id] = (time.time(), req.resources)
    logger.debug("heartbeat from %s: %s", req.resources.agent_id, req.resources.model_dump())
    return HeartbeatResponse()


@router.post("/lease", response_model=LeaseResponse)
async def lease(req: LeaseRequest) -> LeaseResponse:
    """Long-poll for one matching task. Polls the queue every 1s up to 30s."""
    deadline = time.time() + 30.0
    q = _q()
    while True:
        task = q.lease(req.agent_id, req.caps, ttl_s=req.lease_ttl_s)
        if task is not None:
            return LeaseResponse(task=task, wait_s=0)
        if time.time() >= deadline:
            return LeaseResponse(task=None, wait_s=30)
        await asyncio.sleep(1.0)


@router.post("/ack/{task_id}", response_model=AckResponse)
async def ack(req: AckRequest, task_id: str = Path(...)) -> AckResponse:
    q = _q()
    if q.get(task_id) is None:
        raise HTTPException(status_code=404, detail="task not found")
    q.ack(task_id, req.agent_id, ok=(req.status == "ok"), output_uri=req.output_uri, error=req.error)
    return AckResponse()


def get_last_seen() -> dict[str, tuple[float, AgentResources]]:
    """Used by /healthz / telemetry endpoints to surface agent presence."""
    return dict(_LAST_SEEN)
