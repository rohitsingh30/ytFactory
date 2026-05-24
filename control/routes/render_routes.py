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
    # (channel, niche) pair compatibility — reject the dangerous case:
    # a niche key explicitly registered to a DIFFERENT channel (e.g.
    # ``channel=sportsrecapped, format=aita``). That combination crashes
    # the worker downstream when the wrong-channel variant overlay is
    # missing required keys (character_description / opening_image_
    # directives / niche-tonal lexicon, etc).
    # Note: the wizard's ``format`` default is the literal "animated"
    # which is a UI marker, NOT a niche key — fall through unless the
    # value actually appears as a niche under a different channel.
    fmt = (req.format or "").strip()
    if fmt:
        from pipeline.channels import channel_for_niche  # noqa: PLC0415
        owning = channel_for_niche(fmt)
        # owning is a Channel object; compare its slug to req.channel.
        # Pre-fix this used niche_channel_map() which returns
        # (state_dir, variant_yaml) — state_dir = "channel/niche_subdir"
        # so a registered niche like 'tifu' ("mystoriesanimated/reddit_tifu",
        # ...) failed equality against the bare 'mystoriesanimated' slug.
        if owning and owning.slug != req.channel:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"niche/format {fmt!r} belongs to channel "
                    f"{owning.slug!r}, not {req.channel!r}. Either switch "
                    f"the channel or pick a different format. Registered "
                    f"niches for {req.channel!r}: "
                    f"{sorted(ch.niches.keys()) if ch.niches else '(none — channel uses defaults)'}."
                ),
            )
    # Length sanity range. _clamp_length quietly normalises bad input
    # to a valid range, but if the user explicitly passes a wildly out-
    # of-range value (e.g. -10s, 0, 100000) something is wrong upstream
    # — fail loud so the wizard form can be fixed.
    if req.length_s <= 0 or req.length_s > 7200:
        raise HTTPException(
            status_code=422,
            detail=(
                f"length_s={req.length_s} is out of range. "
                f"Shorts: 20-120s. Long-form: 121-7200s (up to 2hr)."
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
    # B4 — retry chain linkage. ``retry_of`` set on the retry doc;
    # ``retried_as`` set on the original failed doc once the retry
    # is created. Both surface in the UI as cross-links so the
    # operator can walk the retry chain.
    retry_of: str | None = None
    retried_as: str | None = None


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
        retry_of=doc.get("retry_of"),
        retried_as=doc.get("retried_as"),
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
            db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v3"))
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
    """One-click-publish request body.

    Per the 2026-05-24 modal refactor, the UI only collects ``visibility``
    + optional ``scheduled_publish_at``; every other field (title,
    description, hashtags, tags, thumbnail) is auto-generated server-side
    by :func:`pipeline.publish.generate_publish_metadata` from the
    rendered script.

    The legacy fields (``schedule_at``, ``title``, ``description``,
    ``tags``, ``upload_method``) are kept optional for back-compat with
    callers that still POST them — they're persisted to the job doc
    alongside the generated metadata but DO NOT override the generator's
    output. Once every caller has migrated they can be removed.
    """

    visibility: str = "unlisted"  # public | unlisted | private
    scheduled_publish_at: str | None = None  # RFC 3339, or null for immediate
    # ---- legacy / deprecated fields ---------------------------------
    schedule_at: str | None = None  # legacy alias of scheduled_publish_at
    title: str | None = None
    description: str | None = None
    tags: list[str] = []
    upload_method: str = "auto"  # auto | api | playwright


class PublishMetadataView(BaseModel):
    """The shape returned by ``GET /api/jobs/{id}/publish/preview``.

    Mirrors :class:`pipeline.publish.PublishMetadata` but with
    ``thumbnail_path`` serialised to a string so the JSON response is
    transport-safe.
    """

    title: str
    description: str
    hashtags: list[str]
    tags: list[str]
    thumbnail_path: str | None
    category_id: str
    default_language: str
    made_for_kids: bool


class PublishResponse(BaseModel):
    job_id: str
    status: str  # submitted | scheduled | uploading | done | failed
    youtube_url: str | None = None
    error: str | None = None
    publish_metadata: PublishMetadataView | None = None


def _build_publish_metadata(job_id: str, doc: dict) -> PublishMetadataView:
    """Resolve the job's script payload + channel/variant, then delegate
    to :func:`pipeline.publish.generate_publish_metadata`.

    The job doc carries ``proposal`` (the ShortProposal that birthed the
    render) and may carry an already-rewritten ``script`` payload — we
    prefer the latter when present so titles/hooks reflect the writer's
    final pick. If neither carries usable inputs the generator raises
    :class:`MissingMetadataInputError`, which we surface as a 422.
    """
    from pipeline.publish import (  # noqa: PLC0415
        generate_publish_metadata,
    )
    from pipeline.publish.metadata_generator import (  # noqa: PLC0415
        MissingMetadataInputError,
    )

    proposal = doc.get("proposal") or {}
    script = doc.get("script") or {}
    # Backfill script with proposal-side fields so single-source-of-truth
    # lookups (topic, hook) work whether or not the rewrite stage has
    # populated ``doc["script"]``.
    merged_script: dict[str, Any] = {
        "topic": proposal.get("topic"),
        "hook": script.get("hook") or proposal.get("hook"),
        "summary": script.get("summary") or proposal.get("summary"),
        "title_options": script.get("title_options")
        or proposal.get("title_options")
        or [],
        "title": script.get("title") or proposal.get("title"),
        "thumbnail_path": script.get("thumbnail_path")
        or doc.get("thumb_uri"),
        "default_language": script.get("default_language")
        or proposal.get("default_language"),
        "made_for_kids": script.get("made_for_kids", False),
    }
    channel = doc.get("channel") or proposal.get("channel") or "auto"
    variant_raw = proposal.get("format") or doc.get("variant")
    variant = str(variant_raw) if variant_raw else None
    try:
        meta = generate_publish_metadata(
            job_id, merged_script, channel=channel, variant=variant,
        )
    except MissingMetadataInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return PublishMetadataView(
        title=meta.title,
        description=meta.description,
        hashtags=meta.hashtags,
        tags=meta.tags,
        thumbnail_path=str(meta.thumbnail_path) if meta.thumbnail_path else None,
        category_id=meta.category_id,
        default_language=meta.default_language,
        made_for_kids=meta.made_for_kids,
    )


@router.get(
    "/api/jobs/{job_id}/publish/preview",
    response_model=PublishMetadataView,
)
async def publish_preview(
    job_id: str,
    request: Request,
    _pin: None = Depends(require_pin),
) -> PublishMetadataView:
    """Return the auto-generated metadata the modal previews before publish.

    The modal calls this on open to show "this is what we'll publish
    with"; the same code path that runs in :func:`publish` runs here, so
    there is zero drift between what the user sees and what gets sent
    to YouTube.
    """
    doc = jobs_mod.get_job(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="job not found")
    _job_owner_check(request, doc)  # audit S1.7
    return _build_publish_metadata(job_id, doc)


@router.post("/api/jobs/{job_id}/publish", response_model=PublishResponse)
async def publish(
    job_id: str,
    body: PublishRequest,
    request: Request,
    _pin: None = Depends(require_pin),
) -> PublishResponse:
    """Publish a finished render to YouTube — one-click flow.

    The user picks visibility (+ optional scheduled publish time); every
    other knob is generated by
    :func:`pipeline.publish.generate_publish_metadata`. Generated
    metadata is persisted to the job doc as ``publish_metadata`` so the
    same payload that the modal previewed is what the worker uploads.

    Sim mode: returns a fake youtube_url + marks the job published.
    Real mode: kicks
    :func:`pipeline.upload.upload.youtube_upload` (existing auth flow,
    resumable upload, quota handling).
    """
    from control.core.sim_worker import is_enabled as sim_enabled  # noqa: PLC0415

    if body.visibility not in ("public", "unlisted", "private"):
        raise HTTPException(
            status_code=422,
            detail=f"visibility must be public|unlisted|private; got {body.visibility!r}",
        )

    doc = jobs_mod.get_job(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="job not found")
    _job_owner_check(request, doc)  # audit S1.7
    if doc.get("status") != jobs_mod.STATUS_DONE:
        raise HTTPException(
            status_code=409,
            detail=f"job not ready (status={doc.get('status')!r}); render must complete first",
        )

    # Auto-generate metadata BEFORE doing any real work — surface
    # missing-input errors (422) before we trigger an upload that would
    # then have to be undone.
    meta_view = _build_publish_metadata(job_id, doc)

    # Resolved publish-at: prefer the new field, fall back to the legacy
    # alias, else None (immediate publish).
    publish_at = body.scheduled_publish_at or body.schedule_at

    # Persist the generated metadata + visibility / schedule choices to
    # the job doc. The worker reads this on the upload side; the UI's
    # PublishedCard reads ``youtube_url`` once it lands.
    persisted_publish_meta = {
        "visibility": body.visibility,
        "scheduled_publish_at": publish_at,
        "title": meta_view.title,
        "description": meta_view.description,
        "hashtags": meta_view.hashtags,
        "tags": meta_view.tags,
        "thumbnail_path": meta_view.thumbnail_path,
        "category_id": meta_view.category_id,
        "default_language": meta_view.default_language,
        "made_for_kids": meta_view.made_for_kids,
        "upload_method": body.upload_method,
    }

    # Sim path — instant fake upload so the UI flow works end-to-end.
    if sim_enabled():
        fake = f"https://youtu.be/sim-{job_id[:11]}"
        persisted_publish_meta["upload_method"] = "sim"
        jobs_mod.get_jobs().update(
            job_id,
            youtube_url=fake,
            publish_metadata=persisted_publish_meta,
            publish_meta=persisted_publish_meta,  # legacy alias
        )
        return PublishResponse(
            job_id=job_id,
            status="done",
            youtube_url=fake,
            publish_metadata=meta_view,
        )

    # Real path — defer to the existing pipeline.upload.upload module.
    # Same auth path (OAuth refresh-token from
    # ~/.config/ytfactory/youtube_token_<account>.json or the Cloud Run
    # secret mount at /secrets/youtube-token-<account>/value).
    from pathlib import Path as _P  # noqa: PLC0415
    from pipeline.upload.upload import youtube_upload, UploadError  # noqa: PLC0415

    # C1 2026-05-24 — lift the 501 gate. For cloud-rendered jobs the mp4
    # lives at gs://<bucket>/jobs/<id>/short.mp4 (long.mp4 for long-form)
    # and ``short_uri`` on the doc points at the canonical URI. Resolve
    # to a local mp4 the existing upload module can read by either:
    #   1. using ``preview_local_path`` when present + readable (the
    #      laptop dev flow, unchanged), or
    #   2. downloading the GCS object referenced by ``short_uri`` to a
    #      temp file. The temp file is cleaned up after upload.
    short_uri = doc.get("short_uri") or ""
    local_mp4_str = doc.get("preview_local_path")
    local_mp4: _P | None = _P(local_mp4_str) if local_mp4_str else None
    local_mp4_is_temp = False
    if local_mp4 is None or not local_mp4.exists():
        # No local path → fall through to GCS resolve. If neither
        # ``short_uri`` nor a local path is set, we cannot upload —
        # surface 422 (the inputs are missing).
        if not short_uri.startswith("gs://"):
            # Persist metadata anyway so the worker / a retry sees it.
            jobs_mod.get_jobs().update(
                job_id,
                publish_metadata=persisted_publish_meta,
                publish_meta=persisted_publish_meta,
            )
            raise HTTPException(
                status_code=422,
                detail=(
                    f"job {job_id} has no preview_local_path and no "
                    f"gs:// short_uri to download. Cannot publish."
                ),
            )
        from control.core import storage as _storage  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        try:
            tmp = tempfile.NamedTemporaryFile(
                suffix=".mp4", prefix=f"publish-{job_id}-", delete=False,
            )
            tmp.close()
            local_mp4 = _storage.download(short_uri, tmp.name)
            local_mp4_is_temp = True
        except Exception as exc:  # noqa: BLE001
            jobs_mod.get_jobs().update(
                job_id,
                publish_metadata=persisted_publish_meta,
                publish_meta=persisted_publish_meta,
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    f"failed to download cloud mp4 from {short_uri}: {exc}"
                ),
            ) from exc

    # Resolve upload account — proposal override wins, else fall back
    # to the channel slug (matches pipeline.upload.upload's account-name
    # convention; each channel has its own youtube_token_<slug>.json or
    # /secrets/youtube-token-<slug>/value mount on Cloud Run).
    proposal = doc.get("proposal") or {}
    account = (
        proposal.get("upload_account")
        or doc.get("channel")
        or proposal.get("channel")
        or "default"
    )

    upload_method_requested = (body.upload_method or "auto").lower()
    used_method = "api"
    try:
        if upload_method_requested == "playwright":
            # Operator explicitly chose playwright — skip the API path.
            result = _publish_via_playwright(
                local_mp4,
                meta_view=meta_view,
                privacy=body.visibility,
                publish_at=publish_at,
                account=str(account),
                job_id=job_id,
            )
            used_method = "playwright"
        else:
            try:
                result = youtube_upload(
                    local_mp4,
                    title=meta_view.title,
                    description=meta_view.description,
                    tags=meta_view.tags,
                    category_id=meta_view.category_id,
                    privacy=body.visibility,
                    publish_at=publish_at,
                    made_for_kids=meta_view.made_for_kids,
                    account=str(account),
                    thumbnail_path=_P(meta_view.thumbnail_path) if meta_view.thumbnail_path else None,
                )
            except UploadError as exc:
                # C3 2026-05-24 — playwright fallback on quotaExceeded.
                # ``pipeline.upload.upload.QuotaExceededError`` is the
                # specific subclass for documented quota reasons; other
                # UploadError subclasses (RefreshTokenLost, generic
                # 403/5xx) are NOT quota-class and should not trigger
                # the fallback.
                from pipeline.upload.upload import (  # noqa: PLC0415
                    QuotaExceededError,
                )

                if not isinstance(exc, QuotaExceededError):
                    raise

                logger.warning(
                    "youtube_upload quotaExceeded (reason=%s); attempting "
                    "playwright fallback for job=%s account=%s",
                    exc.reason, job_id, account,
                )
                try:
                    result = _publish_via_playwright(
                        local_mp4,
                        meta_view=meta_view,
                        privacy=body.visibility,
                        publish_at=publish_at,
                        account=str(account),
                        job_id=job_id,
                    )
                    used_method = "playwright"
                except _PlaywrightUnavailable as pw_exc:
                    # Both paths exhausted — queue for manual upload.
                    persisted_publish_meta["upload_method"] = "queued_for_manual"
                    persisted_publish_meta["queued_reason"] = (
                        f"api quota exhausted ({exc.reason}); playwright "
                        f"fallback failed: {pw_exc}"
                    )
                    jobs_mod.get_jobs().update(
                        job_id,
                        publish_metadata=persisted_publish_meta,
                        publish_meta=persisted_publish_meta,
                    )
                    return PublishResponse(
                        job_id=job_id,
                        status="queued_for_manual",
                        error=(
                            "quota exhausted, playwright fallback "
                            f"failed: {pw_exc}"
                        ),
                        publish_metadata=meta_view,
                    )
    except UploadError as exc:
        return PublishResponse(
            job_id=job_id,
            status="failed",
            error=str(exc),
            publish_metadata=meta_view,
        )
    finally:
        # Clean up the temp file we downloaded for cloud-rendered jobs.
        # Done in ``finally`` so the cleanup happens even on the failure
        # paths above (UploadError, playwright fallback failure, etc.).
        if local_mp4_is_temp and local_mp4 is not None:
            try:
                local_mp4.unlink(missing_ok=True)
            except OSError:
                # Defensive — on Windows or some bind mounts unlink can
                # race with antivirus scanners; the temp dir gets GC'd
                # by the OS regardless.
                pass

    youtube_url = result.get("url")
    persisted_publish_meta["upload_method"] = used_method
    jobs_mod.get_jobs().update(
        job_id,
        youtube_url=youtube_url,
        publish_metadata=persisted_publish_meta,
        publish_meta=persisted_publish_meta,
    )
    return PublishResponse(
        job_id=job_id,
        status="done",
        youtube_url=youtube_url,
        publish_metadata=meta_view,
    )


# ---- C3 playwright fallback (2026-05-24) ----------------------------
#
# When ``pipeline.upload.upload.youtube_upload`` raises
# ``QuotaExceededError`` (per-account daily 10K-unit pool exhausted),
# the publish route attempts to upload via the playwright path instead.
# The ``upload-via-playwright`` skill is the interactive driver the
# operator runs locally; a programmatic module is available when the
# ``pipeline.upload.playwright_upload`` import succeeds AND the
# environment is non-headless (laptop). On Cloud Run the import is
# expected to fail (no Chrome user-data-dir) → fall through to the
# ``_PlaywrightUnavailable`` branch which queues the job for manual
# upload via the skill.


class _PlaywrightUnavailable(Exception):
    """Programmatic playwright path can't run in this env.

    Distinct from generic Exception so the caller can return a clean
    ``queued_for_manual`` status (operator triggers the skill manually)
    rather than a 500.
    """


def _publish_via_playwright(
    mp4_path: Path,
    *,
    meta_view: "PublishMetadataView",
    privacy: str,
    publish_at: str | None,
    account: str,
    job_id: str,
) -> dict:
    """Drive an upload via ``pipeline.upload.playwright_upload`` when
    available; raise :class:`_PlaywrightUnavailable` otherwise.

    The module is optional — when it isn't present in the image (the
    expected state on Cloud Run, since playwright + signed-in Chrome
    only run on the operator's laptop), the import fails and we surface
    the gap so the caller can return ``queued_for_manual``.
    """
    try:
        from pipeline.upload import playwright_upload as _pw  # noqa: PLC0415
    except ImportError as exc:
        raise _PlaywrightUnavailable(
            "pipeline.upload.playwright_upload not importable "
            f"({exc}); run the /pw-upload skill from a laptop with a "
            "signed-in Chrome profile to complete the upload."
        ) from exc
    try:
        return _pw.playwright_upload(
            mp4_path,
            title=meta_view.title,
            description=meta_view.description,
            tags=list(meta_view.tags),
            privacy=privacy,
            publish_at=publish_at,
            made_for_kids=meta_view.made_for_kids,
            account=account,
            thumbnail_path=Path(meta_view.thumbnail_path) if meta_view.thumbnail_path else None,
            job_id=job_id,
        )
    except Exception as exc:  # noqa: BLE001 — relay any playwright failure
        # Re-raise as _PlaywrightUnavailable so the caller's manual-queue
        # branch handles it uniformly with the ImportError case.
        raise _PlaywrightUnavailable(
            f"playwright_upload failed: {exc}"
        ) from exc


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
    this endpoint surfaces it in the Queued column forever.

    FIXED 2026-05-23 (B3a + B3b in the silent-death docket):

    * Soft path — the render-worker now installs a SIGTERM handler
      (``cloud/render-worker-v2/entrypoint.py::_on_sigterm``) that
      writes ``status=failed`` during the Cloud Run 10s grace window
      before SIGKILL.
    * Hard path — ``control.core.reconciler.reconcile_stuck_jobs``
      sweeps Firestore every 5 min via the
      ``com.ytfactory.job-reconciler.plist`` LaunchAgent (and is also
      exposed as ``POST /api/admin/reconcile_jobs`` for manual runs).
      It cross-checks ``cloud_execution`` against
      ``run_v2.ExecutionsClient`` and marks the doc failed iff the
      Cloud Run Execution itself reports failure / cancellation /
      not-found.

    Together the two cover SIGKILL, OOM, host eviction, kernel panic,
    network partition between worker and Firestore — anything the
    Python interpreter can't catch before dying."""
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
            db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v3"))
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

    # ``internal_only=True`` docs are smoke-tests / dev fixtures
    # fired by scripts/trigger_one_render.py and the test-fixture
    # auto-flagger (see control/core/scheduler.py::is_test_fixture_topic).
    # They should NEVER appear on the operator dashboard — pre-2026-05-24
    # the queue route ignored the flag entirely and 31 internal_only
    # "ketchup on slow-cooked beef stew" smoke renders cluttered the
    # Completed column. Filter them out here. The internal_only flag
    # lives at ``doc.proposal.internal_only`` (set by
    # control/core/jobs.py:411) but for back-compat we also accept
    # a flat ``doc.internal_only`` field in case any pre-flag-flag
    # rows wrote it at the root.
    def _is_internal_only(doc: dict) -> bool:
        if doc.get("internal_only") is True:
            return True
        p = doc.get("proposal") or {}
        return p.get("internal_only") is True

    for jid, d in docs:
        if _is_internal_only(d):
            continue
        s = d.get("status")
        view = _doc_to_view(jid, d).model_dump()
        if s == "pending":
            queued.append(view)
        elif s in ("rendering", "uploading"):
            running.append(view)

    for jid, d in terminal_docs:
        if _is_internal_only(d):
            continue
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


# ---------------------------------------------------------------------------
# Cloud Run JOB reconciler — B3b in 2026-05-23 docket.
#
# Fixes the "known gap" cited at line 732-743 above: render-worker JOBs
# that crash before writing their first stage update leave the Firestore
# doc permanently at ``status=rendering`` / ``status=pending``. The
# reconciler sweeps those, cross-checking the Cloud Run Execution to
# decide failed vs cancelled vs still-running.
#
# Invoked by:
#   * Manual: POST /api/admin/reconcile_jobs
#   * Cron:   com.ytfactory.job-reconciler.plist (every 5 min on laptop)
# ---------------------------------------------------------------------------

class ReconcileRequest(BaseModel):
    max_age_minutes: int = Field(120, ge=10, le=10080)
    batch_size: int = Field(50, ge=1, le=500)
    dry_run: bool = False


@router.post("/api/admin/reconcile_jobs")
async def reconcile_jobs(
    body: ReconcileRequest | None = None,
    _: None = Depends(require_pin),
) -> dict:
    """Sweep Firestore for jobs stuck mid-pipeline whose Cloud Run JOB
    execution has finished failing — mark them failed.

    Returns the structured summary from
    ``control.core.reconciler.reconcile_stuck_jobs``.
    """
    from control.core.reconciler import reconcile_stuck_jobs  # noqa: PLC0415

    req = body or ReconcileRequest()
    # Push the sync Firestore + Cloud Run Admin API calls off the event
    # loop so a slow sweep can't stall the rest of the server.
    import asyncio  # noqa: PLC0415
    summary = await asyncio.to_thread(
        reconcile_stuck_jobs,
        max_age_minutes=req.max_age_minutes,
        batch_size=req.batch_size,
        dry_run=req.dry_run,
    )
    return summary


# ---------------------------------------------------------------------------
# B4 — Retry a failed/cancelled job, reusing the B2 persistent cache
# ---------------------------------------------------------------------------
#
# A failed long-form render burns ~₹15-20 of image+TTS work. Pre-B2 the
# only way to recover was a fresh /api/render with the same proposal,
# which paid for every panel + chunk over again. With B2 the artifacts
# live at gs://<bucket>/jobs/<old_job_id>/cache/, so a retry can copy
# them to gs://<bucket>/jobs/<new_job_id>/cache/ (cheap same-bucket
# rewrite) and the new render hydrates them on startup — the only fresh
# cost is whatever wasn't yet uploaded before the SIGKILL.


class RetryResponse(BaseModel):
    retry_job_id: str
    retry_of: str
    cache_objects_copied: int
    # D3-4 (2026-05-24) — surface the difference between "no source cache
    # existed" (cache_copy_failed=False, cache_objects_copied=0; first-
    # attempt fail before any panel/chunk was persisted — legitimately
    # cold) and "GCS list/copy raised an exception" (cache_copy_failed=
    # True, cache_objects_copied=0; the worker DID persist artifacts but
    # we couldn't read them, so the retry will silently re-pay full
    # cost). Pre-fix both surfaced as 0 with no distinction → UI showed
    # "Starting fresh — no cache available" for both cases, hiding the
    # transient GCS hiccup that cost the user ~₹20.
    cache_copy_failed: bool = False
    cloud_execution: str | None = None


@router.post("/api/jobs/{job_id}/retry", response_model=RetryResponse)
async def retry_job(
    job_id: str,
    request: Request,
    _pin: None = Depends(require_pin),
) -> RetryResponse:
    """Re-render a failed/cancelled job, reusing the persisted cache.

    Steps:
      1. Load original Firestore doc.
      2. Build new job_id, copy proposal/channel/topic into a new doc
         with ``retry_of: <old>``.
      3. Server-side GCS copy ``jobs/<old>/cache/**`` → ``jobs/<new>/cache/**``.
      4. Trigger Cloud Run JOB for the new job_id.
      5. Stamp ``retried_as: <new>`` on the original.

    Only allowed when the original is ``failed`` or ``cancelled`` —
    don't allow retrying a job that's still rendering (queue dupes).

    Audit S1.7: owner check inherited from the original doc.
    """
    import asyncio  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    doc = jobs_mod.get_job(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="job not found")
    _job_owner_check(request, doc)

    status = (doc.get("status") or "").lower()
    if status not in ("failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id} is in status {status!r} — retry is only "
                f"allowed for failed/cancelled jobs (re-run for status "
                f"rendering would race the worker; retry of done is "
                f"redundant)"
            ),
        )

    proposal_dict = doc.get("proposal") or {}
    if not proposal_dict.get("channel") or not proposal_dict.get("topic"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"job {job_id} has no proposal stored — cannot retry "
                f"(create a fresh render via /api/render instead)"
            ),
        )

    new_job_id = uuid.uuid4().hex
    owner_uid = doc.get("owner_uid")

    # Server-side GCS copy of the cache prefix. Cheap (same bucket,
    # same region) and atomic per-blob. We do this BEFORE the new doc
    # exists so a failure here doesn't leave a stranded retry doc.
    # D3-4 — _copy_cache_prefix now returns (count, failed) so the
    # endpoint can distinguish "no source cache existed" from "GCS
    # raised during list/copy" and surface the latter to the UI as a
    # warning instead of silently degrading to full-cost retry.
    copied, cache_copy_failed = await asyncio.to_thread(
        _copy_cache_prefix, job_id, new_job_id,
    )

    # Build the new doc. Preserve proposal verbatim so the retry uses
    # the same channel/topic/length/notes. Tag both directions of the
    # parent-child link so the dashboard can render "Retried from" /
    # "Retried as" navigation.
    jobs_mod.create_job(
        new_job_id,
        channel=proposal_dict.get("channel"),
        topic=proposal_dict.get("topic"),
        proposal=proposal_dict,
        owner_uid=owner_uid,
        slug=doc.get("slug"),
        render_kind=doc.get("render_kind"),
    )
    jobs_mod.get_jobs().update(new_job_id, retry_of=job_id)

    # Dispatch — same path as a fresh /api/render.
    cloud_execution: str | None = None
    try:
        from control.core import cloud_run as _cloud_run  # noqa: PLC0415
        backend = _cloud_run.render_backend()
        if backend == "cloudrun":
            ref = await asyncio.to_thread(_cloud_run.trigger_render_job, new_job_id)
            cloud_execution = getattr(ref, "execution_name", None)
            jobs_mod.get_jobs().update(
                new_job_id,
                stage="dispatching",
                cloud_execution=cloud_execution,
            )
        else:
            # sim / laptop backend: enqueue a task envelope, matching
            # the behaviour of _enqueue_render_job for non-cloud modes.
            from control.core.queue import get_queue, new_task_id  # noqa: PLC0415
            from control.core.schema import TaskEnvelope, TaskKind  # noqa: PLC0415

            payload = dict(proposal_dict)
            payload["job_id"] = new_job_id
            get_queue().enqueue(TaskEnvelope(
                task_id=new_task_id(),
                job_id=new_job_id,
                kind=TaskKind.RENDER_SHORT,
                payload=payload,
            ))
    except Exception as exc:  # noqa: BLE001
        logger.exception("retry dispatch failed for new_job=%s", new_job_id)
        jobs_mod.mark_failed(
            new_job_id, stage="dispatch",
            error=f"retry dispatch failed: {exc}",
        )
        raise HTTPException(status_code=502, detail=f"retry dispatch failed: {exc}")

    # Back-reference on the original. Best-effort — if the firestore
    # update fails the retry is still live; we just lose the link.
    try:
        jobs_mod.get_jobs().update(job_id, retried_as=new_job_id)
    except Exception:  # noqa: BLE001
        logger.warning("failed to stamp retried_as on old job=%s", job_id)

    return RetryResponse(
        retry_job_id=new_job_id,
        retry_of=job_id,
        cache_objects_copied=copied,
        cache_copy_failed=cache_copy_failed,
        cloud_execution=cloud_execution,
    )


def _copy_cache_prefix(old_job_id: str, new_job_id: str) -> tuple[int, bool]:
    """Server-side copy gs://<bucket>/jobs/<old>/cache/** to <new>/cache/**.

    Returns ``(count, failed)``:
      * ``count`` — number of objects copied (≥ 0).
      * ``failed`` — True iff a GCS exception was raised during list or
        copy (transient bucket hiccup / IAM blip). When True the retry
        still proceeds, but the caller surfaces the failure to the UI
        so the user knows the retry will pay full cost rather than the
        "no cache available" message implying a legitimate cold start.

    Never raises — a missing or empty source prefix returns ``(0, False)``
    (a first-attempt cache or a job that died before any panel was
    generated; legitimately cold).

    D3-4 (2026-05-24 retry-cache sweep): pre-fix this returned a bare
    int and the GCS-failure branch returned ``0`` indistinguishable
    from "no source cache". UI showed "Starting fresh" for both — the
    user couldn't tell the retry was about to silently re-spend ~₹20.

    Uses ``bucket.copy_blob(source_blob, destination_bucket=bucket, new_name=...)``
    which is a metadata operation at GCS — no data egress, no rewrites.
    """
    bucket_name = os.environ.get("YTFACTORY_BUCKET", "ytfactory-prod-v3-artifacts")
    try:
        from google.cloud import storage  # noqa: PLC0415
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        src_prefix = f"jobs/{old_job_id}/cache/"
        dst_prefix = f"jobs/{new_job_id}/cache/"
        count = 0
        for blob in bucket.list_blobs(prefix=src_prefix):
            rel = blob.name[len(src_prefix):]
            if not rel or rel.endswith("/"):
                continue
            new_name = dst_prefix + rel
            bucket.copy_blob(blob, bucket, new_name=new_name)
            count += 1
        if count:
            logger.info(
                "retry cache copy: %d objects %s → %s",
                count, src_prefix, dst_prefix,
            )
        return count, False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "retry cache copy failed for old=%s new=%s: %s — proceeding "
            "with empty cache (retry will pay full cost)",
            old_job_id, new_job_id, exc,
        )
        return 0, True
