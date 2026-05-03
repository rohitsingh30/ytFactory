"""Public chat + confirm endpoints.

POST /api/chat            — exchange one message; may return a draft proposal.
POST /api/chat/confirm    — turn the latest proposal into a Job + first Task.

Anonymous-friendly. Auth + rate-limit middleware lands in task #12.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from control.chat_service import ChatService, new_session_id
from control.queue import get_queue, new_task_id
from shared.schema import (
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
async def chat(req: ChatRequest) -> ChatResponse:
    sid = req.session_id or new_session_id()
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
async def confirm(req: ConfirmRequest) -> ConfirmResponse:
    proposal = chat_service.get_proposal(req.session_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="no proposal in session — keep chatting first")

    job_id = uuid.uuid4().hex
    task_id = new_task_id()

    # The first task on the path: pull the source story (or use the user-provided text).
    # Light worker — runs in the cloud, not on the laptop.
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
    task = TaskEnvelope(
        task_id=task_id,
        job_id=job_id,
        kind=TaskKind.PULL_STORY,
        payload=payload,
    )
    get_queue().enqueue(task)
    logger.info("confirmed proposal session=%s → job=%s task=%s", req.session_id, job_id, task_id)

    return ConfirmResponse(job_id=job_id, task_id=task_id, proposal=proposal.model_dump())


# ---------------------------------------------------------------------------
# Test seam — let the e2e tests inject a deterministic "AI" response.
# ---------------------------------------------------------------------------


def _force_proposal(session_id: str, proposal: ShortProposal) -> None:
    """Tests only — pre-seed a session with a proposal, bypassing Azure."""
    sess = chat_service._get_session(session_id)  # noqa: SLF001
    sess.proposal = proposal
