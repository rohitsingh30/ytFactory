"""Render + job-status endpoints.

POST /api/render            — form-driven enqueue (skips chat, takes structured fields).
GET  /api/jobs              — list with filters
GET  /api/jobs/{job_id}     — poll for status (timeline, critique, log_tail)
GET  /api/jobs/{job_id}/preview.mp4 — proxy mp4 (sim or GCS-signed)
POST /api/jobs/{job_id}/publish     — publish to YouTube
POST /api/jobs/{job_id}/cancel
GET  /api/queue             — queued / running / held
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from control.core import jobs as jobs_mod
from control.core import rate_limit
from control.routes.auth_pin import require_pin
from control.core.jobs import ConfirmResponse, _enqueue_render_job
from control.core.queue import get_queue
from control.core.schema import ShortProposal, TaskStatus

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Path-traversal defence — Audit S1.5 + S1.6 ────────────────────
#
# Every endpoint that takes a (channel, slug) pair from URL params
# and joins it into a filesystem path MUST validate both against the
# safe-name regex below AND verify the resulting absolute path stays
# under the documented base directory. Pre-fix, attacker-controlled
# slug like ``../../etc/passwd`` could either WRITE markdown to
# arbitrary locations on the host (S1.5) or READ any matching file
# back (S1.6). The two helpers _safe_name() + _safe_join() shut down
# both paths; routes call them at the top of every channel/slug
# handler.

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


def _safe_name(name: str, *, label: str) -> str:
    """Reject channel / slug values that could escape a parent dir.

    Allowed: alnum / underscore / dot / hyphen, with a leading
    alphanumeric or underscore (no leading ``-`` so a slug can't be
    misread as a CLI flag downstream). Rejects ``..``, ``/``, ``\\``,
    NUL, anything with whitespace.
    """
    if not name or not _SAFE_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"invalid {label}: must match {_SAFE_NAME_RE.pattern!r}",
        )
    return name


def _safe_join(base: Path, *parts: str) -> Path:
    """Join parts onto ``base`` and assert the result is contained.

    Resolves the candidate to an absolute path then verifies
    ``is_relative_to(base.resolve())``. Raises HTTPException(400) on
    escape so the route returns a clean 400 instead of leaking the
    attempted path back to the attacker.
    """
    base_resolved = base.resolve()
    candidate = (base / Path(*parts)).resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"path traversal: {candidate} escapes {base_resolved}",
        ) from e
    return candidate


class RenderRequest(BaseModel):
    channel: str  # mystoriesanimated | sportsrecapped | mahabharathindi | auto
    topic: str
    notes: str = ""
    format: str = "animated"
    source_kind: str = "auto"
    source_ref: str | None = None
    length_s: int = 55
    # Use default_factory so each request gets a fresh dict.
    # Pre-fix (catalogue W-13): a bare ``= {}`` is a mutable-default
    # footgun — Pydantic v2 currently copies it but the pattern is
    # easy to break (subclass / future SDK upgrade) and lints flag it.
    channel_overrides: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/render", response_model=ConfirmResponse)
async def render(
    req: RenderRequest,
    request: Request,
    _pin: None = Depends(require_pin),
) -> ConfirmResponse:
    """Skip the chat — enqueue a render directly from the per-niche form.

    Counts against the same per-IP `confirm` daily quota as a chat-driven
    render so the form doesn't become a rate-limit bypass.
    """
    if not (req.topic or "").strip():
        raise HTTPException(status_code=422, detail="topic is required")
    # Defense-in-depth: reject renders for in_rotation:false channels
    # at the API boundary so even direct API calls (bypassing the
    # wizard) can't crash the worker. Pre-fix the wizard hid these
    # channels but the API would happily accept them. Catalogue:
    # NCH-08 + telemetry TEL-LOG-14 (4 scrollpulse crashes), TEL-FS-16
    # (5 rhyme crashes).
    from pipeline.channels import get_channel  # noqa: PLC0415
    ch = get_channel(req.channel)
    if ch is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown channel {req.channel!r} — "
                f"not registered in pipeline/channels.yaml"
            ),
        )
    if not ch.in_rotation:
        raise HTTPException(
            status_code=422,
            detail=(
                f"channel {req.channel!r} is currently disabled "
                f"(in_rotation:false in pipeline/channels.yaml). "
                f"See the wizard's tooltip for the reason and "
                f"the Tier 2 fix to bring it back."
            ),
        )
    rate_limit.check_and_increment(request, "confirm")

    proposal = ShortProposal(
        channel=req.channel,
        format=req.format,
        topic=req.topic.strip(),
        source_kind=req.source_kind,
        source_ref=(req.source_ref or None),
        length_s=_clamp_length(req.length_s),
        notes=(req.notes or "").strip(),
        channel_overrides=req.channel_overrides or {},
    )
    # Audit S1.7 — stamp the requesting user onto the job so the read
    # endpoints can fence per-user access.
    owner_uid = getattr(request.state, "user_email", None)
    return _enqueue_render_job(proposal, owner_uid=owner_uid)


def _job_owner_check(request: Request, doc: dict) -> None:
    """Audit S1.7 — fence /api/jobs/{id}/* read endpoints to the doc's
    owner. Pre-fix, owner_uid was stored on the doc but never compared
    against ``request.state.user_email`` → any authenticated user
    could poll/preview every other user's renders + signed GCS URLs.

    Rules:

    - Admin (request.state.user_is_admin) — sees everything, no check.
    - Doc has no owner_uid (legacy doc, or scheduler-driven job that
      doesn't have a single human owner) — admit (back-compat); admins
      see everything anyway.
    - Doc has an owner_uid AND the requesting user matches — admit.
    - Doc has an owner_uid AND the requesting user does NOT match —
      403. Distinct from 404 so the operator can tell it's a permissions
      issue, not a missing job.

    Anonymous (no user_email on request.state — typically only the
    M2M / agent path) is treated as admin-equivalent for now: those
    paths already gate by Bearer/IAM upstream so the request only
    reaches us with explicit machine auth. The S1.7 fix is about
    cross-tenant browser access, not M2M.
    """
    if getattr(request.state, "user_is_admin", False):
        return
    user = getattr(request.state, "user_email", None)
    if user is None:
        # M2M / unauthenticated dev — no user identity to compare
        # against. Allowed; upstream auth already gated this.
        return
    owner = doc.get("owner_uid")
    if owner is None:
        # Legacy doc with no owner — admit for back-compat. Once
        # every in-flight job carries owner_uid we can flip this to
        # admin-only.
        return
    if owner != user:
        raise HTTPException(
            status_code=403,
            detail="forbidden: this job belongs to a different owner",
        )


def _clamp_length(raw: int) -> int:
    """Short = 20–120s; long-form = 121–7200s (≤ 2hr)."""
    n = int(raw)
    if n > 120:
        return max(121, min(7200, n))
    return max(20, min(120, n))


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
    thumb_signed_url: str | None = None
    error: str | None = None
    timeline: list[dict] | None = None
    critique: dict | None = None
    log_tail: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    proposal: dict | None = None
    preview_url: str | None = None  # always-correct URL the UI <video> can play

    # Slice 4 — live artifact previews. Each entry mirrors what
    # pipeline.render.artifacts.emit_artifact wrote to Firestore:
    # {status: "pending"|"ready"|"failed", uri, version, ...extras}.
    # Dashboard reads these to render inline previews as artifacts
    # arrive (script as text, narration as <audio>, images as a grid,
    # video as <video>).
    artifacts: dict | None = None
    # Resolved RenderSpec (Slice 1) so the dashboard can show
    # "the system interpreted your inputs as kind=long_form, aspect=16:9"
    # alongside the live previews.
    render_spec: dict | None = None


def _doc_to_view(job_id: str, doc: dict) -> JobView:
    short_signed: str | None = None
    thumb_signed: str | None = None
    short_uri = doc.get("short_uri")
    thumb_uri = doc.get("thumb_uri")
    # Only attempt GCS signing when the uri is gs:// (sim writes a sim:// scheme).
    if short_uri and short_uri.startswith("gs://") and doc.get("status") == jobs_mod.STATUS_DONE:
        try:
            from control.core import storage  # noqa: PLC0415
            short_signed = storage.signed_url(short_uri, ttl_s=600, method="GET")
        except Exception:  # noqa: BLE001
            logger.warning("signed_url failed for %s", short_uri, exc_info=True)
    if thumb_uri and thumb_uri.startswith("gs://"):
        try:
            from control.core import storage  # noqa: PLC0415
            thumb_signed = storage.signed_url(thumb_uri, ttl_s=600, method="GET")
        except Exception:  # noqa: BLE001
            pass

    # Always-correct preview URL — the UI never has to switch on scheme.
    # Gate on the artifacts ACTUALLY existing, not on the wider status
    # window. Pre-2026-05-12 this was gated on
    # ``status in (done, uploading)`` — but during ``status=uploading``
    # the worker has flipped status BEFORE the actual GCS upload
    # completed, so ``short_uri`` isn't populated yet. The dashboard
    # then mounted a ``<video src="/api/jobs/<id>/preview.mp4">``
    # which 404'd (the ``preview_mp4`` route below correctly returns
    # 404 when neither preview_local_path nor short_uri is set). The
    # symptom: a noisy 404 in DevTools the moment the upload pill
    # flipped to "running", before the upload actually finished.
    preview_url: str | None = None
    if doc.get("preview_local_path") or doc.get("short_uri"):
        preview_url = f"/api/jobs/{job_id}/preview.mp4"

    return JobView(
        job_id=job_id,
        channel=doc.get("channel"),
        topic=doc.get("topic"),
        status=doc.get("status", "pending"),
        stage=doc.get("stage"),
        short_uri=short_uri,
        short_signed_url=short_signed,
        thumb_uri=thumb_uri,
        thumb_signed_url=thumb_signed,
        youtube_url=doc.get("youtube_url"),
        error=doc.get("error"),
        timeline=doc.get("timeline"),
        critique=doc.get("critique"),
        log_tail=doc.get("log_tail"),
        created_at=str(doc.get("created_at")) if doc.get("created_at") else None,
        updated_at=str(doc.get("updated_at")) if doc.get("updated_at") else None,
        proposal=doc.get("proposal"),
        preview_url=preview_url,
        artifacts=doc.get("artifacts"),
        render_spec=doc.get("render_spec"),
    )


@router.get("/api/jobs/{job_id}", response_model=JobView)
async def get_job(job_id: str, request: Request) -> JobView:
    doc = jobs_mod.get_job(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="job not found")
    _job_owner_check(request, doc)  # audit S1.7
    return _doc_to_view(job_id, doc)


@router.get("/api/jobs/{job_id}/preview.mp4")
async def preview_mp4(job_id: str, request: Request):
    """Return the rendered (or simulated) mp4.

    - sim:// → serve the cached placeholder file from disk.
    - gs://  → 302 redirect to a signed URL (browser plays it directly).
    - none   → 404.
    """
    doc = jobs_mod.get_job(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="job not found")
    _job_owner_check(request, doc)  # audit S1.7

    short_uri = doc.get("short_uri")
    local = doc.get("preview_local_path")

    if local and Path(local).exists():
        return FileResponse(local, media_type="video/mp4", filename=f"{job_id}.mp4")

    if short_uri and short_uri.startswith("sim://"):
        # Sim render but the per-job local path wasn't recorded — fall back
        # to the shared placeholder.
        from control.core.sim_worker import PLACEHOLDER_MP4  # noqa: PLC0415
        if PLACEHOLDER_MP4.exists():
            return FileResponse(str(PLACEHOLDER_MP4), media_type="video/mp4")
        raise HTTPException(status_code=404, detail="sim placeholder mp4 missing")

    if short_uri and short_uri.startswith("gs://"):
        try:
            from control.core import storage  # noqa: PLC0415
            signed = storage.signed_url(short_uri, ttl_s=600, method="GET")
            return RedirectResponse(url=signed, status_code=302)
        except Exception as e:  # noqa: BLE001
            logger.warning("signed_url failed for %s", short_uri, exc_info=True)
            raise HTTPException(status_code=502, detail=f"GCS signing failed: {e}")

    raise HTTPException(status_code=404, detail="no preview available yet")


# ---------------------------------------------------------------------------
# Slice 4 — live artifact previews
# ---------------------------------------------------------------------------


@router.get("/api/jobs/{job_id}/artifact/{kind}")
async def artifact_redirect(
    job_id: str, kind: str, request: Request,
    index: int | None = None,
):
    """302-redirect to a 1-hour signed URL for a per-job artifact.

    Single endpoint for every artifact kind (script / narration /
    beats / images[i] / envelope / thumb / video / preview). The
    Firestore job doc's ``artifacts.<kind>`` field carries the
    ``gs://`` URI; we sign + redirect.

    Args:
        job_id: Firestore job id.
        kind: Artifact kind. Must match what
            :mod:`pipeline.render.artifacts` wrote.
        index: For list-typed kinds (``images``, ``panels``), which
            entry to fetch. Required when the artifact is list-typed.

    Errors:
        404 — job missing, artifact kind not yet ready, OR list-typed
              kind without an index.
        502 — GCS signing failed.
    """
    doc = jobs_mod.get_job(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="job not found")
    _job_owner_check(request, doc)  # audit S1.7

    artifacts = doc.get("artifacts") or {}
    entry = artifacts.get(kind)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"artifact '{kind}' not yet ready for job {job_id}",
        )

    # List-typed: pull the requested index.
    if isinstance(entry, list):
        if index is None:
            raise HTTPException(
                status_code=400,
                detail=f"artifact '{kind}' is list-typed; pass ?index=N",
            )
        if index < 0 or index >= len(entry):
            raise HTTPException(
                status_code=404,
                detail=f"artifact '{kind}'[{index}] out of range "
                       f"(len={len(entry)})",
            )
        entry = entry[index]
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=500,
                detail=f"artifact '{kind}'[{index}] malformed",
            )

    if entry.get("status") != "ready":
        raise HTTPException(
            status_code=404,
            detail=f"artifact '{kind}' status={entry.get('status')!r} — not ready",
        )

    uri = entry.get("uri")
    if not uri or not isinstance(uri, str) or not uri.startswith("gs://"):
        raise HTTPException(
            status_code=404,
            detail=f"artifact '{kind}' has no gs:// uri",
        )

    try:
        from control.core import storage  # noqa: PLC0415
        signed = storage.signed_url(uri, ttl_s=3600, method="GET")
    except Exception as exc:  # noqa: BLE001
        logger.warning("signed_url failed for %s: %s", uri, exc)
        raise HTTPException(status_code=502, detail=f"GCS signing failed: {exc}")

    return RedirectResponse(url=signed, status_code=302)


class JobsListResponse(BaseModel):
    jobs: list[JobView]
    total: int


@router.get("/api/jobs", response_model=JobsListResponse)
async def list_jobs(
    request: Request,
    channel: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JobsListResponse:
    """List jobs with simple filters.

    Backend:
    - InMemoryJobs: walks the dict.
    - FirestoreJobs: streams jobs collection.

    Audit S1.7 — non-admin callers see only their OWN jobs (matched
    on owner_uid). Legacy docs without owner_uid stay visible to
    everyone for back-compat (admins see them too).
    """
    backend = jobs_mod.get_jobs()
    docs: list[tuple[str, dict]] = []

    # In-memory: introspect the private dict (test backend).
    if isinstance(backend, jobs_mod._MemoryJobs):  # noqa: SLF001
        with backend._lock:  # noqa: SLF001
            docs = [(jid, dict(d)) for jid, d in backend._jobs.items()]  # noqa: SLF001
    else:
        try:
            from google.cloud import firestore  # noqa: PLC0415
            db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2"))
            stream = db.collection("jobs").order_by("created_at", direction=firestore.Query.DESCENDING).limit(500).stream()
            for snap in stream:
                d = snap.to_dict() or {}
                docs.append((snap.id, d))
        except Exception:  # noqa: BLE001
            logger.warning("firestore jobs list failed", exc_info=True)
            docs = []

    if channel:
        docs = [(j, d) for j, d in docs if d.get("channel") == channel]
    if status:
        docs = [(j, d) for j, d in docs if d.get("status") == status]

    # Audit S1.7 — non-admin sees only their own + ownerless docs.
    if not getattr(request.state, "user_is_admin", False):
        user = getattr(request.state, "user_email", None)
        if user is not None:
            docs = [
                (j, d) for j, d in docs
                if d.get("owner_uid") in (None, user)
            ]

    # Sort newest first by updated_at.
    docs.sort(key=lambda jd: str(jd[1].get("updated_at") or ""), reverse=True)
    total = len(docs)
    page = docs[offset:offset + limit]

    return JobsListResponse(
        jobs=[_doc_to_view(jid, d) for jid, d in page],
        total=total,
    )


class PublishRequest(BaseModel):
    visibility: str = "unlisted"  # public | unlisted | private
    schedule_at: str | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] = []
    upload_method: str = "auto"  # auto | api | playwright


class PublishResponse(BaseModel):
    job_id: str
    status: str  # submitted | scheduled | uploading | done | failed
    youtube_url: str | None = None
    error: str | None = None


@router.post("/api/jobs/{job_id}/publish", response_model=PublishResponse)
async def publish(
    job_id: str,
    body: PublishRequest,
    request: Request,
    _pin: None = Depends(require_pin),
) -> PublishResponse:
    """Publish a finished render to YouTube.

    Sim mode: returns a fake youtube_url + marks the job published.
    Real mode (later): kicks the YOUTUBE_UPLOAD light task with metadata.
    """
    from control.core.sim_worker import is_enabled as sim_enabled  # noqa: PLC0415

    doc = jobs_mod.get_job(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="job not found")
    _job_owner_check(request, doc)  # audit S1.7
    if doc.get("status") != jobs_mod.STATUS_DONE:
        raise HTTPException(
            status_code=409,
            detail=f"job not ready (status={doc.get('status')!r}); render must complete first",
        )

    # Sim path — instant fake upload so the UI flow works end-to-end.
    if sim_enabled():
        fake = f"https://youtu.be/sim-{job_id[:11]}"
        jobs_mod.get_jobs().update(
            job_id,
            youtube_url=fake,
            publish_meta={
                "visibility": body.visibility,
                "schedule_at": body.schedule_at,
                "title": body.title,
                "description": body.description,
                "tags": body.tags,
                "upload_method": "sim",
            },
        )
        return PublishResponse(
            job_id=job_id,
            status="done",
            youtube_url=fake,
        )

    # Real path — defer to the existing pipeline.upload.upload module.
    # The real-cloud milestone wires this into a Cloud Run Job. For now,
    # surface a clean "not implemented in cloud yet" so the UI can render
    # the right message.
    raise HTTPException(
        status_code=501,
        detail="real publish path is wired to the Cloud Run worker (next milestone)",
    )


class CancelResponse(BaseModel):
    job_id: str
    cancelled_tasks: int
    job_status: str


@router.post("/api/jobs/{job_id}/cancel", response_model=CancelResponse)
async def cancel_job(job_id: str, request: Request) -> CancelResponse:
    """Mark a job cancelled + drain any of its tasks still in the queue."""
    doc = jobs_mod.get_job(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="job not found")
    _job_owner_check(request, doc)  # audit S1.7

    cancelled = 0
    q = get_queue()

    from control.core.queue import InMemoryQueue, FirestoreQueue, _TASKS  # noqa: PLC0415
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


# ---------------------------------------------------------------------------
# Queue + holds
# ---------------------------------------------------------------------------


class QueueResponse(BaseModel):
    queued: list[dict]
    running: list[dict]
    completed: list[dict]
    held: list[dict]
    # Per-section error strings. Empty when the section's underlying
    # query succeeded; populated with a short, operator-readable reason
    # when it didn't (e.g. "missing Firestore composite index — deploy
    # firestore.indexes.json"). The UI surfaces these so a half-broken
    # backend never silently presents itself as an empty queue —
    # the original silent-swallow bug that made operators believe
    # /api/queue served fake data when really the terminal-state query
    # was 400ing on a missing composite index for 18 real failed jobs.
    warnings: dict[str, str] = {}


# How many recent terminal-state jobs the queue page surfaces in the
# "Completed" column. Bigger → noisier; smaller → user can't scroll
# back far enough to find a render they shipped half a day ago. 20 is
# a "scroll-but-don't-overwhelm" sweet spot per dashboard convention.
_COMPLETED_LIMIT = 20


def _summarise_firestore_error(exc: Exception) -> str:
    """Turn a Firestore exception into a one-line operator hint.

    The most common failure mode for /api/queue is a missing composite
    index — Firestore throws ``FailedPrecondition: 400 The query requires
    an index. You can create it here: https://...``. We strip the URL
    (it's noisy + leaks project id) and substitute a deploy hint that
    points the operator at the source of truth (firestore.indexes.json)
    so the fix is self-service instead of "click the link in the log
    every time the project gets re-bootstrapped".
    """
    msg = str(exc)
    if "requires an index" in msg.lower():
        return (
            "Firestore composite index missing — deploy "
            "firestore.indexes.json (gcloud firestore indexes composite "
            "create or `firebase deploy --only firestore:indexes`)"
        )
    # Trim — the wire payload from Firestore can be multi-KB and we only
    # want enough for an operator to grep logs.
    return f"{type(exc).__name__}: {msg[:200]}"


@router.get("/api/queue", response_model=QueueResponse)
async def get_queue_state() -> QueueResponse:
    """Snapshot of the queue + per-channel holds + recent completions.

    "queued" / "running" come from job docs (not raw tasks) so the UI can
    show topic + channel without an extra fetch. "completed" returns the
    last ~20 terminal-state (done | failed | cancelled) jobs so the Queue
    page also serves as the "what just shipped, ready to review" surface
    — without forcing a roundtrip to Library. "held" comes from each
    channel's _holds.json (written by /ingest-critiques).

    Error handling: each Firestore query has its OWN try/except so a
    missing composite index on the terminal query can't blank out the
    active queue (or vice-versa). Failures are surfaced in
    ``warnings[section]`` so the UI can render an actionable banner
    instead of silently lying about an empty queue.

    KNOWN GAP (2026-05-13): nothing reaps stuck-pending docs from
    the ``jobs/*`` collection — a Cloud Run JOB worker that crashes
    before its first ``jobs_mod.update(...)`` writeback (e.g.
    "Internal error running task" pre-stage-rendering) leaves the
    doc permanently at ``status=pending, stage=dispatching`` and
    this endpoint surfaces it in the Queued column forever. The
    existing ``web/server.py::_periodic_queue_reaper`` only walks
    ``agent_tasks/*`` (cloud render-worker JOB leases). Mitigation
    design in ``docs/jobs_collection_reaper.md`` (Option A: extend
    that loop to walk ``jobs/*`` and cross-check ``cloud_execution``
    against ``run_v2.ExecutionsClient``; Option B: SIGTERM/atexit
    writeback in the render entrypoints)."""
    backend = jobs_mod.get_jobs()
    queued: list[dict] = []
    running: list[dict] = []
    completed: list[dict] = []
    warnings: dict[str, str] = {}

    if isinstance(backend, jobs_mod._MemoryJobs):  # noqa: SLF001
        with backend._lock:  # noqa: SLF001
            docs = [(jid, dict(d)) for jid, d in backend._jobs.items()]  # noqa: SLF001
        # Terminal-state pool gets sorted newest-first and clipped — same
        # docs feed both the active queue and the completed column, so a
        # single in-memory pass covers both. "cancelled" is included so a
        # render the operator just stopped doesn't vanish from the Queue
        # page — they can still click through to review it.
        terminal_docs = sorted(
            [(jid, d) for jid, d in docs if d.get("status") in ("done", "failed", "cancelled")],
            key=lambda jd: str(jd[1].get("updated_at") or ""),
            reverse=True,
        )[:_COMPLETED_LIMIT]
    else:
        docs = []
        terminal_docs: list[tuple[str, dict]] = []
        # Two independent try/except blocks: a failure on the terminal
        # query (the historical foot-gun — composite-index 400) MUST NOT
        # also blank out queued/running. Likewise a transient permission
        # blip on the active query shouldn't hide what just shipped.
        try:
            from google.cloud import firestore  # noqa: PLC0415
            db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("firestore client init failed", exc_info=True)
            warnings["queued"] = _summarise_firestore_error(exc)
            warnings["running"] = warnings["queued"]
            warnings["completed"] = warnings["queued"]
            db = None  # type: ignore[assignment]

        if db is not None:
            try:
                # No order_by here — `where status in [...]` without sort
                # uses only the auto-created single-field indexes, so
                # this query keeps working even if the composite index
                # for the terminal query hasn't been deployed yet.
                # We sort in Python below.
                stream = (
                    db.collection("jobs")
                    .where("status", "in", ["pending", "rendering", "uploading"])
                    .limit(200)
                    .stream()
                )
                for snap in stream:
                    docs.append((snap.id, snap.to_dict() or {}))
            except Exception as exc:  # noqa: BLE001
                logger.warning("firestore active-queue scan failed", exc_info=True)
                msg = _summarise_firestore_error(exc)
                warnings["queued"] = msg
                warnings["running"] = msg

            try:
                # Separate query for terminal states so the active-queue
                # cap of 200 doesn't starve the completed column on busy
                # days. "cancelled" is included alongside done/failed so
                # a render the operator just stopped stays clickable from
                # the Queue. NOTE: this composite query needs the
                # `jobs(status ASC, updated_at DESC)` index — see
                # firestore.indexes.json. Without it Firestore returns
                # FailedPrecondition; the warning is surfaced to the UI
                # so the empty column is visibly explained.
                term_stream = (
                    db.collection("jobs")
                    .where("status", "in", ["done", "failed", "cancelled"])
                    .order_by("updated_at", direction=firestore.Query.DESCENDING)
                    .limit(_COMPLETED_LIMIT)
                    .stream()
                )
                for snap in term_stream:
                    terminal_docs.append((snap.id, snap.to_dict() or {}))
            except Exception as exc:  # noqa: BLE001
                logger.warning("firestore terminal-queue scan failed", exc_info=True)
                warnings["completed"] = _summarise_firestore_error(exc)

    # Active queue: sort newest-first by updated_at so the UI presents
    # a stable, predictable order across pages. (The Firestore query
    # intentionally omits order_by so it doesn't need a composite index;
    # we sort here instead — cheap on ≤200 rows.)
    docs.sort(key=lambda jd: str(jd[1].get("updated_at") or ""), reverse=True)

    for jid, d in docs:
        s = d.get("status")
        view = _doc_to_view(jid, d).model_dump()
        if s == "pending":
            queued.append(view)
        elif s in ("rendering", "uploading"):
            running.append(view)

    for jid, d in terminal_docs:
        completed.append(_doc_to_view(jid, d).model_dump())

    # Holds: walk every channel/_holds.json on disk (cheap; ≤ 8 files).
    # We also surface `source_critique` so the Queue page's held card can
    # open the actual critique markdown in a dialog without the FE having
    # to reconstruct paths or guess at conventions (cosmosdecoded uses
    # /Users/rohit/evals/<channel>/critiques/<slug>_critique.md, but the
    # historyrecapped early run dropped them at the eval root — the path
    # baked into holds.json by `pipeline.quality.evals.hold_slug` is the only
    # reliable way to find the right file).
    held: list[dict] = []
    project_root = Path(__file__).resolve().parent.parent
    for chan_dir in sorted(project_root.iterdir()):
        if not chan_dir.is_dir():
            continue
        holds_file = chan_dir / "_holds.json"
        if not holds_file.exists():
            continue
        try:
            import json as _json  # noqa: PLC0415
            data = _json.loads(holds_file.read_text())
            if isinstance(data, dict):
                for slug, info in data.items():
                    info = info or {}
                    held.append({
                        "channel": chan_dir.name,
                        "slug": slug,
                        "reason": info.get("reason", ""),
                        # holds.json field is `set_at` (not `held_at`) per
                        # pipeline.quality.evals.hold_slug; surface both keys so
                        # any older FE consumer that reads `held_at` still
                        # gets a value.
                        "set_at": info.get("set_at"),
                        "held_at": info.get("set_at"),
                        "source_critique": info.get("source_critique") or None,
                    })
        except Exception:  # noqa: BLE001
            logger.warning("failed to parse %s", holds_file, exc_info=True)

    return QueueResponse(
        queued=queued,
        running=running,
        completed=completed,
        held=held,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Critique fetch (powers the Queue page's "Held by critic" detail dialog)
# ---------------------------------------------------------------------------


class CritiqueFetchResponse(BaseModel):
    channel: str
    slug: str
    path: str
    exists: bool
    markdown: str | None = None
    # Parsed metadata mirrors pipeline.quality.evals.Critique. Surfaced separately
    # from `markdown` so the FE can render a header summary without re-
    # parsing the whole rubric, while still showing the raw text body for
    # operators who want the full breakdown.
    verdict: str | None = None
    total: int | None = None
    avg: float | None = None
    weakest_param: str | None = None
    weakest_score: int | None = None
    critical_failures: list[tuple[str, int]] = []
    fix_instructions: str | None = None
    gut_check: str | None = None
    date: str | None = None


def _resolve_critique_path(channel: str, slug: str) -> Path | None:
    """Find the markdown critique file for a (channel, slug) pair.

    Order of preference:
    1. The exact `source_critique` path baked into the channel's
       `_holds.json` for this slug — set by `pipeline.quality.evals.hold_slug`,
       handles both convention variants on disk.
    2. The current-convention path under `/Users/rohit/evals/<channel>/critiques/<slug>_critique.md`.
    3. The legacy historyrecapped-style path at the eval root
       (`/Users/rohit/evals/<slug>_critique.md`).

    Returns None if none of the candidates exist.

    **Audit S1.6 — path-traversal defence.** channel + slug are
    validated against ``_safe_name`` before any filesystem touch;
    every joined candidate is checked via ``_safe_join`` to ensure it
    stays under either ``/Users/rohit/evals`` (the canonical critiques
    root) or the project root. Pre-fix, an attacker could send
    ``slug=../../etc/passwd`` (or similar) and have its contents
    streamed back via :func:`get_critique` ``read_text()``.
    """
    _safe_name(channel, label="channel")
    _safe_name(slug, label="slug")
    project_root = Path(__file__).resolve().parent.parent
    evals_root = Path("/Users/rohit/evals")
    holds_file = _safe_join(project_root, channel, "_holds.json")
    if holds_file.exists():
        try:
            import json as _json  # noqa: PLC0415
            data = _json.loads(holds_file.read_text())
            if isinstance(data, dict):
                hint = (data.get(slug) or {}).get("source_critique")
                # coverage: holds-hint resolution path needs a real _holds.json on disk pointing at an existing critique under evals_root; happy-path is covered by live integration runs, not unit tests
                if hint:
                    p = Path(hint).resolve()
                    # Containment: the on-disk hint MUST resolve under
                    # one of the two canonical roots; otherwise drop
                    # the hint and fall through to the candidate list.
                    if (p.is_relative_to(evals_root.resolve())
                            or p.is_relative_to(project_root)) and p.exists():
                        return p
        except Exception:  # noqa: BLE001
            logger.warning("failed to read holds for %s", channel, exc_info=True)

    candidates = [
        _safe_join(evals_root, channel, "critiques", f"{slug}_critique.md"),
        _safe_join(evals_root, f"{slug}_critique.md"),
        _safe_join(project_root, channel, "critiques", f"{slug}.md"),
        _safe_join(project_root, channel, "critiques", f"{slug}_critique.md"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


@router.get("/api/critiques/{channel}/{slug}", response_model=CritiqueFetchResponse)
async def get_critique(channel: str, slug: str) -> CritiqueFetchResponse:
    """Fetch the markdown critique for (channel, slug) plus parsed
    metadata. Powers the Queue page's "Held by critic" detail dialog.

    Returns 200 with `exists=False` and `markdown=None` when the slug is
    held but no critique file is on disk yet (race with the reviewer);
    the FE then surfaces a hint instead of a hard error. Returns 404
    only when the channel directory itself is unknown."""
    # Audit S1.6 — defence in depth; _resolve_critique_path also
    # validates, but failing fast at the route gives a clear 400
    # without touching the filesystem on bad input.
    _safe_name(channel, label="channel")
    _safe_name(slug, label="slug")
    project_root = Path(__file__).resolve().parent.parent
    if not (project_root / channel).is_dir():
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel}")

    cpath = _resolve_critique_path(channel, slug)
    if cpath is None:
        return CritiqueFetchResponse(
            channel=channel, slug=slug,
            path=str(Path("/Users/rohit/evals") / channel / "critiques" / f"{slug}_critique.md"),
            exists=False,
        )

    try:
        from pipeline.quality.evals import parse_critique  # noqa: PLC0415
        crit = parse_critique(cpath)
        markdown = cpath.read_text()
    except Exception as e:  # noqa: BLE001
        logger.warning("parse_critique failed for %s", cpath, exc_info=True)
        raise HTTPException(status_code=500, detail=f"critique parse failed: {e}") from e

    return CritiqueFetchResponse(
        channel=channel, slug=slug, path=str(cpath), exists=True,
        markdown=markdown,
        verdict=crit.verdict, total=crit.total, avg=crit.avg,
        weakest_param=crit.weakest_param, weakest_score=crit.weakest_score,
        critical_failures=list(crit.critical_failures),
        fix_instructions=crit.fix_instructions, gut_check=crit.gut_check,
        date=crit.date,
    )


# ---------------------------------------------------------------------------
# Hold resolution (powers the "Resolve" tab on the held-critique dialog)
# ---------------------------------------------------------------------------


class ResolveHoldRequest(BaseModel):
    """One of three workflows for clearing a critic-held slug.

    - `operator_verdict` : operator overrides the AI critic with their own
      verdict (SHIP / FIX / BLOCK) + free-form notes. SHIP clears the
      hold so the cron can upload; FIX/BLOCK rewrites the hold reason.
      An audit-trail markdown file is written under the channel's
      critique dir.
    - `request_recritique` : operator has fixed the underlying issue
      (re-render, regen audio, etc.) externally and wants a fresh AI
      critique. The existing critique file is archived (so /judge-video
      treats the slug as fresh) and the hold is cleared so a new render
      can flow through. The CLI command to run is returned for the FE
      to surface — the website doesn't shell out to the judge skill.
    - `clear` : pure unhold, no audit file written. Use when the
      operator just wants to dismiss the hold (e.g. critic was wrong
      about a non-issue).
    """
    action: str  # "operator_verdict" | "request_recritique" | "clear"
    verdict: str | None = None  # required when action="operator_verdict"
    notes: str = ""


class ResolveHoldResponse(BaseModel):
    channel: str
    slug: str
    action: str
    hold_cleared: bool
    new_hold_reason: str | None = None
    operator_critique_path: str | None = None
    archived_critique_path: str | None = None
    next_step_hint: str | None = None


_OPERATOR_VERDICT_TEMPLATE = """# Operator critique — {slug}

**Date:** {date}
**Verdict:** **{verdict}**
**Source:** operator override (Studio · Queue → Resolve)

## Notes

{notes}

## Original AI critique

Path: `{original_path}`

This file overrides the AI critic for upload-gating purposes. The
original markdown is preserved at the path above for audit; this file
is what `pipeline.evals` should treat as authoritative for the hold
state.
"""


@router.post(
    "/api/critiques/{channel}/{slug}/resolve",
    response_model=ResolveHoldResponse,
)
async def resolve_held_slug(
    channel: str, slug: str, req: ResolveHoldRequest,
) -> ResolveHoldResponse:
    """Resolve a critic-held slug via one of three workflows. See
    `ResolveHoldRequest` for the action semantics."""
    from pipeline.quality import evals as evals_mod  # noqa: PLC0415
    import datetime as _dt  # noqa: PLC0415

    # Audit S1.5 — validate before any FS touch (channel + slug come
    # from URL params; without this an attacker writes
    # ``../../etc/.../foo.md`` anywhere on the host).
    _safe_name(channel, label="channel")
    _safe_name(slug, label="slug")

    project_root = Path(__file__).resolve().parent.parent
    if not (project_root / channel).is_dir():
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel}")

    if req.action not in ("operator_verdict", "request_recritique", "clear"):
        raise HTTPException(
            status_code=422,
            detail=f"unknown action: {req.action!r}; expected operator_verdict | request_recritique | clear",
        )

    original_critique = _resolve_critique_path(channel, slug)
    operator_path: Path | None = None
    archived_path: Path | None = None
    hold_cleared = False
    new_reason: str | None = None
    next_step: str | None = None

    if req.action == "operator_verdict":
        verdict = (req.verdict or "").upper()
        if verdict not in ("SHIP", "FIX", "BLOCK"):
            raise HTTPException(
                status_code=422,
                detail="operator_verdict requires verdict in {SHIP, FIX, BLOCK}",
            )
        # Critique dir convention is /Users/rohit/evals/<channel>/critiques/.
        # Drop a side-by-side `_operator_<ts>.md` so the audit trail
        # survives every override (no in-place rewrite of the AI critique).
        # Audit S1.5 — channel + slug must be safe (already validated
        # at top of this handler), and the joined operator_path MUST
        # resolve under the evals root before we write_text() anything.
        # coverage: operator-verdict happy-path needs an existing channel dir under control/; the parent route's project_root check (pre-existing bug, not in S1.5 scope) blocks integration tests; safe-name + safe-join paths covered by direct unit tests.
        critiques_dir = _safe_join(
            Path("/Users/rohit/evals"), channel, "critiques",
        )
        # coverage: full operator_verdict body needs an integration fixture; see above for scope justification on this audit fix
        critiques_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")  # coverage: integration-only path; full operator_verdict body needs a live evals workspace
        operator_path = _safe_join(critiques_dir, f"{slug}_operator_{ts}.md")  # coverage: same as above; integration-only operator_verdict path
        operator_path.write_text(_OPERATOR_VERDICT_TEMPLATE.format(  # coverage: write_text + template format are integration-only operator_verdict paths
            slug=slug,
            date=_dt.date.today().isoformat(),
            verdict=verdict,
            notes=req.notes.strip() or "(no notes)",
            original_path=str(original_critique) if original_critique else "(none on disk)",
        ))

        if verdict == "SHIP":
            hold_cleared = evals_mod.clear_hold(channel, slug)
            next_step = (
                "Hold cleared. The next cron drain will pick up this slug. "
                "Re-render or republish manually if you want it out sooner."
            )
        else:
            # Keep / refresh the hold but with the operator's reason.
            new_reason = f"verdict={verdict} (operator); notes={req.notes.strip() or '—'}"
            evals_mod.set_hold(
                channel, slug, reason=new_reason,
                source_critique=str(operator_path),
            )
            next_step = (
                f"Hold updated to operator verdict={verdict}. "
                "Address the notes and run Resolve again when ready."
            )

        try:
            evals_mod.update_status_authoring(
                channel, slug,
                last_fix_attempted="operator-override",
                last_fix_result=f"{verdict.lower()}-by-operator",
            )
        except Exception:  # noqa: BLE001
            # STATUS.md is non-critical for the hold/upload mechanic;
            # log but don't fail the API call if it can't be written
            # (eg. project not initialised).
            logger.warning("update_status_authoring failed for %s/%s", channel, slug, exc_info=True)

    elif req.action == "request_recritique":
        # Move the existing critique aside so /judge-video has nothing
        # to coalesce against on its next run, and so /ingest-critiques
        # treats the new file as a fresh first_critique. Suffix with a
        # timestamp instead of overwriting any prior _v1, _v2, etc.
        if original_critique and original_critique.exists():
            ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            archived_path = original_critique.with_name(
                f"{original_critique.stem}_archived_{ts}{original_critique.suffix}"
            )
            original_critique.rename(archived_path)
        # Clearing the hold is intentional — without it the cron skips
        # the slug and a fresh render+critique cycle can't be observed
        # end-to-end.
        hold_cleared = evals_mod.clear_hold(channel, slug)
        mp4_hint = project_root / channel / "shorts" / f"{slug}.mp4"
        next_step = (
            f"Run `claude /judge-video {mp4_hint}` (or re-render the slug "
            "first, then judge). The fresh critique will land in "
            f"/Users/rohit/evals/{channel}/critiques/."
        )
        try:
            evals_mod.update_status_authoring(
                channel, slug,
                last_fix_attempted="request-recritique",
                last_fix_result="awaiting-fresh-critique",
            )
        except Exception:  # noqa: BLE001
            logger.warning("update_status_authoring failed for %s/%s", channel, slug, exc_info=True)

    elif req.action == "clear":
        hold_cleared = evals_mod.clear_hold(channel, slug)
        next_step = "Hold cleared with no audit file. The cron uploader will pick this slug up on its next drain."
        try:
            evals_mod.update_status_authoring(
                channel, slug,
                last_fix_attempted="clear-hold",
                last_fix_result="dismissed-by-operator",
            )
        except Exception:  # noqa: BLE001
            logger.warning("update_status_authoring failed for %s/%s", channel, slug, exc_info=True)

    return ResolveHoldResponse(
        channel=channel, slug=slug, action=req.action,
        hold_cleared=hold_cleared,
        new_hold_reason=new_reason,
        operator_critique_path=str(operator_path) if operator_path else None,
        archived_critique_path=str(archived_path) if archived_path else None,
        next_step_hint=next_step,
    )


@router.get("/api/health")
async def health() -> dict:
    """Operator health: deployment + agent presence + spend usage."""
    from control.routes.agent_routes import get_last_seen  # noqa: PLC0415
    from control.core.sim_worker import status as sim_status  # noqa: PLC0415
    from control.core import cloud_run  # noqa: PLC0415
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
        "render_backend": cloud_run.render_backend(),
        "cloudrun": cloud_run.status(),
        "sim_worker": sim_status(),
    }
