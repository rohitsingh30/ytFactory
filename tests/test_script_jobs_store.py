"""Tests for ``web.script_jobs_store.ScriptJobsStore``.

Two surfaces:

1. **Memory mode** (default; ``YTFACTORY_QUEUE_BACKEND`` unset/``memory``):
   the store must behave like a plain ``dict`` so existing call sites
   (``store[id] = rec``, ``store.pop(id, None)``, ``store.clear()``)
   keep working. No Firestore dependency, no daemons.

2. **Firestore mode**: writes go through to a fake Firestore client so
   we pin the persistence contract — flush(), hydrate(), the periodic
   sweeper marking running records dirty, and graceful shutdown
   (cancelled flush_dirty_loop runs one final sweep).
"""

from __future__ import annotations

import asyncio
import os
import time
import unittest
from typing import Any
from unittest.mock import MagicMock

from tests._helpers import PROJECT_ROOT  # noqa: F401

from web import script_jobs_store as sjs


class _FakeDoc:
    def __init__(self, doc_id: str, data: dict[str, Any]):
        self.id = doc_id
        self._data = data

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


class _FakeDocRef:
    def __init__(self, parent: "_FakeCollection", doc_id: str):
        self._parent = parent
        self._doc_id = doc_id

    def set(self, payload: dict[str, Any]) -> None:
        self._parent._docs[self._doc_id] = dict(payload)

    def delete(self) -> None:
        self._parent._docs.pop(self._doc_id, None)


class _FakeCollection:
    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self, doc_id)

    def stream(self):
        return [_FakeDoc(k, v) for k, v in self._docs.items()]


class _FakeFirestore:
    def __init__(self) -> None:
        self._collections: dict[str, _FakeCollection] = {}

    def collection(self, name: str) -> _FakeCollection:
        return self._collections.setdefault(name, _FakeCollection())


# ---------------------------------------------------------------------------
# Memory mode
# ---------------------------------------------------------------------------


class MemoryModeTests(unittest.TestCase):
    """Default backend = plain in-memory dict, zero side effects."""

    def setUp(self) -> None:
        # Make sure the env doesn't accidentally enable firestore.
        self._restore = os.environ.pop("YTFACTORY_QUEUE_BACKEND", None)

    def tearDown(self) -> None:
        if self._restore is not None:
            os.environ["YTFACTORY_QUEUE_BACKEND"] = self._restore

    def test_setitem_getitem(self):
        store = sjs.ScriptJobsStore()
        store["job1"] = {"job_id": "job1", "state": "running"}
        self.assertEqual(store["job1"]["state"], "running")
        self.assertIn("job1", store)

    def test_pop_with_default(self):
        store = sjs.ScriptJobsStore()
        store["job1"] = {"job_id": "job1"}
        self.assertEqual(store.pop("job1", None), {"job_id": "job1"})
        self.assertIsNone(store.pop("missing", None))

    def test_clear(self):
        store = sjs.ScriptJobsStore()
        store["a"] = {}
        store["b"] = {}
        store.clear()
        self.assertEqual(len(store), 0)

    def test_values_iter(self):
        store = sjs.ScriptJobsStore()
        store["a"] = {"v": 1}
        store["b"] = {"v": 2}
        self.assertEqual(sorted(r["v"] for r in store.values()), [1, 2])

    def test_in_place_mutation_visible(self):
        """The renderer mutates ``rec[k] = v`` after fetching the record;
        that pattern must keep working unchanged in memory mode."""
        store = sjs.ScriptJobsStore()
        store["job1"] = {"state": "running"}
        rec = store["job1"]
        rec["state"] = "done"
        self.assertEqual(store["job1"]["state"], "done")

    def test_flush_is_noop_in_memory(self):
        store = sjs.ScriptJobsStore()
        store["job1"] = {"state": "done"}
        store.flush("job1")  # no exception, no firestore client
        self.assertFalse(store.firestore_enabled)

    def test_hydrate_is_noop_in_memory(self):
        store = sjs.ScriptJobsStore()
        loaded = asyncio.new_event_loop().run_until_complete(store.hydrate())
        self.assertEqual(loaded, 0)


# ---------------------------------------------------------------------------
# Firestore mode
# ---------------------------------------------------------------------------


def _make_firestore_store() -> tuple[sjs.ScriptJobsStore, _FakeFirestore]:
    fake = _FakeFirestore()
    store = sjs.ScriptJobsStore.__new__(sjs.ScriptJobsStore)
    dict.__init__(store)
    store._collection = "script_jobs"
    store._dirty = set()
    store._fs = fake
    store._flush_task = None
    return store, fake


class FirestoreModeTests(unittest.TestCase):
    """Backend = firestore — writes mirror through to the fake client."""

    def test_setitem_marks_dirty(self):
        store, _ = _make_firestore_store()
        store["job1"] = {"job_id": "job1", "state": "running"}
        self.assertIn("job1", store._dirty)

    def test_flush_writes_through(self):
        store, fake = _make_firestore_store()
        store["job1"] = {"job_id": "job1", "state": "done", "mp4_path": "gs://b/x.mp4"}
        store.flush("job1")
        self.assertNotIn("job1", store._dirty)
        self.assertEqual(
            fake.collection("script_jobs")._docs["job1"]["state"], "done"
        )

    def test_delete_mirrors_to_firestore(self):
        store, fake = _make_firestore_store()
        store["job1"] = {"state": "running"}
        store.flush("job1")
        del store["job1"]
        self.assertNotIn("job1", fake.collection("script_jobs")._docs)

    def test_pop_with_default_mirrors_delete(self):
        store, fake = _make_firestore_store()
        store["job1"] = {"state": "running"}
        store.flush("job1")
        store.pop("job1", None)
        self.assertNotIn("job1", fake.collection("script_jobs")._docs)
        # Pop of missing key with default must not raise (plain dict semantic).
        self.assertIsNone(store.pop("missing", None))

    def test_hydrate_loads_existing_docs(self):
        store, fake = _make_firestore_store()
        fake.collection("script_jobs")._docs["existing"] = {
            "job_id": "existing", "state": "done", "mp4_path": "/tmp/x.mp4"
        }
        loaded = asyncio.new_event_loop().run_until_complete(store.hydrate())
        self.assertEqual(loaded, 1)
        self.assertEqual(store["existing"]["state"], "done")
        # Hydrated records must NOT be flagged dirty (they're already
        # the source of truth).
        self.assertNotIn("existing", store._dirty)

    def test_serialise_strips_non_native_types(self):
        # Pin contract: future schema additions of complex types don't
        # blow up Firestore writes.
        from pathlib import Path
        record = {
            "ok": "string",
            "n": 42,
            "f": 1.5,
            "lst": ["a", 1, None],
            "nested": {"a": {"b": "c"}},
            "weird": Path("/x"),  # not natively serialisable
        }
        out = sjs._serialise_for_firestore(record)
        self.assertEqual(out["ok"], "string")
        self.assertEqual(out["n"], 42)
        self.assertEqual(out["lst"], ["a", 1, None])
        self.assertEqual(out["nested"], {"a": {"b": "c"}})
        # Path should fall through to str() so the write still lands.
        self.assertEqual(out["weird"], "/x")


class FlushDirtyLoopTests(unittest.IsolatedAsyncioTestCase):
    """The periodic sweeper persists in-place mutations on running jobs."""

    async def test_sweep_marks_running_jobs_dirty(self):
        store, fake = _make_firestore_store()
        store["job_running"] = {"state": "running"}
        store["job_done"] = {"state": "done"}
        # First flush so the dirty set clears.
        store.flush("job_running")
        store.flush("job_done")
        self.assertEqual(len(store._dirty), 0)
        # Caller mutates in place WITHOUT calling mark_dirty.
        store["job_running"]["mp4_path"] = "/tmp/half.mp4"
        store["job_done"]["mp4_path"] = "/tmp/final.mp4"
        # Sweep should re-flag the running one but ignore the done one.
        store._sweep_running()
        self.assertIn("job_running", store._dirty)
        self.assertNotIn("job_done", store._dirty)

    async def test_loop_persists_running_mutations_periodically(self):
        store, fake = _make_firestore_store()
        store["job1"] = {"state": "running", "stage": 1}
        store.flush("job1")
        # Start the loop with a fast interval; mutate; let it sweep.
        task = asyncio.create_task(store.flush_dirty_loop(interval_s=0.05))
        try:
            await asyncio.sleep(0.02)
            store["job1"]["stage"] = 99
            await asyncio.sleep(0.15)
            self.assertEqual(
                fake.collection("script_jobs")._docs["job1"]["stage"], 99
            )
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_cancellation_does_one_final_flush(self):
        store, fake = _make_firestore_store()
        store["job1"] = {"state": "done"}  # done but never flushed
        task = asyncio.create_task(store.flush_dirty_loop(interval_s=10.0))
        await asyncio.sleep(0.01)  # let the loop enter sleep
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # The cancellation handler must persist the leftover dirty record.
        self.assertEqual(
            fake.collection("script_jobs")._docs["job1"]["state"], "done"
        )


if __name__ == "__main__":
    unittest.main()
