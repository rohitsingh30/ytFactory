"""Public chat + confirm endpoints.

POST /api/chat            — exchange one message; may return a draft proposal.
POST /api/chat/confirm    — turn the latest proposal into a Job + first Task.

Anonymous-friendly. Auth + rate-limit middleware lands in task #12.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from control.core import rate_limit
from control.core import jobs as jobs_mod
from control.chat_service import ChatService, new_session_id
from control.core.queue import get_queue, new_task_id
from control.core.schema import (
    ShortProposal,
    TaskEnvelope,
    TaskKind,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat")
chat_service = ChatService()


class ChatRequest(BaseModel):
    message: str
    session_id: str = ""


class ChatResponse(BaseModel):
    response: str
    session_id: str
    proposal: Optional[dict] = None
    configured: bool = True


@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    sid = req.session_id or new_session_id()
    # Anti-abuse: per-IP daily quota + global Azure spend cap.
    rate_limit.check_spend_cap()
    rate_limit.check_and_increment(request, "chat")

    if not chat_service.is_configured:
        return ChatResponse(
            response=(
                "Chat AI not configured. Set AZURE_OPENAI_ENDPOINT and "
                "AZURE_OPENAI_API_KEY (the same values your trading project uses) "
                "and restart the control plane."
            ),
            session_id=sid,
            configured=False,
        )

    result = await chat_service.chat(sid, req.message)
    return ChatResponse(
        response=result.response,
        session_id=sid,
        proposal=result.proposal.model_dump() if result.proposal else None,
    )


class ConfirmRequest(BaseModel):
    session_id: str


class ConfirmResponse(BaseModel):
    job_id: str
    task_id: str
    proposal: dict


@router.post("/confirm", response_model=ConfirmResponse)
async def confirm(req: ConfirmRequest, request: Request) -> ConfirmResponse:
    rate_limit.check_and_increment(request, "confirm")
    proposal = chat_service.get_proposal(req.session_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="no proposal in session — keep chatting first")

    return _enqueue_render_job(proposal)


def _enqueue_render_job(proposal: ShortProposal) -> ConfirmResponse:
    """Shared path for chat-confirm AND form-driven /api/render."""
    job_id = uuid.uuid4().hex
    task_id = new_task_id()

    payload = {
        "job_id": job_id,
        "channel": proposal.channel,
        "format": proposal.format,
        "topic": proposal.topic,
        "source_kind": proposal.source_kind,
        "source_ref": proposal.source_ref,
        "length_s": proposal.length_s,
        "notes": proposal.notes,
    }

    # Job state lives in jobs/<job_id> for the UI to poll.
    jobs_mod.create_job(
        job_id,
        channel=proposal.channel,
        topic=proposal.topic,
        proposal=proposal.model_dump(),
    )

    # Initial work unit: the heavy RENDER_SHORT mega-task. Light fan-out
    # workers (YOUTUBE_UPLOAD, RESEARCH_HANDOFF) get enqueued by the
    # render worker on success.
    task = TaskEnvelope(
        task_id=task_id,
        job_id=job_id,
        kind=TaskKind.RENDER_SHORT,
        payload=payload,
    )
    get_queue().enqueue(task)
    logger.info("enqueued render job=%s task=%s channel=%s", job_id, task_id, proposal.channel)

    return ConfirmResponse(job_id=job_id, task_id=task_id, proposal=proposal.model_dump())




# ---------------------------------------------------------------------------
# Test seam — let the e2e tests inject a deterministic "AI" response.
# ---------------------------------------------------------------------------


def _force_proposal(session_id: str, proposal: ShortProposal) -> None:
    """Tests only — pre-seed a session with a proposal, bypassing Azure."""
    sess = chat_service._get_session(session_id)  # noqa: SLF001
    sess.proposal = proposal
