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
        now = _utcnow()
        doc = {
            "job_id": job_id,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            **fields,
        }
        self._ref(job_id).set(doc)
        return doc

    def update(self, job_id: str, **fields: Any) -> None:
        fields["updated_at"] = _utcnow()
        self._ref(job_id).set(fields, merge=True)

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


def create_job(job_id: str, *, channel: str, topic: str, proposal: dict[str, Any], owner_uid: str | None = None) -> None:
    get_jobs().create(
        job_id,
        channel=channel,
        topic=topic,
        proposal=proposal,
        owner_uid=owner_uid,
        status=STATUS_PENDING,
        stage="queued",
    )


def mark_stage(job_id: str, *, status: str, stage: str, **extra: Any) -> None:
    """Advance the job through a stage. extra is merged into the doc."""
    get_jobs().update(job_id, status=status, stage=stage, **extra)


def mark_done(job_id: str, *, short_uri: str, youtube_url: str | None = None, thumb_uri: str | None = None) -> None:
    fields: dict[str, Any] = {"status": STATUS_DONE, "stage": "done", "short_uri": short_uri, "error": None}
    if youtube_url:
        fields["youtube_url"] = youtube_url
    if thumb_uri:
        fields["thumb_uri"] = thumb_uri
    get_jobs().update(job_id, **fields)


def mark_failed(job_id: str, *, stage: str, error: str) -> None:
    get_jobs().update(job_id, status=STATUS_FAILED, stage=stage, error=error[:2000])


def get_job(job_id: str) -> dict[str, Any] | None:
    return get_jobs().get(job_id)


# ---------------------------------------------------------------------------
# Render dispatch — proposal → Job + (queue task | direct Cloud Run trigger)
# ---------------------------------------------------------------------------


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
    """
    # Deferred imports — these modules import jobs.py for ConfirmResponse,
    # so importing them at module load time would be circular.
    from control.core import cloud_run  # noqa: PLC0415
    from control.core.queue import get_queue, new_task_id  # noqa: PLC0415
    from control.core.schema import TaskEnvelope, TaskKind  # noqa: PLC0415

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
