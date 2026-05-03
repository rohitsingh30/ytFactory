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
from shared.schema import ShortProposal

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
