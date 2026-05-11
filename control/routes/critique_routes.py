"""Critique-chat backend (2026-05-11).

Two HTTP endpoints sit on this router; everything else in the
critique flow happens directly against Firestore (browser <->
laptop runner via realtime ``onSnapshot``):

- POST /api/jobs/{job_id}/critique/start
    Create-or-fetch the parent ``critiques/<id>`` doc for this job.
    Idempotent: if a critique already exists in ``queued`` or
    ``in_progress`` state for this job + caller, return the existing
    id instead of creating a duplicate.

- POST /api/jobs/{job_id}/critique/token
    Mint a short-lived Firebase Auth custom token from the user's
    ``yt_session`` cookie so the browser can subscribe to Firestore
    directly (with the same uid the security rules check on
    ``created_by``).

Both endpoints sit behind the existing OAuth session middleware in
``web/server.py:auth_middleware`` — no per-route auth check needed.

Design + state machine documented in
[`docs/critique_chat.md`](../../docs/critique_chat.md).
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from control.core import jobs as jobs_mod

logger = logging.getLogger(__name__)
router = APIRouter()


# Status values mirror docs/critique_chat.md.
STATUS_QUEUED = "queued"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_ABANDONED = "abandoned"

_LIVE_STATUSES = (STATUS_QUEUED, STATUS_IN_PROGRESS)

# Allowed agent kinds — keep small and explicit so a user can't ask
# the laptop runner to spawn `/bin/bash`.
_ALLOWED_AGENTS = ("claude", "copilot")


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class CritiqueStartRequest(BaseModel):
    agent: str = Field(default="claude", description="claude | copilot")


class CritiqueStartResponse(BaseModel):
    critique_id: str
    job_id: str
    agent: str
    status: str
    mp4_uri: str | None = None
    created: bool  # True = freshly minted; False = returned existing live doc


class CritiqueTokenResponse(BaseModel):
    token: str
    uid: str
    expires_in_s: int


# ---------------------------------------------------------------------------
# Lazy Firestore client + helpers
# ---------------------------------------------------------------------------


def _firestore_client():
    """Lazy import so unit tests don't need the SDK on the path."""
    from google.cloud import firestore  # noqa: PLC0415
    return firestore.Client(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2"),
    )


def _critiques_collection():
    return _firestore_client().collection("critiques")


def _email_to_uid(email: str) -> str:
    """Stable Firebase uid derived from the OAuth email.

    Firestore security rules can then check
    ``request.auth.uid == resource.data.created_by_uid`` after we
    write the same value into the critique doc. We don't reuse the
    raw email as uid because Firebase uids cap at 128 chars and
    Google's RTDB-style rules historically misbehave on '@' / '.'.

    Kept deterministic so successive sessions for the same user land
    on the same uid — important for ownership checks across browsers
    and devices.
    """
    import hashlib  # noqa: PLC0415
    return "yt_" + hashlib.sha256(email.encode("utf-8")).hexdigest()[:48]


def _require_auth_email(request: Request) -> str:
    """The OAuth middleware sets ``request.state.user_email`` after a
    successful session check. Fall back to a clear 401 when missing
    so the browser knows to retry the sign-in flow rather than
    show a generic error."""
    email = getattr(request.state, "user_email", None)
    if not email:
        # Sign-in middleware should have caught this; return 401 in case
        # the caller bypassed it (e.g. local dev with auth disabled).
        raise HTTPException(status_code=401, detail="sign-in required")
    return email


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/api/jobs/{job_id}/critique/start",
    response_model=CritiqueStartResponse,
)
async def critique_start(job_id: str, body: CritiqueStartRequest, request: Request) -> CritiqueStartResponse:
    """Create-or-fetch the parent critique doc for ``job_id``.

    Idempotent on (job_id, created_by, status ∈ live). Two browser
    tabs opened by the same user on the same render share one
    critique session — so the conversation stays coherent instead of
    forking on each refresh.
    """
    if body.agent not in _ALLOWED_AGENTS:
        raise HTTPException(
            status_code=400,
            detail=f"agent must be one of {_ALLOWED_AGENTS}",
        )
    email = _require_auth_email(request)
    uid = _email_to_uid(email)

    job = jobs_mod.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    coll = _critiques_collection()

    # Idempotent return — look for an existing live critique for this
    # (job, owner) pair before minting a new id.
    existing = (
        coll
        .where("job_id", "==", job_id)
        .where("created_by_uid", "==", uid)
        .where("status", "in", list(_LIVE_STATUSES))
        .limit(1)
        .get()
    )
    if existing:
        snap = existing[0]
        d = snap.to_dict() or {}
        return CritiqueStartResponse(
            critique_id=snap.id,
            job_id=job_id,
            agent=str(d.get("agent", body.agent)),
            status=str(d.get("status", STATUS_QUEUED)),
            mp4_uri=d.get("mp4_uri"),
            created=False,
        )

    critique_id = uuid.uuid4().hex
    now = _server_now()
    doc = {
        "critique_id": critique_id,
        "job_id": job_id,
        "channel": job.get("channel"),
        "agent": body.agent,
        "status": STATUS_QUEUED,
        "claimed_by": None,
        "claimed_at": None,
        "created_by": email,
        "created_by_uid": uid,
        "created_at": now,
        "updated_at": now,
        "mp4_uri": job.get("short_uri"),
        "script_uri": (job.get("proposal") or {}).get("script_uri"),
        "summary": None,
        "commit_sha": None,
        "gate_results": None,
        "error": None,
    }
    coll.document(critique_id).set(doc)
    logger.info(
        "critique_start: critique=%s job=%s agent=%s created_by=%s",
        critique_id, job_id, body.agent, email,
    )
    return CritiqueStartResponse(
        critique_id=critique_id,
        job_id=job_id,
        agent=body.agent,
        status=STATUS_QUEUED,
        mp4_uri=doc.get("mp4_uri"),
        created=True,
    )


@router.post(
    "/api/jobs/{job_id}/critique/token",
    response_model=CritiqueTokenResponse,
)
async def critique_token(job_id: str, request: Request) -> CritiqueTokenResponse:
    """Mint a Firebase Auth custom token for the signed-in user.

    The browser hands this token to ``signInWithCustomToken`` in the
    Firebase JS SDK so its direct Firestore reads/writes are auth'd
    as the same uid the security rules will eventually check on
    ``created_by_uid``.

    Custom tokens are signed with the runtime SA's RSA key. On Cloud
    Run we have no local key — same situation as ``signed_url`` —
    so we delegate to ``iam.serviceAccounts.signBlob``. The
    ``firebase-admin`` SDK does this transparently when constructed
    with no explicit credentials *and* the runtime SA has
    ``roles/iam.serviceAccountTokenCreator`` on itself (granted
    2026-05-11 in this same session for the preview-mp4 fix; the
    same grant powers token signing).
    """
    email = _require_auth_email(request)
    uid = _email_to_uid(email)

    # Lazy import: tests can stub firebase_admin without the dep.
    try:
        import firebase_admin  # noqa: PLC0415
        from firebase_admin import auth as fb_auth  # noqa: PLC0415
        from firebase_admin import credentials  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "firebase-admin not installed on the server — add "
                "firebase-admin to requirements and redeploy."
            ),
        ) from exc

    if not firebase_admin._apps:  # noqa: SLF001
        # ApplicationDefault picks up the runtime SA on Cloud Run, the
        # gcloud user on laptop dev. The signBlob delegation kicks in
        # at the create_custom_token call site below.
        firebase_admin.initialize_app(credentials.ApplicationDefault())

    claims = {
        "email": email,
        "yt_actor": "critique_chat",
        "job_id": job_id,
    }
    try:
        token = fb_auth.create_custom_token(uid, claims)
    except Exception as exc:
        # Most common failure: missing serviceAccountTokenCreator self-
        # grant on the runtime SA. Surface a clear pointer instead of
        # the SDK's terse "permission denied".
        msg = str(exc)
        if "iam.serviceAccounts.signBlob" in msg or "private key" in msg:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Cannot mint Firebase custom token — grant "
                    "roles/iam.serviceAccountTokenCreator to the runtime "
                    "service account on itself: see docs/critique_chat.md"
                ),
            ) from exc
        raise HTTPException(status_code=500, detail=f"token mint failed: {msg[:200]}") from exc

    # firebase_admin returns bytes; the JS SDK wants a str.
    if isinstance(token, bytes):
        token = token.decode("ascii")

    return CritiqueTokenResponse(token=token, uid=uid, expires_in_s=3600)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _server_now():
    """Wall clock + Firestore SERVER_TIMESTAMP sentinel.

    Helper exists purely so tests can monkey-patch it instead of
    monkey-patching ``time.time`` (which would also clobber the
    rate-limiter and break test isolation)."""
    from google.cloud import firestore  # noqa: PLC0415
    return firestore.SERVER_TIMESTAMP
