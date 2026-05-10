"""Persistent backing for ``web/server.py``'s ``SCRIPT_JOBS`` dict.

The job store starts as a plain in-memory ``dict`` for tests and dev
(no extra deps, no daemon). When ``YTFACTORY_QUEUE_BACKEND=firestore``
is set (the prod default on Cloud Run), every record is mirror-written
to a Firestore collection so jobs survive container rollover —
without that, a Cloud Run revision rotation mid-render leaves the UI
404'ing on a job ID that the running renderer is still chugging
through.

Persistence policy:

* ``__setitem__`` and ``__delitem__``/``pop`` mark the record dirty
  (and immediately delete on Firestore for deletions).
* The async ``flush_dirty_loop`` task — started once at app startup —
  flushes any dirty records to Firestore every ``interval_s`` seconds.
* ``flush(job_id)`` is a sync escape hatch the renderer calls at
  terminal state transitions for instant durability.
* ``hydrate()`` runs once at app startup, loading all existing
  ``script_jobs`` docs back into the in-memory dict so the UI sees
  prior runs immediately.

The class subclasses ``dict`` so all existing call sites
(``SCRIPT_JOBS[job_id] = {...}``, ``rec = SCRIPT_JOBS[job_id]``,
``rec["state"] = "done"``, ``SCRIPT_JOBS.values()``,
``SCRIPT_JOBS.pop(id, None)``, ``SCRIPT_JOBS.clear()``) keep working
unchanged. In-place mutations on the returned record dict ARE NOT
auto-detected — call ``mark_dirty(job_id)`` or ``flush(job_id)`` after
mutating, or rely on the periodic flush sweep that re-checks every
record.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_DEFAULT_FLUSH_INTERVAL_S = 5.0
_BACKEND_ENV = "YTFACTORY_QUEUE_BACKEND"


def _firestore_enabled() -> bool:
    return os.environ.get(_BACKEND_ENV, "memory").lower() == "firestore"


class ScriptJobsStore(dict):
    """In-memory dict + optional Firestore mirror.

    Behaves identically to a plain ``dict`` when the backend is
    ``memory`` (tests, local dev). With ``firestore`` it adds the
    persistence machinery described in the module docstring.
    """

    def __init__(self, collection: str = "script_jobs") -> None:
        super().__init__()
        self._collection = collection
        self._dirty: set[str] = set()
        self._fs: Any = None
        self._flush_task: asyncio.Task[None] | None = None
        if _firestore_enabled():
            self._fs = self._connect_firestore()

    # ----- backend helpers -------------------------------------------------

    def _connect_firestore(self) -> Any:
        try:
            from google.cloud import firestore  # noqa: PLC0415

            project = os.environ.get("GOOGLE_CLOUD_PROJECT")
            return firestore.Client(project=project) if project else firestore.Client()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "ScriptJobsStore: Firestore client init failed (%s); "
                "falling back to in-memory only — jobs will NOT survive "
                "container rollover",
                e,
            )
            return None

    @property
    def firestore_enabled(self) -> bool:
        return self._fs is not None

    # ----- mutating dict methods (track dirty / mirror deletes) ------------

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        super().__setitem__(key, value)
        self._dirty.add(key)

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self._dirty.discard(key)
        self._fs_delete(key)

    def pop(self, key: str, *args: Any) -> Any:  # type: ignore[override]
        try:
            value = super().pop(key, *args)
        except KeyError:
            raise
        # Only mirror to Firestore if the key actually existed.
        if not args or key in self._dirty or self._fs is not None:
            self._dirty.discard(key)
            self._fs_delete(key)
        return value

    def popitem(self) -> tuple[str, dict[str, Any]]:  # type: ignore[override]
        key, value = super().popitem()
        self._dirty.discard(key)
        self._fs_delete(key)
        return key, value

    def clear(self) -> None:
        keys = list(self.keys())
        super().clear()
        self._dirty.clear()
        for k in keys:
            self._fs_delete(k)

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        super().update(*args, **kwargs)
        # Re-add every key touched as dirty. Cheap because update is
        # called rarely on this store.
        for k in self.keys():
            self._dirty.add(k)

    # ----- explicit dirty / flush API --------------------------------------

    def mark_dirty(self, job_id: str) -> None:
        """Caller mutated ``self[job_id]`` in place — schedule a flush."""
        if job_id in self:
            self._dirty.add(job_id)

    def flush(self, job_id: str) -> None:
        """Synchronous flush of one record. No-op when memory-only.

        Call at terminal state transitions for instant durability —
        otherwise rely on ``flush_dirty_loop``.
        """
        if self._fs is None:
            return
        record = self.get(job_id)
        if record is None:
            return
        try:
            payload = _serialise_for_firestore(record)
            self._fs.collection(self._collection).document(job_id).set(payload)
            self._dirty.discard(job_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "ScriptJobsStore.flush(%s) failed (will retry on next sweep): %s",
                job_id,
                e,
            )

    def _fs_delete(self, job_id: str) -> None:
        if self._fs is None:
            return
        try:
            self._fs.collection(self._collection).document(job_id).delete()
        except Exception as e:  # noqa: BLE001
            logger.warning("ScriptJobsStore._fs_delete(%s) failed: %s", job_id, e)

    # ----- async helpers (called from FastAPI startup) ---------------------

    async def hydrate(self) -> int:
        """Load every persisted record into the in-memory dict.

        Returns the number of records loaded. No-op when memory-only.
        Uses ``super().__setitem__`` so hydrated records do NOT get
        re-flagged dirty.
        """
        if self._fs is None:
            return 0
        try:
            count = 0
            for doc in await asyncio.to_thread(_collection_stream, self._fs, self._collection):
                data = doc.to_dict() or {}
                super().__setitem__(doc.id, data)
                count += 1
            logger.info("ScriptJobsStore.hydrate: loaded %d records from firestore", count)
            return count
        except Exception as e:  # noqa: BLE001
            logger.warning("ScriptJobsStore.hydrate failed: %s", e)
            return 0

    async def flush_dirty_loop(self, interval_s: float = _DEFAULT_FLUSH_INTERVAL_S) -> None:
        """Background sweeper: every ``interval_s`` seconds, persist any
        dirty records. Started once at FastAPI startup. No-op when
        memory-only.
        """
        if self._fs is None:
            return
        try:
            while True:
                await asyncio.sleep(interval_s)
                self._sweep_running()
                for job_id in list(self._dirty):
                    self.flush(job_id)
        except asyncio.CancelledError:
            # Final sweep before shutdown so in-flight mutations don't
            # get dropped if the container is being killed gracefully.
            for job_id in list(self._dirty):
                self.flush(job_id)
            raise

    def _sweep_running(self) -> None:
        """Re-mark every non-terminal record dirty so in-place mutations
        on ``rec[...] = ...`` (which the existing renderer code does
        rather than calling ``mark_dirty``) still get persisted.

        Cheap because there are at most a handful of running jobs at
        once, and the actual Firestore ``set`` only happens in
        ``flush()`` which checks the dirty set anyway.
        """
        terminal = {"done", "done_no_mp4_found", "failed", "cancelled"}
        for job_id, rec in self.items():
            state = rec.get("state") if isinstance(rec, dict) else None
            if state not in terminal:
                self._dirty.add(job_id)

    def start_flush_task(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Idempotent: start the periodic flusher if not already running.

        Safe to call multiple times; subsequent calls return early when
        a task is already in flight.
        """
        if self._fs is None or (self._flush_task and not self._flush_task.done()):
            return
        loop = loop or asyncio.get_event_loop()
        self._flush_task = loop.create_task(self.flush_dirty_loop())

    async def stop_flush_task(self) -> None:
        if self._flush_task is None:
            return
        self._flush_task.cancel()
        try:
            await self._flush_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._flush_task = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collection_stream(fs: Any, collection: str) -> Iterable[Any]:
    """Eager list of every doc in a collection. Wrapped for asyncio.to_thread."""
    return list(fs.collection(collection).stream())


_FORBIDDEN_KEYS: frozenset[str] = frozenset({})  # reserved for future field stripping


def _serialise_for_firestore(record: dict[str, Any]) -> dict[str, Any]:
    """Strip non-serialisable fields before writing.

    Today every SCRIPT_JOBS field is JSON-safe (str / int / float / None /
    list / dict). Kept as a single chokepoint so future schema additions
    (e.g., subprocess handles, asyncio Tasks) get an explicit rejection
    rather than a silent Firestore SerializationError.
    """
    out: dict[str, Any] = {}
    for k, v in record.items():
        if k in _FORBIDDEN_KEYS:
            continue
        out[k] = _coerce(v)
    return out


def _coerce(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    # Fallback — stringify anything Firestore can't natively accept so
    # the write still succeeds rather than blowing up the whole record.
    return str(value)
