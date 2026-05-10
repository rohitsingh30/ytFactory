"""Tests for the /api/jobs/from_script render-progress + mtime gate.

Covers the 2026-05-09 instant-done bug: skill-driven render jobs were
flipping straight from "running" to "done" pointing at a stale prior
mp4 because (a) `_resolve_mp4_for_script_job` matched any same-slug
mp4 on disk, and (b) the API exposed no per-stage progress so users
saw no progression.

These tests pin the helper contracts so the bug can't regress:

  * `_resolve_mp4_for_script_job` honours `min_mtime` — a stale mp4
    older than the job's start is ignored.
  * `_parse_script_job_progress` walks the renderer's `[N/4]` and
    `[image-done] beat I of N` markers and surfaces phase + percent
    + per-image counter.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from tests._helpers import PROJECT_ROOT  # noqa: F401

from web import server


class ResolveMp4Tests(unittest.TestCase):
    """`_resolve_mp4_for_script_job` mtime-gate behaviour."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Layout: <tmp>/channel/<niche>/shorts/<slug>.mp4
        self.shorts = self.tmp / "channel" / "niche" / "shorts"
        self.shorts.mkdir(parents=True)
        self.script = self.tmp / "channel" / "niche" / "scripts" / "myslug.json"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("{}")
        self.mp4 = self.shorts / "myslug.mp4"
        self.mp4.write_bytes(b"\x00" * 16)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_min_mtime_returns_existing_mp4(self):
        """Backwards-compat: callers that don't pass min_mtime get
        the legacy any-existing-mp4-wins behaviour."""
        out = server._resolve_mp4_for_script_job([str(self.script)])
        self.assertEqual(out, str(self.mp4))

    def test_stale_mp4_is_skipped(self):
        """The instant-done bug: an mp4 older than the job's start
        must NOT be returned, even though its filename matches."""
        # mp4 mtime = 2 hours ago
        old = time.time() - 7200
        os.utime(self.mp4, (old, old))
        # Job started 1 hour ago — mp4 is staler than that.
        started = time.time() - 3600
        out = server._resolve_mp4_for_script_job(
            [str(self.script)], min_mtime=started
        )
        self.assertIsNone(out)

    def test_fresh_mp4_within_tolerance_is_returned(self):
        """An mp4 written WHILE the job ran (mtime ≈ started_at) must
        be accepted — the 30s tolerance covers small clock drift /
        cache-hit fast paths where compose still rewrites the file."""
        started = time.time() - 5  # 5s ago
        # Touch mp4 to "now" — clearly newer than started.
        out = server._resolve_mp4_for_script_job(
            [str(self.script)], min_mtime=started
        )
        self.assertEqual(out, str(self.mp4))

    def test_explicit_tolerance_window(self):
        """Pre-job-start mp4 within the tolerance window still counts —
        prevents flaky failures when the renderer touches the mp4 a
        few hundred ms before our recorded started_at."""
        started = time.time()
        # mp4 mtime is 10s before started — within 30s default tolerance.
        os.utime(self.mp4, (started - 10, started - 10))
        out = server._resolve_mp4_for_script_job(
            [str(self.script)], min_mtime=started
        )
        self.assertEqual(out, str(self.mp4))


class ParseProgressTests(unittest.TestCase):
    """`_parse_script_job_progress` log-tail parser."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "render.log"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, text: str) -> None:
        self.log.write_text(text)

    def test_no_log_returns_starting(self):
        out = server._parse_script_job_progress(None)
        self.assertEqual(out["phase"], "starting")
        self.assertEqual(out["phase_index"], 0)
        self.assertEqual(out["phase_total"], 4)
        self.assertEqual(out["percent"], 0)

    def test_missing_log_file_is_safe(self):
        out = server._parse_script_job_progress(str(self.tmp / "nope.log"))
        self.assertEqual(out["phase"], "starting")

    def test_phase_1_tts(self):
        self._write("[warmup] cloudrun ok\n[1/4] TTS (cloudrun_chatterbox)…\n")
        out = server._parse_script_job_progress(str(self.log))
        self.assertEqual(out["phase"], "tts")
        self.assertEqual(out["phase_index"], 1)
        # Conservative: entered phase but not finished — 0% of total.
        self.assertEqual(out["percent"], 0)

    def test_phase_3_with_image_total_only(self):
        self._write(
            "[1/4] TTS cached\n"
            "[2/4] beats cached\n"
            "[3/4] cloudrun_flux2_klein: generating 11 images (custom prompts)…\n"
        )
        out = server._parse_script_job_progress(str(self.log))
        self.assertEqual(out["phase"], "images")
        self.assertEqual(out["phase_index"], 3)
        self.assertEqual(out["images_total"], 11)
        self.assertEqual(out["images_done"], 0)
        self.assertIn("0/11", out["text"])

    def test_phase_3_with_per_image_progress(self):
        lines = [
            "[1/4] TTS cached",
            "[2/4] beats cached",
            "[3/4] cloudrun_flux2_klein: generating 11 images (custom prompts)…",
            "[image-done] beat 0 of 11",
            "[image-done] beat 1 of 11",
            "[image-done] beat 2 of 11",
        ]
        self._write("\n".join(lines))
        out = server._parse_script_job_progress(str(self.log))
        self.assertEqual(out["phase"], "images")
        # beat 2 of 11 means we've finished the 3rd image (0-indexed → +1).
        self.assertEqual(out["images_done"], 3)
        self.assertEqual(out["images_total"], 11)
        self.assertIn("3/11", out["text"])
        # Percent should be inside phase 3's slot (50–75%).
        self.assertGreater(out["percent"], 50)
        self.assertLess(out["percent"], 75)

    def test_phase_4_compose(self):
        self._write(
            "[1/4] TTS cached\n"
            "[2/4] beats cached\n"
            "[3/4] cloudrun_flux2_klein: generating 11 images\n"
            "[image-done] beat 10 of 11\n"
            "[4/4] ffmpeg compose (slideshow)…\n"
        )
        out = server._parse_script_job_progress(str(self.log))
        self.assertEqual(out["phase"], "compose")
        self.assertEqual(out["phase_index"], 4)

    def test_done_state_jumps_to_100(self):
        self._write("[1/4] TTS cached\n[2/4] beats cached\n")
        out = server._parse_script_job_progress(str(self.log), state="done")
        self.assertEqual(out["percent"], 100)
        self.assertEqual(out["text"], "Done")
        self.assertEqual(out["phase_index"], 4)

    def test_failed_state_freezes_at_last_phase(self):
        self._write(
            "[1/4] TTS cached\n"
            "[2/4] beats cached\n"
            "[3/4] cloudrun_flux2_klein: generating 11 images\n"
            "Traceback (most recent call last):\n"
        )
        out = server._parse_script_job_progress(str(self.log), state="failed")
        self.assertEqual(out["phase"], "images")
        self.assertIn("Failed", out["text"])

    def test_re_emitted_image_phase_resets_counter(self):
        """Critic-patch path can re-emit `[3/4] generating N images` with
        a smaller N. The counter should reset so the UI doesn't show
        impossible counts (e.g. 11/3)."""
        lines = [
            "[1/4] TTS cached",
            "[2/4] beats cached",
            "[3/4] cloudrun_flux2_klein: generating 11 images",
            "[image-done] beat 10 of 11",
            "[3/4] cloudrun_flux2_klein: generating 3 images",
            "[image-done] beat 0 of 3",
        ]
        self._write("\n".join(lines))
        out = server._parse_script_job_progress(str(self.log))
        self.assertEqual(out["images_total"], 3)
        self.assertEqual(out["images_done"], 1)


class GetScriptJobProgressIntegrationTests(unittest.TestCase):
    """End-to-end via TestClient: progress travels through the API.

    Pins the contract that `GET /api/jobs/from_script/{id}` and
    `/api/overview`'s recent_renders both surface the parsed progress
    block — that's what the UI's stage strip binds to.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(server.app)
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "render.log"
        self.log.write_text(
            "[1/4] TTS cached\n"
            "[2/4] beats cached\n"
            "[3/4] cloudrun_flux2_klein: generating 11 images\n"
            "[image-done] beat 4 of 11\n"
        )
        self.job_id = "ittest" + str(int(time.time()))[-4:]
        server.SCRIPT_JOBS[self.job_id] = {
            "job_id": self.job_id,
            "label": "integration test",
            "cmd": ["scripts/make_shorts.py"],
            "state": "running",
            "started_at": time.time(),
            "completed_at": None,
            "exit_code": None,
            "mp4_path": None,
            "error": None,
            "log_path": str(self.log),
            "backend": "local",
        }

    def tearDown(self):
        import shutil
        server.SCRIPT_JOBS.pop(self.job_id, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_endpoint_includes_progress(self):
        r = self.client.get(f"/api/jobs/from_script/{self.job_id}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("progress", body)
        self.assertEqual(body["progress"]["phase"], "images")
        self.assertEqual(body["progress"]["images_done"], 5)
        self.assertEqual(body["progress"]["images_total"], 11)
        self.assertGreater(body["progress"]["percent"], 50)

    def test_list_endpoint_includes_progress(self):
        r = self.client.get("/api/jobs/from_script?limit=20")
        self.assertEqual(r.status_code, 200)
        jobs = {j["job_id"]: j for j in r.json()["jobs"]}
        self.assertIn(self.job_id, jobs)
        self.assertEqual(jobs[self.job_id]["progress"]["phase"], "images")


if __name__ == "__main__":
    unittest.main()
