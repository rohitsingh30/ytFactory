"""Tests for the /api/jobs/{id} → SCRIPT_JOBS fall-through (2026-05-10).

Pins the bug-fix contract: a render submitted via /api/jobs/from_script
(every `/make-*` skill) must be visible at GET /api/jobs/{id} so the
new web-next render-detail page (`/app/render/<id>`) can poll one
endpoint regardless of submission origin. Without the fall-through,
every skill-rendered job 404s the moment its UI page loads — which is
the exact failure the user reported.

Also exercises the snapshot adapter (_script_job_to_snapshot) so the
returned shape carries both the legacy job_snapshot keys and the new
JobView keys the web-next UI binds to.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from tests._helpers import PROJECT_ROOT  # noqa: F401

from web import server


class JobsIdFallthroughTests(unittest.TestCase):
    """End-to-end via TestClient — verify the routing.

    Each test uses a unique job_id so they're hermetic against each other
    and against any other tests in the same module that touch SCRIPT_JOBS.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(server.app)
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "render.log"
        self.log.write_text(
            "[1/4] TTS cached\n"
            "[2/4] beats cached\n"
            "[3/4] cloudrun_flux2_klein: generating 12 images\n"
            "[image-done] beat 4 of 12\n"
        )
        self.created: list[str] = []

    def tearDown(self):
        import shutil
        for jid in self.created:
            server.SCRIPT_JOBS.pop(jid, None)
            server.JOBS.pop(jid, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _put_script_job(self, job_id: str, **overrides):
        rec = {
            "job_id": job_id,
            "label": "test render",
            "cmd": [
                "scripts/make_shorts.py",
                "--channel", "mystoriesanimated/variants/aita_animated.yaml",
                "--script", "mystoriesanimated/reddit_amitheasshole/scripts/aita99.json",
            ],
            "state": "running",
            "started_at": time.time() - 30,
            "completed_at": None,
            "exit_code": None,
            "mp4_path": None,
            "error": None,
            "log_path": str(self.log),
            "backend": "local",
        }
        rec.update(overrides)
        server.SCRIPT_JOBS[job_id] = rec
        self.created.append(job_id)
        return rec

    # ---- 404 baseline ------------------------------------------------------

    def test_unknown_job_id_404s(self):
        r = self.client.get("/api/jobs/no_such_job_id")
        self.assertEqual(r.status_code, 404)

    # ---- Fall-through to SCRIPT_JOBS ---------------------------------------

    def test_skill_submitted_job_resolves_via_fallthrough(self):
        """The user's reported failure mode — pre-fix this 404'd."""
        jid = "fallt_skill_" + str(int(time.time() * 1000))[-6:]
        self._put_script_job(jid, state="running")
        r = self.client.get(f"/api/jobs/{jid}")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["job_id"], jid)
        # New JobView keys the web-next UI binds to.
        self.assertEqual(body["status"], "rendering")
        self.assertEqual(body["channel"], "mystoriesanimated")
        self.assertEqual(body["topic"], "aita99")
        # Legacy job_snapshot keys (same response — old UIs keep working).
        self.assertEqual(body["state"], "running")
        self.assertEqual(body["niche"], "mystoriesanimated")
        self.assertEqual(body["slug"], "aita99")

    def test_done_state_maps_to_status_done(self):
        jid = "fallt_done_" + str(int(time.time() * 1000))[-6:]
        self._put_script_job(jid, state="done", mp4_path="/tmp/out.mp4",
                             completed_at=time.time())
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["preview_url"], f"/api/jobs/{jid}/short")

    def test_done_no_mp4_found_maps_to_status_done(self):
        jid = "fallt_nomp4_" + str(int(time.time() * 1000))[-6:]
        self._put_script_job(jid, state="done_no_mp4_found",
                             completed_at=time.time())
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertEqual(body["status"], "done")
        self.assertIsNone(body["preview_url"])
        self.assertIsNone(body["short_uri"])

    def test_failed_state_carries_error(self):
        jid = "fallt_fail_" + str(int(time.time() * 1000))[-6:]
        self._put_script_job(jid, state="failed",
                             error="renderer exit_code=1",
                             completed_at=time.time())
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "renderer exit_code=1")

    def test_gs_uri_surfaces_as_short_uri(self):
        """Cloud Run backend renders to GCS; the snapshot must expose
        both the gs:// URI (for callers that resolve it themselves) and
        the proxied /short URL (for callers that just want a URL)."""
        jid = "fallt_gs_" + str(int(time.time() * 1000))[-6:]
        self._put_script_job(jid, state="done",
                             mp4_path="gs://bucket/jobs/x/out.mp4",
                             completed_at=time.time())
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertEqual(body["short_uri"], "gs://bucket/jobs/x/out.mp4")
        self.assertEqual(body["preview_url"], f"/api/jobs/{jid}/short")

    def test_log_tail_included_in_snapshot(self):
        """The new render-detail page expects log_tail inline so it can
        render the live trail without a second round-trip."""
        jid = "fallt_log_" + str(int(time.time() * 1000))[-6:]
        self._put_script_job(jid, state="running")
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertIn("[3/4]", body["log_tail"])
        self.assertIn("beat 4 of 12", body["log_tail"])

    def test_proposal_carries_from_script_metadata(self):
        jid = "fallt_meta_" + str(int(time.time() * 1000))[-6:]
        self._put_script_job(jid, state="running",
                             cloudrun_execution="exec-abc",
                             spec_uri="gs://bucket/jobs/x/spec.json",
                             backend="cloudrun")
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertTrue(body["proposal"]["from_script"])
        self.assertEqual(body["proposal"]["backend"], "cloudrun")
        self.assertEqual(body["proposal"]["cloudrun_execution"], "exec-abc")
        self.assertIn("progress", body["proposal"])

    def test_iso_timestamps(self):
        jid = "fallt_ts_" + str(int(time.time() * 1000))[-6:]
        started = time.time() - 60
        completed = time.time()
        self._put_script_job(jid, state="done", started_at=started,
                             completed_at=completed, mp4_path="/tmp/x.mp4")
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertTrue(body["created_at"].endswith("Z"))
        self.assertTrue(body["updated_at"].endswith("Z"))


class ChannelTopicParseTests(unittest.TestCase):
    """Pin the channel/topic best-effort parse from the renderer cmd."""

    def test_extracts_channel_and_topic(self):
        cmd = [
            "scripts/make_shorts.py",
            "--channel", "mystoriesanimated/variants/aita_animated.yaml",
            "--script", "mystoriesanimated/reddit_amitheasshole/scripts/aita17.json",
        ]
        ch, topic = server._channel_topic_from_cmd(cmd)
        self.assertEqual(ch, "mystoriesanimated")
        self.assertEqual(topic, "aita17")

    def test_handles_unknown_layout_gracefully(self):
        # A non-shorts entry (long_form / sports_doc) — same flag shape.
        cmd = [
            "scripts/render_long_form.py",
            "--channel", "cosmosdecoded/config.yaml",
            "--script", "cosmosdecoded/narrations/eddington_1919.json",
        ]
        ch, topic = server._channel_topic_from_cmd(cmd)
        self.assertEqual(ch, "cosmosdecoded")
        self.assertEqual(topic, "eddington_1919")

    def test_missing_flags_returns_nones(self):
        # Out-of-tree cmd — both Nones, no exception.
        ch, topic = server._channel_topic_from_cmd([
            "scripts/something.py", "--foo", "bar"
        ])
        self.assertIsNone(ch)
        self.assertIsNone(topic)

    def test_empty_cmd(self):
        ch, topic = server._channel_topic_from_cmd([])
        self.assertIsNone(ch)
        self.assertIsNone(topic)

    def test_none_cmd(self):
        ch, topic = server._channel_topic_from_cmd(None)
        self.assertIsNone(ch)
        self.assertIsNone(topic)


class ShortMp4FallthroughTests(unittest.TestCase):
    """`GET /api/jobs/{id}/short` falls through to SCRIPT_JOBS too.

    Local mp4_path → FileResponse; gs:// mp4_path → 302 to a signed URL.
    Tests cover the no-mp4 and missing-file edges.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(server.app)
        self.tmp = Path(tempfile.mkdtemp())
        self.created: list[str] = []

    def tearDown(self):
        import shutil
        for jid in self.created:
            server.SCRIPT_JOBS.pop(jid, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_local_mp4_is_served(self):
        mp4 = self.tmp / "render.mp4"
        mp4.write_bytes(b"\x00" * 64)
        jid = "short_local_" + str(int(time.time() * 1000))[-6:]
        server.SCRIPT_JOBS[jid] = {
            "job_id": jid,
            "state": "done",
            "started_at": time.time() - 30,
            "completed_at": time.time(),
            "mp4_path": str(mp4),
            "log_path": None,
        }
        self.created.append(jid)
        r = self.client.get(f"/api/jobs/{jid}/short")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "video/mp4")
        self.assertEqual(r.content, b"\x00" * 64)

    def test_missing_local_file_404s(self):
        jid = "short_missing_" + str(int(time.time() * 1000))[-6:]
        server.SCRIPT_JOBS[jid] = {
            "job_id": jid,
            "state": "done",
            "mp4_path": "/no/such/file.mp4",
            "log_path": None,
            "started_at": time.time() - 30,
        }
        self.created.append(jid)
        r = self.client.get(f"/api/jobs/{jid}/short")
        self.assertEqual(r.status_code, 404)

    def test_no_mp4_path_404s(self):
        jid = "short_nomp4_" + str(int(time.time() * 1000))[-6:]
        server.SCRIPT_JOBS[jid] = {
            "job_id": jid,
            "state": "running",
            "mp4_path": None,
            "log_path": None,
            "started_at": time.time(),
        }
        self.created.append(jid)
        r = self.client.get(f"/api/jobs/{jid}/short")
        self.assertEqual(r.status_code, 404)

    def test_unknown_id_404s(self):
        r = self.client.get("/api/jobs/no_such/short")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
