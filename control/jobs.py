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
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_JOBS = "jobs"


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
    # Stamp the active OTel traceparent onto the job doc. The
    # render-worker reads it back via attach_traceparent_from_env so
    # the JOB's root span links into the chat-request trace.
    extra: dict[str, Any] = {}
    try:
        from pipeline.observability import propagation as _trace_prop  # noqa: PLC0415
        carrier: dict[str, str] = {}
        _trace_prop.inject_into_dict(carrier)
        if "traceparent" in carrier:
            extra["traceparent"] = carrier["traceparent"]
        if "tracestate" in carrier:
            extra["tracestate"] = carrier["tracestate"]
    except Exception:  # noqa: BLE001
        pass
    get_jobs().create(
        job_id,
        channel=channel,
        topic=topic,
        proposal=proposal,
        owner_uid=owner_uid,
        status=STATUS_PENDING,
        stage="queued",
        **extra,
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
