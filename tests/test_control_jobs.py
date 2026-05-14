"""Tests for control/jobs.py — JobEnvelope state store (memory backend)."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"

from control.core import jobs as jobs_mod  # noqa: E402


class JobLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        jobs_mod.reset_jobs()

    def test_create_job_initial_fields(self):
        jobs_mod.create_job("j1", channel="sportsrecapped",
                            topic="Aguero 93:20", proposal={"length_s": 55})
        doc = jobs_mod.get_job("j1")
        self.assertIsNotNone(doc)
        assert doc is not None
        self.assertEqual(doc["job_id"], "j1")
        self.assertEqual(doc["channel"], "sportsrecapped")
        self.assertEqual(doc["topic"], "Aguero 93:20")
        self.assertEqual(doc["status"], jobs_mod.STATUS_PENDING)
        self.assertEqual(doc["stage"], "queued")
        self.assertIsNone(doc.get("owner_uid"))
        # Timestamps populated.
        self.assertIn("created_at", doc)
        self.assertIn("updated_at", doc)
        # Telemetry: TEL-FS-01 + TEL-FS-02. Slug + render_kind MUST be
        # seeded at create-time so failed jobs that never reach the
        # worker still display in the queue with a real category.
        self.assertIn("slug", doc)
        self.assertTrue(doc["slug"], "slug must not be empty")
        self.assertIn("aguero", doc["slug"].lower())
        self.assertEqual(doc["render_kind"], "short")  # length_s=55 → short

    def test_create_job_infers_long_form_render_kind(self):
        # length_s > 120 → render_kind = "long_form"
        jobs_mod.create_job("j-long", channel="historyrecapped",
                            topic="Operation Mincemeat",
                            proposal={"length_s": 600})
        doc = jobs_mod.get_job("j-long")
        assert doc is not None
        self.assertEqual(doc["render_kind"], "long_form")
        self.assertIn("operation-mincemeat", doc["slug"].lower())

    def test_mark_failed_persists_slug_and_render_kind_when_passed(self):
        # Telemetry: TEL-FS-01/02. Even when the worker finds the doc
        # already has slug from create_job, mark_failed should accept
        # explicit overrides so a worker that discovers the canonical
        # slug mid-render can update it. 266 failed docs in 30d had
        # neither field populated pre-fix.
        jobs_mod.create_job("j-fail", channel="auto", topic="t",
                            proposal={"length_s": 60})
        jobs_mod.mark_failed("j-fail", stage="render",
                             error="boom",
                             slug="canonical-slug-x",
                             render_kind="short")
        doc = jobs_mod.get_job("j-fail")
        assert doc is not None
        self.assertEqual(doc["slug"], "canonical-slug-x")
        self.assertEqual(doc["render_kind"], "short")
        self.assertEqual(doc["status"], jobs_mod.STATUS_FAILED)
        self.assertEqual(doc["stage"], "render")

    def test_mark_done_persists_slug_and_render_kind_when_passed(self):
        jobs_mod.create_job("j-done", channel="auto", topic="t",
                            proposal={"length_s": 60})
        jobs_mod.mark_done("j-done",
                           short_uri="gs://b/x.mp4",
                           slug="final-slug",
                           render_kind="short")
        doc = jobs_mod.get_job("j-done")
        assert doc is not None
        self.assertEqual(doc["slug"], "final-slug")
        self.assertEqual(doc["render_kind"], "short")

    def test_mark_stage_advances_status(self):
        jobs_mod.create_job("j2", channel="auto", topic="t", proposal={})
        jobs_mod.mark_stage("j2", status=jobs_mod.STATUS_RENDERING, stage="render", slug="t-x")
        doc = jobs_mod.get_job("j2")
        assert doc is not None
        self.assertEqual(doc["status"], jobs_mod.STATUS_RENDERING)
        self.assertEqual(doc["stage"], "render")
        self.assertEqual(doc["slug"], "t-x")

    def test_mark_done_clears_error_and_sets_uris(self):
        jobs_mod.create_job("j3", channel="auto", topic="t", proposal={})
        # Failure first, then recovery.
        jobs_mod.mark_failed("j3", stage="render", error="oops")
        doc = jobs_mod.get_job("j3")
        assert doc is not None
        self.assertEqual(doc["status"], jobs_mod.STATUS_FAILED)
        self.assertEqual(doc["error"], "oops")
        # mark_done explicitly clears error.
        jobs_mod.mark_done("j3", short_uri="gs://b/short.mp4",
                           youtube_url="https://youtu.be/abc", thumb_uri="gs://b/t.png")
        doc = jobs_mod.get_job("j3")
        assert doc is not None
        self.assertEqual(doc["status"], jobs_mod.STATUS_DONE)
        self.assertEqual(doc["stage"], "done")
        self.assertEqual(doc["short_uri"], "gs://b/short.mp4")
        self.assertEqual(doc["youtube_url"], "https://youtu.be/abc")
        self.assertEqual(doc["thumb_uri"], "gs://b/t.png")
        self.assertIsNone(doc["error"])

    def test_mark_done_without_youtube_keeps_short_uri_only(self):
        jobs_mod.create_job("j4", channel="auto", topic="t", proposal={})
        jobs_mod.mark_done("j4", short_uri="gs://b/x.mp4")
        doc = jobs_mod.get_job("j4")
        assert doc is not None
        self.assertEqual(doc["short_uri"], "gs://b/x.mp4")
        self.assertNotIn("youtube_url", doc)

    def test_mark_failed_truncates_long_errors(self):
        jobs_mod.create_job("j5", channel="auto", topic="t", proposal={})
        big = "x" * 5000
        jobs_mod.mark_failed("j5", stage="render", error=big)
        doc = jobs_mod.get_job("j5")
        assert doc is not None
        self.assertLessEqual(len(doc["error"]), 2000)
        self.assertTrue(doc["error"].startswith("xxxx"))

    def test_get_unknown_returns_none(self):
        self.assertIsNone(jobs_mod.get_job("does-not-exist"))

    def test_update_unknown_creates_doc(self):
        # The memory backend creates a stub if you update an unknown job —
        # important so workers that race the chat-confirm write don't drop
        # status updates on the floor.
        jobs_mod.get_jobs().update("racey", status=jobs_mod.STATUS_RENDERING, stage="render")
        doc = jobs_mod.get_job("racey")
        assert doc is not None
        self.assertEqual(doc["status"], jobs_mod.STATUS_RENDERING)


class BackendFactoryTest(unittest.TestCase):
    def setUp(self) -> None:
        jobs_mod.reset_jobs()

    def test_memory_backend_when_env_says_memory(self):
        os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"
        backend = jobs_mod.get_jobs()
        self.assertEqual(type(backend).__name__, "_MemoryJobs")

    def test_reset_jobs_drops_singleton(self):
        backend1 = jobs_mod.get_jobs()
        jobs_mod.reset_jobs()
        backend2 = jobs_mod.get_jobs()
        self.assertIsNot(backend1, backend2)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# FirestoreJobs backend (mocked)
# ---------------------------------------------------------------------------

class FirestoreJobsTest(unittest.TestCase):
    def _make_fj(self):
        from control.core.jobs import _FirestoreJobs
        with patch("google.cloud.firestore.Client") as MockClient:
            mock_db = MagicMock()
            MockClient.return_value = mock_db
            fj = _FirestoreJobs()
        return fj

    def test_create_returns_doc(self):
        fj = self._make_fj()
        doc_ref = MagicMock()
        fj._db.collection.return_value.document.return_value = doc_ref
        result = fj.create("job-1", channel="auto", topic="t")
        self.assertEqual(result["job_id"], "job-1")
        doc_ref.set.assert_called_once()

    def test_update_calls_set_with_merge(self):
        fj = self._make_fj()
        doc_ref = MagicMock()
        fj._db.collection.return_value.document.return_value = doc_ref
        fj.update("job-1", status="done")
        doc_ref.set.assert_called_once()
        _, kw = doc_ref.set.call_args
        self.assertTrue(kw.get("merge"))

    def test_get_existing_returns_dict(self):
        fj = self._make_fj()
        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {"job_id": "job-2", "status": "pending"}
        doc_ref = MagicMock()
        doc_ref.get.return_value = snap
        fj._db.collection.return_value.document.return_value = doc_ref
        result = fj.get("job-2")
        self.assertEqual(result["job_id"], "job-2")

    def test_get_missing_returns_none(self):
        fj = self._make_fj()
        snap = MagicMock()
        snap.exists = False
        doc_ref = MagicMock()
        doc_ref.get.return_value = snap
        fj._db.collection.return_value.document.return_value = doc_ref
        result = fj.get("ghost")
        self.assertIsNone(result)

    def test_ref_returns_document(self):
        fj = self._make_fj()
        mock_col = MagicMock()
        mock_doc = MagicMock()
        mock_col.document.return_value = mock_doc
        fj._db.collection.return_value = mock_col
        ref = fj._ref("job-3")
        self.assertEqual(ref, mock_doc)


class GetJobsFirestoreTest(unittest.TestCase):
    def setUp(self) -> None:
        jobs_mod.reset_jobs()

    def tearDown(self) -> None:
        os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"
        jobs_mod.reset_jobs()

    def test_firestore_backend_selected_when_env_says_firestore(self):
        from control.core.jobs import _FirestoreJobs
        with patch.dict(os.environ, {"YTFACTORY_QUEUE_BACKEND": "firestore"}):
            with patch("google.cloud.firestore.Client") as MockClient:
                mock_db = MagicMock()
                MockClient.return_value = mock_db
                backend = jobs_mod.get_jobs()
                self.assertIsInstance(backend, _FirestoreJobs)
                jobs_mod.reset_jobs()


# ---------------------------------------------------------------------------
# _enqueue_render_job — both cloudrun and sim/queue paths
# ---------------------------------------------------------------------------

class EnqueueRenderJobTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"
        jobs_mod.reset_jobs()
        from control.core.queue import reset_queue
        reset_queue()

    def _make_proposal(self):
        from control.core.schema import ShortProposal
        return ShortProposal(
            channel="historyrecapped",
            format="auto",
            topic="Battle of Thermopylae",
            source_kind="manual_backlog",
            source_ref="historyrecapped/narrations/thermo.json",
            length_s=55,
        )

    def test_sim_path_enqueues_task(self):
        from control.core import cloud_run as cr_mod
        from control.core.queue import get_queue
        proposal = self._make_proposal()
        with patch.object(cr_mod, "render_backend", return_value="sim"):
            resp = jobs_mod._enqueue_render_job(proposal)
        self.assertIsNotNone(resp.job_id)
        # Task should be in the queue.
        q = get_queue()
        tasks = list(q._tasks.values())
        self.assertEqual(len(tasks), 1)

    def test_cloudrun_path_triggers_job(self):
        from control.core import cloud_run as cr_mod
        proposal = self._make_proposal()
        mock_ref = cr_mod.ExecutionRef(
            job_id="x", execution_name="exec-001", triggered_via="sdk"
        )
        with patch.object(cr_mod, "render_backend", return_value="cloudrun"):
            with patch.object(cr_mod, "trigger_render_job", return_value=mock_ref):
                resp = jobs_mod._enqueue_render_job(proposal)
        self.assertIsNotNone(resp.job_id)
        doc = jobs_mod.get_job(resp.job_id)
        self.assertEqual(doc["stage"], "dispatching")

    def test_cloudrun_path_marks_failed_on_exception(self):
        from control.core import cloud_run as cr_mod
        proposal = self._make_proposal()
        with patch.object(cr_mod, "render_backend", return_value="cloudrun"):
            with patch.object(cr_mod, "trigger_render_job", side_effect=RuntimeError("cloud down")):
                resp = jobs_mod._enqueue_render_job(proposal)
        doc = jobs_mod.get_job(resp.job_id)
        self.assertEqual(doc["status"], jobs_mod.STATUS_FAILED)
        self.assertIn("dispatch", doc["stage"])

    def test_laptop_path_enqueues_task(self):
        """laptop backend is same as sim — drops on queue."""
        from control.core import cloud_run as cr_mod
        from control.core.queue import get_queue
        proposal = self._make_proposal()
        with patch.object(cr_mod, "render_backend", return_value="laptop"):
            resp = jobs_mod._enqueue_render_job(proposal)
        self.assertIsNotNone(resp.job_id)
        q = get_queue()
        tasks = list(q._tasks.values())
        self.assertEqual(len(tasks), 1)
