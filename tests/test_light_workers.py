"""Tests for the light workers — youtube_upload + research_handoff.

Heavy modules (pipeline.upload, pipeline.research, pipeline.youtube_stats)
+ Firestore + GCS are all mocked.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"
_TMP = tempfile.mkdtemp(prefix="ytf-light-test-")
os.environ["YTFACTORY_SCRATCH_ROOT"] = _TMP

from agent.runner import TaskContext  # noqa: E402
from control.queue import get_queue, reset_queue  # noqa: E402
from shared.schema import TaskEnvelope, TaskKind, TaskStatus  # noqa: E402


def _ctx(kind: TaskKind, payload: dict) -> TaskContext:
    env = TaskEnvelope(
        task_id="t-" + os.urandom(4).hex(),
        job_id=payload.get("job_id") or "j-" + os.urandom(4).hex(),
        kind=kind,
        payload=payload,
    )
    scratch = Path(_TMP) / f"task-{env.task_id[:8]}"
    scratch.mkdir(parents=True, exist_ok=True)
    return TaskContext(task=env, scratch=scratch)


class YoutubeUploadTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_queue()

    async def test_happy_path_enqueues_research_handoff_and_gcs(self) -> None:
        from workers.light import youtube_upload as yu

        ctx = _ctx(TaskKind.YOUTUBE_UPLOAD, {
            "job_id": "job-abc",
            "channel": "mystoriesanimated",
            "topic": "AITA test topic",
            "notes": "extra notes",
            "slug": "aita-test-topic-jobabc",
            "short_uri": "gs://test/jobs/job-abc/short.mp4",
        })

        gc_calls: list[str] = []

        def fake_download(uri, local):
            Path(local).write_bytes(b"\0\0\0\x18ftypmp42")
            return Path(local)

        async def fake_do_upload(mod, mp4_path, title, description, channel):
            return "VID12345"

        with patch.object(yu.storage, "download", side_effect=fake_download), \
             patch.object(yu, "_do_youtube_upload", new=fake_do_upload), \
             patch.object(yu.storage, "gc_heavy_artifacts",
                          side_effect=lambda jid, dry_run=False: (gc_calls.append(jid) or [f"gs://test/jobs/{jid}/x"])), \
             patch.object(yu, "_record_published"):
            url = await yu.youtube_upload(ctx)

        self.assertEqual(url, "https://youtu.be/VID12345")
        self.assertEqual(gc_calls, ["job-abc"])

        # Successor task should now be in the queue.
        q = get_queue()
        # Find any RESEARCH_HANDOFF task we enqueued.
        from control.queue import InMemoryQueue
        assert isinstance(q, InMemoryQueue)
        handoffs = [t for t in q._tasks.values() if t.kind == TaskKind.RESEARCH_HANDOFF]  # noqa: SLF001
        self.assertEqual(len(handoffs), 1)
        h = handoffs[0]
        self.assertEqual(h.job_id, "job-abc")
        self.assertEqual(h.payload["youtube_video_id"], "VID12345")
        self.assertEqual(h.payload["youtube_url"], "https://youtu.be/VID12345")
        self.assertEqual(h.status, TaskStatus.QUEUED)

    async def test_missing_short_uri_raises(self) -> None:
        from workers.light import youtube_upload as yu
        ctx = _ctx(TaskKind.YOUTUBE_UPLOAD, {"job_id": "j", "channel": "auto"})
        with self.assertRaises(RuntimeError) as cm:
            await yu.youtube_upload(ctx)
        self.assertIn("short_uri", str(cm.exception))


class ResearchHandoffTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_video_id_returns_none_and_doesnt_crash(self) -> None:
        from workers.light import research_handoff as rh
        ctx = _ctx(TaskKind.RESEARCH_HANDOFF, {"job_id": "j"})
        out = await rh.research_handoff(ctx)
        self.assertIsNone(out)

    async def test_calls_research_rebuild_when_available(self) -> None:
        from workers.light import research_handoff as rh
        ctx = _ctx(TaskKind.RESEARCH_HANDOFF, {
            "job_id": "j-1",
            "youtube_video_id": "VID42",
            "youtube_url": "https://youtu.be/VID42",
            "channel": "mystoriesanimated",
            "slug": "test-slug",
        })

        with patch.object(rh, "_record_handoff"), \
             patch.object(rh, "_try_rebuild_research_index") as mock_rebuild, \
             patch.object(rh, "_try_fetch_initial_stats") as mock_fetch:
            url = await rh.research_handoff(ctx)

        self.assertEqual(url, "https://youtu.be/VID42")
        mock_rebuild.assert_called_once()
        mock_fetch.assert_called_once_with("VID42")

    async def test_research_failure_does_not_crash_handoff(self) -> None:
        """Even if pipeline.research blows up, the handoff returns the URL."""
        from workers.light import research_handoff as rh
        ctx = _ctx(TaskKind.RESEARCH_HANDOFF, {
            "job_id": "j-1",
            "youtube_video_id": "VID42",
            "youtube_url": "https://youtu.be/VID42",
        })

        with patch.object(rh, "_record_handoff"), \
             patch.object(rh, "_try_rebuild_research_index", side_effect=RuntimeError("boom")), \
             patch.object(rh, "_try_fetch_initial_stats"):
            # The wrappers swallow exceptions, but force-test by removing the
            # try inside _try_rebuild_research_index. The function itself
            # already wraps in try/except; here we just verify the contract
            # that rh.research_handoff returns the URL when subordinates fail.
            try:
                url = await rh.research_handoff(ctx)
                self.assertEqual(url, "https://youtu.be/VID42")
            except Exception as e:  # if it does raise, fail explicitly
                self.fail(f"handoff should not raise on subordinate failures: {e}")


if __name__ == "__main__":
    unittest.main()
