"""Pin the Cloud Run JOB execution reconciler — B3b in 2026-05-23 docket.

Backstory: the worker writes ``status=rendering`` when it boots and
updates Firestore on each stage transition. If the JOB execution
crashes (SIGKILL on task-timeout, OOM, host eviction) the Python
interpreter is dead so no try/except can write status=failed. The
B3a SIGTERM handler covers the soft path; this reconciler is the
safety net for the hard path.

These tests stub Firestore (``google.cloud.firestore``) and the Cloud
Run Executions client (``google.cloud.run_v2.ExecutionsClient``) so
the reconciler logic is exercised without making real API calls.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _fake_doc(job_id: str, **fields):
    """Construct a fake Firestore DocumentSnapshot."""
    snap = SimpleNamespace()
    snap.id = job_id
    snap.to_dict = lambda: dict(fields)
    return snap


def _fake_execution(*, completion_seconds=0, failed=0, succeeded=0,
                    cancelled=0, running=0):
    """Construct a fake Cloud Run Execution proto."""
    ct = SimpleNamespace(seconds=completion_seconds)
    return SimpleNamespace(
        completion_time=ct,
        failed_count=failed,
        succeeded_count=succeeded,
        cancelled_count=cancelled,
        running_count=running,
    )


class _FakeQuery:
    def __init__(self, docs):
        self._docs = docs

    def where(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def stream(self):
        return iter(self._docs)


class _FakeDocRef:
    def __init__(self, sink):
        self._sink = sink

    def set(self, fields, merge=False):  # noqa: ARG002
        self._sink.append(dict(fields))


class _FakeCollection:
    def __init__(self, docs_by_status: dict[str, list], writes_sink: dict):
        self._docs_by_status = docs_by_status
        self._writes_sink = writes_sink

    def where(self, field, op, value):  # noqa: ARG002
        # The reconciler queries by status first; record it.
        if field == "status" and op == "==":
            self._current_status = value
            return _QueryWithStatus(
                self._docs_by_status.get(value, []), self,
            )
        # If called without status hit (defensive), return empty.
        return _FakeQuery([])

    def document(self, doc_id):
        sink = self._writes_sink.setdefault(doc_id, [])
        return _FakeDocRef(sink)


class _QueryWithStatus:
    def __init__(self, docs, parent):
        self._docs = docs
        self._parent = parent

    def where(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def stream(self):
        return iter(self._docs)


class _FakeFirestoreClient:
    def __init__(self, docs_by_status, writes_sink):
        self._collection = _FakeCollection(docs_by_status, writes_sink)

    def collection(self, name):  # noqa: ARG002
        return self._collection


class ReconcilerTest(unittest.TestCase):
    """Pin the silent-death reconciliation."""

    def _run_reconciler(self, *, docs_by_status, execution_responses,
                        dry_run=False):
        from control.core import reconciler

        writes: dict = {}
        fs_client = _FakeFirestoreClient(docs_by_status, writes)

        exec_client = MagicMock()
        # execution_responses is dict {exec_name: Execution|Exception}
        def _get_execution(name):
            response = execution_responses[name]
            if isinstance(response, Exception):
                raise response
            return response
        exec_client.get_execution.side_effect = _get_execution

        fake_firestore = SimpleNamespace(
            Client=MagicMock(return_value=fs_client),
        )
        fake_run_v2 = SimpleNamespace(
            ExecutionsClient=MagicMock(return_value=exec_client),
        )

        # ``from google.cloud import firestore`` resolves via
        # ``getattr(google.cloud, "firestore")`` AFTER the parent
        # package has been imported once. Patching ONLY sys.modules
        # is insufficient because the prior real-import set the
        # attribute on ``google.cloud`` — we have to patch the
        # attribute too. create=True covers the fresh-process case
        # where the attribute doesn't yet exist.
        import google.cloud as _gcloud  # noqa: PLC0415
        with patch.dict("sys.modules", {
            "google.cloud.firestore": fake_firestore,
            "google.cloud.run_v2": fake_run_v2,
        }), patch.object(_gcloud, "firestore", fake_firestore, create=True), \
                patch.object(_gcloud, "run_v2", fake_run_v2, create=True), \
                patch.object(reconciler, "_safe_update",
                             side_effect=lambda db, jid, f: writes.setdefault(jid, []).append(f)):
            summary = reconciler.reconcile_stuck_jobs(dry_run=dry_run)
        return summary, writes

    def test_failed_execution_marks_job_failed(self):
        """The whole point of B3b. SIGKILL-killed execution → status=failed."""
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        docs = {
            "rendering": [_fake_doc(
                "job-dead",
                status="rendering",
                stage="images",
                updated_at=old,
                cloud_execution="projects/x/locations/asia-southeast1/jobs/render/executions/exec-1",
            )],
        }
        execs = {
            "projects/x/locations/asia-southeast1/jobs/render/executions/exec-1":
                _fake_execution(completion_seconds=1700000000, failed=1),
        }
        summary, writes = self._run_reconciler(
            docs_by_status=docs, execution_responses=execs,
        )
        self.assertEqual(summary["marked_failed"], 1)
        self.assertEqual(summary["candidates"], 1)
        write = writes["job-dead"][0]
        self.assertEqual(write["status"], "failed")
        self.assertEqual(write["stage"], "images")
        self.assertIn("failed_count=1", write["error"])
        self.assertIn("Reconciler", write["error"])
        self.assertIn("SIGKILL", write["error"])

    def test_still_running_execution_is_left_alone(self):
        """Worker may be in a long compose stage that doesn't write
        Firestore for minutes. DON'T mark failed if execution is alive.
        """
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        docs = {
            "rendering": [_fake_doc(
                "job-alive",
                status="rendering",
                stage="compose",
                updated_at=old,
                cloud_execution="exec-alive",
            )],
        }
        execs = {
            "exec-alive": _fake_execution(completion_seconds=0, running=1),
        }
        summary, writes = self._run_reconciler(
            docs_by_status=docs, execution_responses=execs,
        )
        self.assertEqual(summary["still_running"], 1)
        self.assertEqual(summary["marked_failed"], 0)
        self.assertNotIn("job-alive", writes)

    def test_cancelled_execution_marks_job_cancelled(self):
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        docs = {
            "running": [_fake_doc(
                "job-cancel",
                status="running",
                stage="tts",
                updated_at=old,
                cloud_execution="exec-c",
            )],
        }
        execs = {
            "exec-c": _fake_execution(completion_seconds=1700000000, cancelled=1),
        }
        summary, writes = self._run_reconciler(
            docs_by_status=docs, execution_responses=execs,
        )
        self.assertEqual(summary["marked_cancelled"], 1)
        self.assertEqual(writes["job-cancel"][0]["status"], "cancelled")

    def test_execution_not_found_marks_job_failed(self):
        """Cloud Run evicts execution records after 60 days → mark failed."""
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        docs = {
            "rendering": [_fake_doc(
                "job-orphan",
                status="rendering",
                stage="images",
                updated_at=old,
                cloud_execution="exec-evicted",
            )],
        }
        execs = {"exec-evicted": Exception("NotFound: 404 execution gone")}
        with patch("control.core.reconciler.logger"):
            summary, writes = self._run_reconciler(
                docs_by_status=docs, execution_responses=execs,
            )
        self.assertEqual(summary["execution_not_found"], 1)
        self.assertEqual(writes["job-orphan"][0]["status"], "failed")
        self.assertIn("evicted", writes["job-orphan"][0]["error"])

    def test_no_execution_ref_marks_job_failed(self):
        """Worker died between dispatch and writing cloud_execution."""
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        docs = {
            "pending": [_fake_doc(
                "job-noref",
                status="pending",
                stage="dispatching",
                updated_at=old,
                # NO cloud_execution field
            )],
        }
        summary, writes = self._run_reconciler(
            docs_by_status=docs, execution_responses={},
        )
        self.assertEqual(summary["no_execution_ref"], 1)
        self.assertEqual(writes["job-noref"][0]["status"], "failed")
        self.assertIn("no cloud_execution", writes["job-noref"][0]["error"])

    def test_dry_run_does_not_write(self):
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        docs = {
            "rendering": [_fake_doc(
                "job-dry", status="rendering", stage="images",
                updated_at=old, cloud_execution="exec-d",
            )],
        }
        execs = {"exec-d": _fake_execution(completion_seconds=1700000000, failed=1)}
        summary, writes = self._run_reconciler(
            docs_by_status=docs, execution_responses=execs, dry_run=True,
        )
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["marked_failed"], 1)
        # No writes happened despite the action count.
        self.assertEqual(writes, {})

    def test_execution_lookup_failure_does_not_mark_or_crash(self):
        """Transient SDK error → leave job alone, retry next tick."""
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        docs = {
            "rendering": [_fake_doc(
                "job-flaky", status="rendering", stage="images",
                updated_at=old, cloud_execution="exec-flaky",
            )],
        }
        execs = {"exec-flaky": Exception("DeadlineExceeded: transient")}
        with patch("control.core.reconciler.logger"):
            summary, writes = self._run_reconciler(
                docs_by_status=docs, execution_responses=execs,
            )
        self.assertEqual(summary["execution_lookup_failed"], 1)
        self.assertEqual(summary["marked_failed"], 0)
        self.assertNotIn("job-flaky", writes)

    def test_succeeded_but_no_writeback_marks_failed_with_investigate_msg(self):
        """Worker died after JOB succeeded but before status=done writeback."""
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        docs = {
            "rendering": [_fake_doc(
                "job-ghostsuccess",
                status="rendering", stage="upload",
                updated_at=old, cloud_execution="exec-gs",
            )],
        }
        execs = {"exec-gs": _fake_execution(completion_seconds=1700000000, succeeded=1)}
        summary, writes = self._run_reconciler(
            docs_by_status=docs, execution_responses=execs,
        )
        self.assertEqual(summary["marked_succeeded_but_unwritten"], 1)
        self.assertEqual(writes["job-ghostsuccess"][0]["status"], "failed")
        self.assertIn("mp4 may exist", writes["job-ghostsuccess"][0]["error"])

    def test_no_candidates_returns_empty_summary_safely(self):
        summary, writes = self._run_reconciler(
            docs_by_status={}, execution_responses={},
        )
        self.assertEqual(summary["candidates"], 0)
        self.assertEqual(summary["marked_failed"], 0)
        self.assertEqual(writes, {})

    def test_completion_time_as_datetime_marks_job_failed(self):
        # Regression: ``google-cloud-run`` (proto-plus) returns
        # ``Execution.completion_time`` as a ``datetime`` subclass
        # (DatetimeWithNanoseconds), NOT a raw proto Timestamp with a
        # ``.seconds`` attribute. Before 2026-05-24 the reconciler did
        # ``getattr(completion_time, "seconds", 0)`` which returned 0
        # for every real datetime, so every completed execution was
        # mislabelled ``still_running`` and never reconciled. Job
        # 82682e8e (mystoriesanimated tifu) sat queued for 24h
        # because of this. Pin the datetime-shape so the bug can't
        # come back the next time the SDK is upgraded.
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        docs = {
            "pending": [_fake_doc(
                "job-datetime",
                status="pending",
                stage="dispatching",
                updated_at=old,
                cloud_execution="exec-dt",
            )],
        }
        execs = {
            "exec-dt": SimpleNamespace(
                completion_time=datetime(2026, 5, 23, 20, 51, 23,
                                         tzinfo=timezone.utc),
                failed_count=1,
                succeeded_count=0,
                cancelled_count=0,
                running_count=0,
            ),
        }
        summary, writes = self._run_reconciler(
            docs_by_status=docs, execution_responses=execs,
        )
        self.assertEqual(summary["marked_failed"], 1,
                         "datetime-shaped completion_time must be "
                         "treated as 'set', else reconciler no-ops")
        self.assertEqual(writes["job-datetime"][0]["status"], "failed")

    def test_unset_completion_time_as_epoch_datetime_is_still_running(self):
        # When the proto Timestamp is unset, proto-plus surfaces it as
        # ``DatetimeWithNanoseconds(1970, 1, 1)`` — truthy but
        # semantically "not set". Must not mark failed: worker may be
        # mid-stage.
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        docs = {
            "rendering": [_fake_doc(
                "job-epoch",
                status="rendering",
                stage="compose",
                updated_at=old,
                cloud_execution="exec-epoch",
            )],
        }
        execs = {
            "exec-epoch": SimpleNamespace(
                completion_time=datetime(1970, 1, 1, tzinfo=timezone.utc),
                failed_count=0,
                succeeded_count=0,
                cancelled_count=0,
                running_count=1,
            ),
        }
        summary, writes = self._run_reconciler(
            docs_by_status=docs, execution_responses=execs,
        )
        self.assertEqual(summary["still_running"], 1)
        self.assertEqual(summary["marked_failed"], 0)
        self.assertNotIn("job-epoch", writes)

    def test_all_status_query_failures_surface_error(self):
        # Regression: previously, a missing composite index made every
        # status query 400 and the reconciler silently returned a
        # success-shaped ``{scanned:0, candidates:0, error:None}``
        # summary. Violates feedback_silent_fallback_unshippable_output.
        # Now ``summary['error']`` is set and ``per_status_errors``
        # lists each failure.
        from control.core import reconciler

        class _ExplodingCollection:
            def where(self, *_a, **_k):
                raise RuntimeError(
                    "FailedPrecondition: 400 The query requires an index",
                )

        class _ExplodingClient:
            def collection(self, *_a, **_k):
                return _ExplodingCollection()

        fake_firestore = SimpleNamespace(
            Client=MagicMock(return_value=_ExplodingClient()),
        )
        fake_run_v2 = SimpleNamespace(
            ExecutionsClient=MagicMock(return_value=MagicMock()),
        )

        import google.cloud as _gcloud  # noqa: PLC0415
        with patch.dict("sys.modules", {
            "google.cloud.firestore": fake_firestore,
            "google.cloud.run_v2": fake_run_v2,
        }), patch.object(_gcloud, "firestore", fake_firestore, create=True), \
                patch.object(_gcloud, "run_v2", fake_run_v2, create=True):
            summary = reconciler.reconcile_stuck_jobs(dry_run=True)

        self.assertEqual(summary["scanned"], 0)
        self.assertEqual(summary["candidates"], 0)
        self.assertIsNotNone(summary["error"],
                             "error must be set when status queries fail")
        self.assertIn("scans failed", summary["error"])
        self.assertIn("firestore.indexes.json", summary["error"])
        self.assertEqual(set(summary["per_status_errors"].keys()),
                         {"queued", "pending", "dispatching",
                          "rendering", "running"})


class ReconcilerStubResistanceTest(unittest.TestCase):
    """Pin the test-stubbing pattern itself.

    Regression: in the 2026-05-23 full-suite run my reconciler tests
    PASSED in isolation but FAILED when run after any test that
    transitively imported the real ``google.cloud.run_v2`` (e.g. any
    test that touches ``cloud_run.trigger_render_job``). Root cause:
    ``patch.dict(sys.modules, ...)`` alone is not enough — the
    statement ``from google.cloud import run_v2`` inside
    ``reconciler.py`` resolves the symbol via
    ``getattr(google.cloud, "run_v2")`` after the parent package has
    been imported once, and the attribute on the namespace package
    survives the sys.modules patch.

    This test simulates that pollution explicitly: it pre-installs a
    real-looking module attribute on ``google.cloud`` BEFORE running
    the reconciler harness. If the harness's stubbing strategy is
    correct, the fake still wins; if a future refactor regresses it,
    this test will go red.
    """

    def test_works_even_when_real_google_cloud_attrs_preloaded(self):
        import google.cloud as _gcloud  # noqa: PLC0415

        # Stand in for "some prior test already imported real run_v2".
        sentinel_run = SimpleNamespace(
            ExecutionsClient=MagicMock(
                side_effect=RuntimeError(
                    "BUG: real run_v2 used instead of test fake — "
                    "patch.dict(sys.modules) alone is insufficient when "
                    "the namespace attribute is preloaded; the test "
                    "harness must patch.object(_gcloud, 'run_v2', ...)"
                ),
            ),
        )
        sentinel_fs = SimpleNamespace(
            Client=MagicMock(
                side_effect=RuntimeError(
                    "BUG: real firestore used instead of test fake"
                ),
            ),
        )

        prev_run = getattr(_gcloud, "run_v2", None)
        prev_fs = getattr(_gcloud, "firestore", None)
        _gcloud.run_v2 = sentinel_run
        _gcloud.firestore = sentinel_fs
        try:
            old = datetime.now(timezone.utc) - timedelta(hours=3)
            docs = {
                "rendering": [_fake_doc(
                    "job-resist",
                    status="rendering",
                    stage="images",
                    updated_at=old,
                    cloud_execution="exec-resist",
                )],
            }
            execs = {
                "exec-resist": _fake_execution(
                    completion_seconds=1700000000, failed=1,
                ),
            }
            harness = ReconcilerTest()
            summary, writes = harness._run_reconciler(
                docs_by_status=docs, execution_responses=execs,
            )
            # If the fakes are NOT honored, the harness will raise
            # RuntimeError from the sentinel above. We get this far
            # only when the patching strategy correctly overrides the
            # namespace-package attribute.
            self.assertEqual(summary["marked_failed"], 1)
            self.assertEqual(writes["job-resist"][0]["status"], "failed")
        finally:
            if prev_run is None:
                delattr(_gcloud, "run_v2")
            else:
                _gcloud.run_v2 = prev_run
            if prev_fs is None:
                delattr(_gcloud, "firestore")
            else:
                _gcloud.firestore = prev_fs


if __name__ == "__main__":
    unittest.main()
