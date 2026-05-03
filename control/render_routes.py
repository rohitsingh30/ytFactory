"""Render + job-status endpoints.

POST /api/render — form-driven enqueue (skips chat, takes structured fields).
GET  /api/jobs/{job_id} — poll for status. UI polls every ~2s after enqueue.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from control import jobs as jobs_mod
from control import rate_limit
from control.chat_routes import ConfirmResponse, _enqueue_render_job
from control.queue import get_queue
from shared.schema import ShortProposal, TaskStatus

logger = logging.getLogger(__name__)

router = APIRouter()


class RenderRequest(BaseModel):
    channel: str  # mystoriesanimated | sportstoriesanimated | mahabharathindi | auto
    topic: str
    notes: str = ""
    format: str = "animated"
    source_kind: str = "auto"
    source_ref: str | None = None
    length_s: int = 55


@router.post("/api/render", response_model=ConfirmResponse)
async def render(req: RenderRequest, request: Request) -> ConfirmResponse:
    """Skip the chat — enqueue a render directly from the per-niche form.

    Counts against the same per-IP `confirm` daily quota as a chat-driven
    render so the form doesn't become a rate-limit bypass.
    """
    if not (req.topic or "").strip():
        raise HTTPException(status_code=422, detail="topic is required")
    rate_limit.check_and_increment(request, "confirm")

    proposal = ShortProposal(
        channel=req.channel,
        format=req.format,
        topic=req.topic.strip(),
        source_kind=req.source_kind,
        source_ref=(req.source_ref or None),
        length_s=max(20, min(120, int(req.length_s))),
        notes=(req.notes or "").strip(),
    )
    return _enqueue_render_job(proposal)


class JobView(BaseModel):
    job_id: str
    channel: str | None = None
    topic: str | None = None
    status: str = "pending"
    stage: str | None = None
    short_uri: str | None = None
    short_signed_url: str | None = None
    youtube_url: str | None = None
    thumb_uri: str | None = None
    error: str | None = None


@router.get("/api/jobs/{job_id}", response_model=JobView)
async def get_job(job_id: str) -> JobView:
    doc = jobs_mod.get_job(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="job not found")

    # If the render landed an mp4 in GCS, surface a short-lived signed URL
    # so the UI can preview/download without exposing bucket credentials.
    short_signed: str | None = None
    short_uri = doc.get("short_uri")
    if short_uri and doc.get("status") == jobs_mod.STATUS_DONE:
        try:
            from control import storage  # noqa: PLC0415 — lazy
            short_signed = storage.signed_url(short_uri, ttl_s=600, method="GET")
        except Exception:  # noqa: BLE001
            logger.warning("signed_url failed for %s", short_uri, exc_info=True)

    return JobView(
        job_id=job_id,
        channel=doc.get("channel"),
        topic=doc.get("topic"),
        status=doc.get("status", "pending"),
        stage=doc.get("stage"),
        short_uri=short_uri,
        short_signed_url=short_signed,
        youtube_url=doc.get("youtube_url"),
        thumb_uri=doc.get("thumb_uri"),
        error=doc.get("error"),
    )


class CancelResponse(BaseModel):
    job_id: str
    cancelled_tasks: int
    job_status: str


@router.post("/api/jobs/{job_id}/cancel", response_model=CancelResponse)
async def cancel_job(job_id: str) -> CancelResponse:
    """Mark a job cancelled + drain any of its tasks still in the queue.

    Already-running tasks (status=LEASED) finish on the agent side; we
    flip the job doc to 'cancelled' so the UI stops polling and the
    YT-upload follow-up will short-circuit on cancelled status.
    Already-done tasks are left alone.
    """
    doc = jobs_mod.get_job(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="job not found")

    cancelled = 0
    q = get_queue()

    # In-memory queue: walk the dict directly. Firestore: query.
    from control.queue import InMemoryQueue, FirestoreQueue, _TASKS  # noqa: PLC0415
    if isinstance(q, InMemoryQueue):
        for t in list(q._tasks.values()):  # type: ignore[attr-defined]
            if t.job_id == job_id and t.status == TaskStatus.QUEUED:
                t.status = TaskStatus.FAILED
                t.error = "cancelled by operator"
                cancelled += 1
    elif isinstance(q, FirestoreQueue):
        try:
            from google.cloud import firestore  # noqa: PLC0415

            db = firestore.Client(project=q._db.project)  # type: ignore[attr-defined]
            qref = (db.collection(_TASKS)
                      .where("job_id", "==", job_id)
                      .where("status", "==", TaskStatus.QUEUED.value))
            for snap in qref.stream():
                snap.reference.update({
                    "status": TaskStatus.FAILED.value,
                    "error": "cancelled by operator",
                })
                cancelled += 1
        except Exception:  # noqa: BLE001
            logger.warning("firestore cancel failed for job %s", job_id, exc_info=True)

    jobs_mod.get_jobs().update(
        job_id, status="cancelled", stage="cancelled", error="cancelled by operator",
    )

    new_doc = jobs_mod.get_job(job_id) or {}
    return CancelResponse(
        job_id=job_id,
        cancelled_tasks=cancelled,
        job_status=new_doc.get("status", "cancelled"),
    )


@router.get("/api/health")
async def health() -> dict:
    """Operator health: deployment + agent presence + spend usage.

    Useful for debugging "why isn't my agent picking up tasks" without
    cracking open the Cloud Run logs.
    """
    from control.agent_routes import get_last_seen  # noqa: PLC0415
    import time

    now = time.time()
    agents = []
    for agent_id, (ts, snap) in get_last_seen().items():
        agents.append({
            "agent_id": agent_id,
            "seconds_ago": int(now - ts),
            "mlx_free_pct": snap.mlx_free_pct,
            "kokoro_warm": snap.kokoro_warm,
            "mflux_warm": snap.mflux_warm,
            "on_battery": snap.on_battery,
        })

    spent = rate_limit.daily_spend_usd()
    cap = rate_limit.daily_cap_usd()

    return {
        "ok": True,
        "agents": agents,
        "azure_spend_usd_today": round(spent, 4),
        "azure_spend_cap_usd": cap,
        "azure_spend_pct": round(100.0 * spent / cap, 2) if cap else None,
    }
