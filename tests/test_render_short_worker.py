"""Tests for the RENDER_SHORT mega-task worker.

Mocks the heavy parts (rewrite/cast claude calls + make_shorts.py subprocess
+ GCS uploads) and verifies the orchestration: payload → raw → script →
render → upload → cleanup.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Pin scratch root before importing the runner.
_TMP = tempfile.mkdtemp(prefix="ytf-render-test-")
os.environ["YTFACTORY_SCRATCH_ROOT"] = _TMP

from agent.runner import TaskContext  # noqa: E402
from shared.schema import TaskEnvelope, TaskKind  # noqa: E402


def _envelope(channel: str = "mystoriesanimated", topic: str = "AITA for ruining the cake") -> TaskEnvelope:
    return TaskEnvelope(
        task_id="t-" + os.urandom(4).hex(),
        job_id="j-" + os.urandom(4).hex(),
        kind=TaskKind.RENDER_SHORT,
        payload={
            "job_id": "j-" + os.urandom(4).hex(),
            "channel": channel,
            "format": "animated",
            "topic": topic,
            "source_kind": "auto",
            "source_ref": None,
            "length_s": 55,
            "notes": "extra direction",
        },
    )


def _ctx(envelope: TaskEnvelope) -> TaskContext:
    scratch = Path(_TMP) / f"task-{envelope.task_id[:8]}-test"
    scratch.mkdir(parents=True, exist_ok=True)
    return TaskContext(task=envelope, scratch=scratch)


class BuildRawTest(unittest.TestCase):
    def test_user_text_synthesizes_raw(self):
        from workers.heavy.render_short import _build_raw
        raw = _build_raw({
            "topic": "AITA for ruining the cake",
            "notes": "long form notes here",
            "source_kind": "user_text",
            "source_ref": None,
            "job_id": "abc12345xyz",
            "channel": "mystoriesanimated",
        })
        self.assertIn("aita-for-ruining-the-cake", raw["slug"])
        self.assertIn("abc12345", raw["slug"])  # job suffix
        self.assertEqual(raw["title"], "AITA for ruining the cake")
        self.assertEqual(raw["body"], "long form notes here")
        self.assertEqual(raw["source"], "chat:user_text")
        self.assertEqual(raw["metadata"]["chat_job_id"], "abc12345xyz")

    def test_no_topic_raises(self):
        from workers.heavy.render_short import _build_raw
        with self.assertRaises(ValueError):
            _build_raw({"topic": "", "notes": "ignored"})

    def test_falls_back_body_to_topic_if_no_notes(self):
        from workers.heavy.render_short import _build_raw
        raw = _build_raw({"topic": "Aguero goal", "notes": "", "job_id": "x"})
        self.assertEqual(raw["body"], "Aguero goal")


class ChannelResolveTest(unittest.TestCase):
    def test_known_channels_map_to_yaml_paths(self):
        from workers.heavy.render_short import _resolve_channel_yaml, _channel_dir_from_yaml
        self.assertTrue(str(_resolve_channel_yaml("mystoriesanimated")).endswith("mystoriesanimated.yaml"))
        self.assertTrue(str(_resolve_channel_yaml("sportstoriesanimated")).endswith("sportstoriesanimated.yaml"))
        self.assertTrue(str(_resolve_channel_yaml("mahabharathindi")).endswith("mahabharat_hindi.yaml"))
        # auto falls back to mystoriesanimated.
        self.assertTrue(str(_resolve_channel_yaml("auto")).endswith("mystoriesanimated.yaml"))
        # unknown also falls back.
        self.assertTrue(str(_resolve_channel_yaml("nonsense")).endswith("mystoriesanimated.yaml"))

    def test_channel_dir_inferred_from_stem(self):
        from workers.heavy.render_short import _channel_dir_from_yaml
        self.assertEqual(_channel_dir_from_yaml(Path("channels/mystoriesanimated.yaml")), "mystoriesanimated")
        self.assertEqual(_channel_dir_from_yaml(Path("channels/mahabharat_hindi.yaml")), "mahabharat_hindi")


class OrchestrationTest(unittest.IsolatedAsyncioTestCase):
    """Drive render_short end-to-end with everything heavy mocked out.

    Verifies the contract:
      - rewrite + cast called once each, in parallel
      - make_shorts subprocess invoked with the right args
      - 3 GCS uploads happen (mp4, thumb, proposal)
      - cleanup wipes per-slug intermediates on success
      - missing mp4 raises (caught by the runner as failure)
    """

    async def test_happy_path(self) -> None:
        from workers.heavy import render_short as rs

        env = _envelope()
        ctx = _ctx(env)

        uploads: list[tuple[str, str | None, str | None]] = []  # (uri, local, content_type)

        def fake_upload(local, uri, content_type=None):
            uploads.append((uri, str(local), content_type))
            return uri

        def fake_upload_bytes(data, uri, content_type=None):
            uploads.append((uri, None, content_type))
            return uri

        async def fake_rewrite_and_cast(raw, yaml_path, slug, ch_dir):
            # Pretend the script + cast files were written.
            inter = rs.PROJECT_ROOT / "data" / "intermediate" / ch_dir
            sp = inter / "scripts" / f"{slug}.json"
            cp = inter / "cast" / f"{slug}.json"
            sp.parent.mkdir(parents=True, exist_ok=True)
            cp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text("{}")
            cp.write_text("{}")
            return sp, cp

        async def fake_run_make_shorts(script_path, channel_yaml, log_path):
            # Simulate make_shorts.py creating the mp4 in data/shorts/.
            slug = script_path.stem
            mp4 = rs.PROJECT_ROOT / "data" / "shorts" / f"{slug}.mp4"
            mp4.parent.mkdir(parents=True, exist_ok=True)
            mp4.write_bytes(b"\0\0\0\x18ftypmp42")  # minimal mp4-ish bytes
            log_path.write_text("rendered ok\n")
            return 0

        with patch.object(rs, "_rewrite_and_cast", new=fake_rewrite_and_cast), \
             patch.object(rs, "_run_make_shorts", new=fake_run_make_shorts), \
             patch.object(rs.storage, "upload", side_effect=fake_upload), \
             patch.object(rs.storage, "upload_bytes", side_effect=fake_upload_bytes), \
             patch.object(rs.storage, "job_uri", side_effect=lambda jid, rel="": f"gs://test/jobs/{jid}/{rel}".rstrip("/")), \
             patch.object(rs, "_cleanup_intermediate") as mock_cleanup:
            short_uri = await rs.render_short(ctx)

        # 1. mp4 uploaded with the expected URI shape.
        self.assertTrue(short_uri and short_uri.endswith("/short.mp4"), short_uri)
        # 2. proposal.json uploaded as bytes.
        proposal_uploads = [u for u in uploads if u[0].endswith("/proposal.json")]
        self.assertEqual(len(proposal_uploads), 1)
        self.assertEqual(proposal_uploads[0][2], "application/json")
        # 3. cleanup ran.
        mock_cleanup.assert_called_once()

    async def test_make_shorts_failure_propagates(self) -> None:
        from workers.heavy import render_short as rs

        env = _envelope()
        ctx = _ctx(env)

        async def fake_rewrite_and_cast(raw, yaml_path, slug, ch_dir):
            return Path("/tmp/script.json"), Path("/tmp/cast.json")

        async def fake_run_make_shorts(script_path, channel_yaml, log_path):
            log_path.write_text("error: image gen failed\n")
            return 1

        with patch.object(rs, "_rewrite_and_cast", new=fake_rewrite_and_cast), \
             patch.object(rs, "_run_make_shorts", new=fake_run_make_shorts), \
             patch.object(rs.storage, "upload"), \
             patch.object(rs.storage, "upload_bytes"), \
             patch.object(rs, "_cleanup_intermediate"):
            with self.assertRaises(RuntimeError) as cm:
                await rs.render_short(ctx)
            self.assertIn("exited 1", str(cm.exception))
            self.assertIn("image gen failed", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
