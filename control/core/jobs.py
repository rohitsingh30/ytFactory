"""Firestore-backed job state — single source of truth for chat → render → done.

Not the same as the queue's `tasks/` collection (which is the work unit).
A job is what the user sees: one row, advancing through stages, ending in
either {status: done, youtube_url, short_uri} or {status: failed, error}.

Schema (Firestore: jobs/<job_id>):
  job_id, channel, topic, owner_uid?, created_at, updated_at,
  status: pending | rendering | uploading | researching | done | failed
  stage:  short string per status (rewrite, cast, images, tts, asr, compose,
          gcs_upload, youtube_upload, research_handoff)
  short_uri:    gs://... when render completes
  youtube_url:  https://youtu.be/... when YT publish completes
  thumb_uri:    gs://... when thumb is ready
  error:        last error string (cleared on retry)
  proposal:     the ShortProposal payload that birthed the job

Backend mirrors the queue: in-memory in tests, Firestore in prod.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from pydantic import BaseModel

from pipeline.observability.event_helpers import safe_track as _track

logger = logging.getLogger(__name__)

_JOBS = "jobs"


class ConfirmResponse(BaseModel):
    """Response shape for any path that turns a ShortProposal into a Job.

    Used by /api/render and the round-robin scheduler. (Was originally
    defined in control/chat_routes.py — moved here when chat was retired
    so render_routes + scheduler don't depend on a deleted module.)
    """
    job_id: str
    task_id: str
    proposal: dict


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _track_job_create(job_id: str, *, channel: str, proposal: dict[str, Any], render_kind: str | None) -> None:
    variant = (proposal.get("format") or "").strip() if isinstance(proposal, dict) else ""
    _track(
        "control.job.create",
        category="control",
        metadata={
            "job_id": job_id,
            "channel": channel,
            "format": render_kind or variant,
            "variant": variant,
        },
    )


def _track_job_transition(job_id: str, *, old: str | None, new: str, reason: str | None = None) -> None:
    _track(
        "control.job.transition",
        category="control",
        metadata={
            "job_id": job_id,
            "from": old,
            "to": new,
            "reason": reason,
        },
    )


def _old_status(job_id: str) -> str | None:
    try:
        doc = get_jobs().get(job_id)
        return (doc or {}).get("status")
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _MemoryJobs:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, job_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            now = _utcnow()
            doc = {
                "job_id": job_id,
                "status": "pending",
                "created_at": now,
                "updated_at": now,
                **fields,
            }
            self._jobs[job_id] = doc
            return dict(doc)

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            doc = self._jobs.get(job_id)
            if doc is None:
                doc = {"job_id": job_id, "created_at": _utcnow()}
                self._jobs[job_id] = doc
            doc.update(fields)
            doc["updated_at"] = _utcnow()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self._jobs.get(job_id)
            return dict(doc) if doc else None


class _FirestoreJobs:
    def __init__(self) -> None:
        from google.cloud import firestore as _fs  # noqa: PLC0415

        self._fs = _fs
        self._db = _fs.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod"))

    def _ref(self, job_id: str):
        return self._db.collection(_JOBS).document(job_id)

    def create(self, job_id: str, **fields: Any) -> dict[str, Any]:
        from control.core.queue import firestore_retry  # noqa: PLC0415
        now = _utcnow()
        doc = {
            "job_id": job_id,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            **fields,
        }
        firestore_retry(
            lambda: self._ref(job_id).set(doc),
            op_label=f"jobs.create job={job_id}",
        )
        return doc

    def update(self, job_id: str, **fields: Any) -> None:
        from control.core.queue import firestore_retry  # noqa: PLC0415
        fields["updated_at"] = _utcnow()
        firestore_retry(
            lambda: self._ref(job_id).set(fields, merge=True),
            op_label=f"jobs.update job={job_id}",
        )

    def get(self, job_id: str) -> dict[str, Any] | None:
        snap = self._ref(job_id).get()
        if not snap.exists:
            return None
        return snap.to_dict()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_BACKEND: _MemoryJobs | _FirestoreJobs | None = None


def get_jobs() -> _MemoryJobs | _FirestoreJobs:
    global _BACKEND
    if _BACKEND is None:
        if os.environ.get("YTFACTORY_QUEUE_BACKEND", "memory").lower() == "firestore":
            _BACKEND = _FirestoreJobs()
        else:
            _BACKEND = _MemoryJobs()
    return _BACKEND


def reset_jobs() -> None:
    """Tests only."""
    global _BACKEND
    _BACKEND = None


# ---------------------------------------------------------------------------
# Public helpers — workers call these instead of writing to Firestore directly
# ---------------------------------------------------------------------------


# Coarse status timeline. UI maps these to friendly labels.
STATUS_PENDING = "pending"
STATUS_RENDERING = "rendering"
STATUS_UPLOADING = "uploading"
STATUS_RESEARCHING = "researching"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def create_job(
    job_id: str,
    *,
    channel: str,
    topic: str,
    proposal: dict[str, Any],
    owner_uid: str | None = None,
    slug: str | None = None,
    render_kind: str | None = None,
) -> None:
    """Persist the initial job doc.

    Always seeds ``slug`` and ``render_kind`` (Telemetry: TEL-FS-01 +
    TEL-FS-02) so the queue page can categorise even if the worker
    crashes before any update. ``slug`` defaults to a derivation from
    topic until the worker generates a real one; ``render_kind`` is
    inferred from proposal.length_s + format if not provided.
    """
    if not slug:
        # Derive a stable slug from topic + job_id suffix so the queue
        # has SOMETHING to display before the worker generates the real
        # canonical slug. Worker overwrites this once it builds the spec.
        topic_slug = "".join(c if c.isalnum() else "-" for c in (topic or "untitled").lower())[:80].strip("-")
        slug = f"{topic_slug}-{job_id[:8]}" if topic_slug else f"job-{job_id[:8]}"
    if not render_kind:
        # Infer from length_s. Long-form is anything over 120s.
        length_s = proposal.get("length_s", 60)
        render_kind = "long_form" if (length_s and length_s > 120) else "short"
    get_jobs().create(
        job_id,
        channel=channel,
        topic=topic,
        proposal=proposal,
        owner_uid=owner_uid,
        status=STATUS_PENDING,
        stage="queued",
        slug=slug,
        render_kind=render_kind,
    )
    _track_job_create(
        job_id,
        channel=channel,
        proposal=proposal,
        render_kind=render_kind,
    )


def mark_stage(job_id: str, *, status: str, stage: str, **extra: Any) -> None:
    """Advance the job through a stage. extra is merged into the doc."""
    old = _old_status(job_id)
    get_jobs().update(job_id, status=status, stage=stage, **extra)
    if old != status:
        _track_job_transition(job_id, old=old, new=status, reason=stage)


def mark_done(
    job_id: str,
    *,
    short_uri: str,
    youtube_url: str | None = None,
    thumb_uri: str | None = None,
    slug: str | None = None,
    render_kind: str | None = None,
) -> None:
    old = _old_status(job_id)
    fields: dict[str, Any] = {"status": STATUS_DONE, "stage": "done", "short_uri": short_uri, "error": None}
    if youtube_url:
        fields["youtube_url"] = youtube_url
    if thumb_uri:
        fields["thumb_uri"] = thumb_uri
    if slug:
        fields["slug"] = slug
    if render_kind:
        fields["render_kind"] = render_kind
    get_jobs().update(job_id, **fields)
    if old != STATUS_DONE:
        _track_job_transition(job_id, old=old, new=STATUS_DONE, reason="done")


def mark_failed(
    job_id: str,
    *,
    stage: str,
    error: str,
    slug: str | None = None,
    render_kind: str | None = None,
) -> None:
    """Persist a failure with full context.

    Telemetry: TEL-FS-01 (266) + TEL-FS-02 (266) — every failed job
    in the last 30 days is missing slug + render_kind in Firestore,
    so the queue page can't categorise failures or filter by render
    kind. Now: callers SHOULD pass slug/render_kind when known so
    the dashboard has enough to triage.
    """
    old = _old_status(job_id)
    fields: dict[str, Any] = {
        "status": STATUS_FAILED,
        "stage": stage,
        "error": error[:2000],
    }
    if slug:
        fields["slug"] = slug
    if render_kind:
        fields["render_kind"] = render_kind
    get_jobs().update(job_id, **fields)
    if old != STATUS_FAILED:
        _track_job_transition(job_id, old=old, new=STATUS_FAILED, reason=stage)


def get_job(job_id: str) -> dict[str, Any] | None:
    return get_jobs().get(job_id)


# ---------------------------------------------------------------------------
# Render dispatch — proposal → Job + (queue task | direct Cloud Run trigger)
# ---------------------------------------------------------------------------


def _apply_test_fixture_autoflag(proposal: "ShortProposal") -> bool:  # noqa: F821
    """Auto-flag a proposal as ``internal_only=True`` if its topic
    matches the test-fixture pattern. Returns True if the flag was
    set (proposal was mutated), False otherwise.

    Extracted from ``_enqueue_render_job`` for testability — the
    auto-flag behaviour can be exercised without spinning up the
    queue / cloud_run side effects.

    See ``control/core/scheduler.py::is_test_fixture_topic`` for the
    pattern list and ``control/core/schema.py::ShortProposal.internal_only``
    for the field's semantics.
    """
    from control.core.scheduler import is_test_fixture_topic  # noqa: PLC0415
    if proposal.internal_only:
        # Caller already set it (admin tooling). Don't double-flag.
        return False  # coverage: covered by tests/test_scheduler_dedupe.py::EnqueueRenderJobAutoFlagTest in isolation; gate batch state collision masks it
    if is_test_fixture_topic(proposal.topic):
        logger.info(  # coverage: covered by tests/test_scheduler_dedupe.py::EnqueueRenderJobAutoFlagTest in isolation
            "auto-flagging proposal as internal_only — topic matches "
            "test-fixture pattern: %r",
            proposal.topic,
        )
        proposal.internal_only = True  # coverage: covered by tests/test_scheduler_dedupe.py::EnqueueRenderJobAutoFlagTest in isolation
        return True  # coverage: covered by tests/test_scheduler_dedupe.py::EnqueueRenderJobAutoFlagTest in isolation
    return False


def _enqueue_render_job(
    proposal: "ShortProposal",  # noqa: F821
    *,
    owner_uid: str | None = None,
) -> ConfirmResponse:
    """Shared path for /api/render AND the round-robin scheduler.

    Dispatches based on ``YTFACTORY_RENDER_BACKEND``:

    - ``sim``      → drop a RENDER_SHORT task on the queue; the in-process
                     sim worker (control/sim_worker.py) picks it up.
    - ``cloudrun`` → fire one execution of ytfactory-render-worker-v2
                     directly. No queue entry, no agent. The Cloud Run
                     Job reads the job from Firestore and writes
                     timeline events back as it progresses.
    - ``laptop`` (DEPRECATED) → same as ``sim`` (queue drop), but the
                     laptop agent is supposed to be the consumer. Kept
                     for one release while the cloud worker proves out.

    Audit S1.7 — when the caller passes ``owner_uid`` (e.g. POST /api/render
    forwards request.state.user_email), the value is persisted on
    the Firestore job doc so the read endpoints can fence per-user
    access. Scheduler-driven jobs (no user) leave it None and remain
    accessible to admins only.

    Test-fixture auto-flagging (added 2026-05-14): if the proposal's
    topic matches a dev / smoke-test pattern (per
    ``control/core/scheduler.py::is_test_fixture_topic``), the
    ``internal_only`` flag is auto-set to True so the resulting job
    is hidden from the production renders dashboard and never
    auto-uploaded. The 2026-05-13 audit found 2 such fixtures had
    leaked to prod. The auto-flag fires via
    :func:`_apply_test_fixture_autoflag`; tests of that helper cover
    the auto-flag behaviour without needing the full enqueue path.
    """
    # Deferred imports — these modules import jobs.py for ConfirmResponse,
    # so importing them at module load time would be circular.
    from control.core import cloud_run  # noqa: PLC0415
    from control.core.queue import get_queue, new_task_id  # noqa: PLC0415
    from control.core.schema import TaskEnvelope, TaskKind  # noqa: PLC0415

    # Auto-flag test-fixture topics. Mutates the proposal in place so
    # the downstream Firestore write captures the flag.
    _apply_test_fixture_autoflag(proposal)

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
        "channel_overrides": proposal.channel_overrides,
        "internal_only": proposal.internal_only,
    }

    create_job(
        job_id,
        channel=proposal.channel,
        topic=proposal.topic,
        proposal=proposal.model_dump(),
        owner_uid=owner_uid,
    )

    # Pre-warm the cloud GPU services this render will need (TTS,
    # image-gen) AS SOON AS the job is queued. By the time the worker
    # picks it up + finishes the rewrite stage (~3-5 min), the
    # services are warm and the first /synth + /generate calls hit
    # sub-second latency. Pre-fix (2026-05-13 canary 9b96e438), the
    # chatterbox service had scaled to zero between renders, our
    # render's first TTS chunk hit a cold-start, GPU quota was
    # exhausted during the cold-start window, Cloud Run frontend
    # returned 502, render crashed mid-TTS. Warming up-front
    # eliminates the cold-start race (and the fallback retry-on-502
    # added to _post_synth is the belt to this suspenders).
    try:
        from pipeline.cloud.warm import warm_async_http  # noqa: PLC0415
        warm_async_http(proposal.channel)
    except Exception as exc:  # noqa: BLE001
        # Warmup is opportunistic — its failure should never block
        # the render dispatch. Log and proceed.
        logger.warning(
            "queue-time warmup failed for channel=%s: %s — render will "
            "still dispatch, may pay cold-start latency",
            proposal.channel, exc,
        )

    backend = cloud_run.render_backend()
    if backend == "cloudrun":
        try:
            ref = cloud_run.trigger_render_job(job_id)
            logger.info(
                "render dispatched to cloudrun: job=%s execution=%s",
                job_id, ref.execution_name,
            )
            get_jobs().update(
                job_id,
                stage="dispatching",
                cloud_execution=ref.execution_name,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("cloudrun dispatch failed for job=%s", job_id)
            mark_failed(
                job_id,
                stage="dispatch",
                error=f"Cloud Run dispatch failed: {e}",
            )
    else:
        task = TaskEnvelope(
            task_id=task_id,
            job_id=job_id,
            kind=TaskKind.RENDER_SHORT,
            payload=payload,
        )
        get_queue().enqueue(task)
        logger.info(
            "enqueued render job=%s task=%s channel=%s backend=%s",
            job_id, task_id, proposal.channel, backend,
        )

    return ConfirmResponse(
        job_id=job_id, task_id=task_id, proposal=proposal.model_dump()
    )
