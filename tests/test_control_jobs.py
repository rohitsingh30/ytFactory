"""Tests for control/jobs.py — JobEnvelope state store (memory backend)."""
from __future__ import annotations

import os
import unittest

os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"

from control import jobs as jobs_mod  # noqa: E402


class JobLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        jobs_mod.reset_jobs()

    def test_create_job_initial_fields(self):
        jobs_mod.create_job("j1", channel="sportstoriesanimated",
                            topic="Aguero 93:20", proposal={"length_s": 55})
        doc = jobs_mod.get_job("j1")
        self.assertIsNotNone(doc)
        assert doc is not None
        self.assertEqual(doc["job_id"], "j1")
        self.assertEqual(doc["channel"], "sportstoriesanimated")
        self.assertEqual(doc["topic"], "Aguero 93:20")
        self.assertEqual(doc["status"], jobs_mod.STATUS_PENDING)
        self.assertEqual(doc["stage"], "queued")
        self.assertIsNone(doc.get("owner_uid"))
        # Timestamps populated.
        self.assertIn("created_at", doc)
        self.assertIn("updated_at", doc)

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
