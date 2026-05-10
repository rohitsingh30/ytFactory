"""Task queue with pluggable backend.

Two backends behind the same `Queue` protocol:
- `InMemoryQueue` for local tests (no GCP needed).
- `FirestoreQueue` for production.

Selection via `YTFACTORY_QUEUE_BACKEND={memory,firestore}` (default firestore).
"""
from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, Protocol

from control.core.schema import HEAVY_KINDS, TaskEnvelope, TaskKind, TaskStatus

_TASKS = "tasks"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Queue(Protocol):
    def enqueue(self, task: TaskEnvelope) -> None: ...
    def lease(self, agent_id: str, caps: Iterable[TaskKind], ttl_s: int) -> TaskEnvelope | None: ...
    def ack(self, task_id: str, agent_id: str, *, ok: bool, output_uri: str | None, error: str | None) -> None: ...
    def reap_expired(self) -> int: ...
    def get(self, task_id: str) -> TaskEnvelope | None: ...


# ---------------------------------------------------------------------------
# In-memory backend (tests, local dev)
# ---------------------------------------------------------------------------


class InMemoryQueue:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskEnvelope] = {}
        self._lock = threading.Lock()

    def enqueue(self, task: TaskEnvelope) -> None:
        with self._lock:
            self._tasks[task.task_id] = task

    def get(self, task_id: str) -> TaskEnvelope | None:
        return self._tasks.get(task_id)

    def lease(self, agent_id: str, caps: Iterable[TaskKind], ttl_s: int) -> TaskEnvelope | None:
        cap_set = set(caps)
        now = _utcnow()
        with self._lock:
            # Prefer FIFO by created_at among queued tasks matching caps.
            candidates = sorted(
                (t for t in self._tasks.values()
                 if t.status == TaskStatus.QUEUED and t.kind in cap_set),
                key=lambda t: t.created_at,
            )
            if not candidates:
                return None
            task = candidates[0]
            task.status = TaskStatus.LEASED
            task.lease_owner = agent_id
            task.lease_expires_at = now + timedelta(seconds=ttl_s)
            task.attempts += 1
            task.updated_at = now
            return task.model_copy()

    def ack(self, task_id: str, agent_id: str, *, ok: bool, output_uri: str | None, error: str | None) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            if task.lease_owner != agent_id:
                # Stale ack — ignore. The reaper or another agent owns it now.
                return
            task.updated_at = _utcnow()
            if ok:
                task.status = TaskStatus.DONE
                task.output_uri = output_uri
                task.error = None
            else:
                task.error = error
                if task.attempts >= task.max_attempts:
                    task.status = TaskStatus.FAILED
                else:
                    task.status = TaskStatus.QUEUED
                    task.lease_owner = None
                    task.lease_expires_at = None

    def reap_expired(self) -> int:
        n = 0
        now = _utcnow()
        with self._lock:
            for task in self._tasks.values():
                if (task.status == TaskStatus.LEASED
                        and task.lease_expires_at is not None
                        and task.lease_expires_at < now):
                    task.status = TaskStatus.QUEUED
                    task.lease_owner = None
                    task.lease_expires_at = None
                    task.updated_at = now
                    n += 1
        return n


# ---------------------------------------------------------------------------
# Firestore backend
# ---------------------------------------------------------------------------


class FirestoreQueue:
    def __init__(self, project: str | None = None) -> None:
        from google.cloud import firestore as _fs  # lazy import — keeps in-memory tests fast

        self._fs = _fs
        self._db = _fs.Client(project=project or os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod"))
        self._col = self._db.collection(_TASKS)

    def enqueue(self, task: TaskEnvelope) -> None:
        self._col.document(task.task_id).set(task.model_dump(mode="json"))

    def get(self, task_id: str) -> TaskEnvelope | None:
        snap = self._col.document(task_id).get()
        if not snap.exists:
            return None
        return TaskEnvelope.model_validate(snap.to_dict())

    def lease(self, agent_id: str, caps: Iterable[TaskKind], ttl_s: int) -> TaskEnvelope | None:
        # Firestore doesn't have a "find first matching and atomically update" primitive,
        # so we query → transactionally claim. The transaction re-reads and verifies.
        cap_values = [k.value for k in caps]
        if not cap_values:
            return None

        q = (self._col
             .where("status", "==", TaskStatus.QUEUED.value)
             .where("kind", "in", cap_values)
             .order_by("created_at")
             .limit(5))
        candidates = list(q.stream())
        if not candidates:
            return None

        for snap in candidates:
            ref = snap.reference
            transaction = self._db.transaction()
            claimed = _try_claim(transaction, ref, agent_id, ttl_s)
            if claimed is not None:
                return claimed
        return None

    def ack(self, task_id: str, agent_id: str, *, ok: bool, output_uri: str | None, error: str | None) -> None:
        ref = self._col.document(task_id)
        transaction = self._db.transaction()
        _try_ack(transaction, ref, agent_id, ok=ok, output_uri=output_uri, error=error)

    def reap_expired(self) -> int:
        now = _utcnow()
        q = self._col.where("status", "==", TaskStatus.LEASED.value).where("lease_expires_at", "<", now.isoformat())
        n = 0
        for snap in q.stream():
            ref = snap.reference
            transaction = self._db.transaction()
            if _try_reap(transaction, ref):
                n += 1
        return n


def _try_claim(transaction, ref, agent_id: str, ttl_s: int) -> TaskEnvelope | None:
    """Atomically claim a task if it's still QUEUED."""
    from google.cloud import firestore as _fs

    @_fs.transactional
    def _txn(tx) -> TaskEnvelope | None:
        snap = ref.get(transaction=tx)
        if not snap.exists:
            return None
        data = snap.to_dict()
        if data["status"] != TaskStatus.QUEUED.value:
            return None
        now = _utcnow()
        data["status"] = TaskStatus.LEASED.value
        data["lease_owner"] = agent_id
        data["lease_expires_at"] = (now + timedelta(seconds=ttl_s)).isoformat()
        data["attempts"] = data.get("attempts", 0) + 1
        data["updated_at"] = now.isoformat()
        tx.set(ref, data)
        return TaskEnvelope.model_validate(data)

    return _txn(transaction)


def _try_ack(transaction, ref, agent_id: str, *, ok: bool, output_uri: str | None, error: str | None) -> None:
    from google.cloud import firestore as _fs

    @_fs.transactional
    def _txn(tx) -> None:
        snap = ref.get(transaction=tx)
        if not snap.exists:
            return
        data = snap.to_dict()
        if data.get("lease_owner") != agent_id:
            return  # stale ack
        now = _utcnow()
        data["updated_at"] = now.isoformat()
        if ok:
            data["status"] = TaskStatus.DONE.value
            data["output_uri"] = output_uri
            data["error"] = None
        else:
            data["error"] = error
            if data.get("attempts", 0) >= data.get("max_attempts", 3):
                data["status"] = TaskStatus.FAILED.value
            else:
                data["status"] = TaskStatus.QUEUED.value
                data["lease_owner"] = None
                data["lease_expires_at"] = None
        tx.set(ref, data)

    _txn(transaction)


def _try_reap(transaction, ref) -> bool:
    from google.cloud import firestore as _fs

    @_fs.transactional
    def _txn(tx) -> bool:
        snap = ref.get(transaction=tx)
        if not snap.exists:
            return False
        data = snap.to_dict()
        if data.get("status") != TaskStatus.LEASED.value:
            return False
        data["status"] = TaskStatus.QUEUED.value
        data["lease_owner"] = None
        data["lease_expires_at"] = None
        data["updated_at"] = _utcnow().isoformat()
        tx.set(ref, data)
        return True

    return _txn(transaction)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_QUEUE: Queue | None = None


def get_queue() -> Queue:
    """Return the process-wide queue singleton.

    Backend is fixed at first call from `YTFACTORY_QUEUE_BACKEND`. Tests that
    need a fresh queue should call `reset_queue()` between cases.
    """
    global _QUEUE
    if _QUEUE is None:
        backend = os.environ.get("YTFACTORY_QUEUE_BACKEND", "memory").lower()
        if backend == "firestore":
            _QUEUE = FirestoreQueue()
        else:
            _QUEUE = InMemoryQueue()
    return _QUEUE


def reset_queue() -> None:
    """Tests only — wipe the singleton so the next get_queue() rebuilds it."""
    global _QUEUE
    _QUEUE = None


def new_task_id() -> str:
    return uuid.uuid4().hex
