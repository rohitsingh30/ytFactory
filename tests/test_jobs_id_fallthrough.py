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
            "[3/4] cloudrun_z_image_turbo: generating 12 images\n"
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
        # Audit Q2.38 — path-traversal containment requires the served
        # mp4 to be under PROJECT_ROOT or YTFACTORY_RENDER_OUT_DIR.
        # Use the latter so the tempdir-based tests still work.
        self.tmp = Path(tempfile.mkdtemp())
        self._render_out_env = os.environ.get("YTFACTORY_RENDER_OUT_DIR")
        os.environ["YTFACTORY_RENDER_OUT_DIR"] = str(self.tmp)
        self.created: list[str] = []

    def tearDown(self):
        import shutil
        for jid in self.created:
            server.SCRIPT_JOBS.pop(jid, None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._render_out_env is None:
            os.environ.pop("YTFACTORY_RENDER_OUT_DIR", None)
        else:
            os.environ["YTFACTORY_RENDER_OUT_DIR"] = self._render_out_env

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


class ControlPlaneFallthroughTests(unittest.TestCase):
    """Third fall-through tier: ``control.core.jobs`` (Firestore).

    /api/render and chat → confirm both call
    ``control.core.jobs._enqueue_render_job``, which mints a 32-char
    ``uuid.uuid4().hex`` job_id and writes to a separate jobs collection.
    Pre-fix, ``GET /api/jobs/{32hex}`` 404'd because ``job_snapshot`` only
    checked ``JOBS`` and ``SCRIPT_JOBS`` — this is the user-reported
    failure mode (job_id ``2f049cd27b0446558a4bb641dfd498a2``).

    These tests exercise the full route via TestClient so the routing
    precedence (``@app.get`` shadowing ``include_router``) stays pinned.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        from control.core import jobs as control_jobs

        self.client = TestClient(server.app)
        self.control_jobs = control_jobs
        # Force a fresh in-memory backend per test so cases don't bleed.
        control_jobs.reset_jobs()
        self.created: list[str] = []

    def tearDown(self):
        self.control_jobs.reset_jobs()
        for jid in self.created:
            server.JOBS.pop(jid, None)
            server.SCRIPT_JOBS.pop(jid, None)

    def _put_control_job(self, **fields):
        """Insert a job into the control-plane store via the public API."""
        import uuid
        jid = uuid.uuid4().hex  # 32 lowercase hex — the real format.
        proposal = fields.pop("proposal", {
            "channel": "mystoriesanimated",
            "format": "animated",
            "topic": "demo topic",
            "source_kind": "chat",
            "length_s": 55,
        })
        self.control_jobs.create_job(
            jid,
            channel=fields.pop("channel", "mystoriesanimated"),
            topic=fields.pop("topic", "demo topic"),
            proposal=proposal,
        )
        if fields:
            self.control_jobs.get_jobs().update(jid, **fields)
        self.created.append(jid)
        return jid

    # ---- 200 / shape -------------------------------------------------------

    def test_control_plane_pending_resolves(self):
        """The user's reported failure — pre-fix this 404'd."""
        jid = self._put_control_job()
        r = self.client.get(f"/api/jobs/{jid}")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["job_id"], jid)
        # New JobView keys the web-next UI binds to.
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["channel"], "mystoriesanimated")
        self.assertEqual(body["topic"], "demo topic")
        # Legacy job_snapshot keys (same response — old UIs keep working).
        self.assertEqual(body["niche"], "mystoriesanimated")
        self.assertEqual(body["slug"], "demo topic")
        self.assertEqual(body["state"], "queued")
        # Stage was set by create_job().
        self.assertEqual(body["stage"], "queued")
        # Proposal carries the ShortProposal payload as-is (NOT
        # synthesized as `from_script`).
        self.assertEqual(body["proposal"]["channel"], "mystoriesanimated")
        self.assertEqual(body["proposal"]["format"], "animated")

    def test_status_rendering_no_preview_url_yet(self):
        """Mirror control._doc_to_view: only set preview_url once the
        mp4 is actually servable (short_uri or preview_local_path is
        set on the doc) — UI shouldn't bind a <video> to a URL
        guaranteed to 404. Pre-2026-05-12 the gate was status-based
        (``status in {done, uploading}``) which lit up preview_url
        the moment the worker flipped to ``uploading`` — but the
        ``short_uri`` doesn't get populated until the GCS upload
        actually completes, so the dashboard's ``<video>`` element
        fired a noisy 404 GET in that window."""
        jid = self._put_control_job()
        self.control_jobs.mark_stage(jid, status="rendering", stage="images")
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertEqual(body["status"], "rendering")
        self.assertEqual(body["state"], "running")
        self.assertEqual(body["stage"], "images")
        self.assertIsNone(body["preview_url"])

    def test_status_uploading_without_short_uri_no_preview(self):
        """Regression for 2026-05-12: status==uploading with no
        short_uri yet must NOT emit a preview_url. Pre-fix the gate
        emitted the URL the moment the worker flipped status to
        "uploading" (line ``_update_job(status="uploading", ...)``
        in cloud/render-worker-v2/entrypoint.py) — but the GCS
        upload hadn't actually completed, so the ``preview_mp4``
        endpoint correctly returned 404. The dashboard ``<video>``
        element fetched ``/preview.mp4`` and the user saw a noisy
        404 in DevTools right at the upload stage."""
        jid = self._put_control_job()
        # Worker flips status to "uploading" BEFORE the upload finishes.
        # short_uri gets populated only when the GCS write returns.
        self.control_jobs.mark_stage(jid, status="uploading", stage="upload")
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertEqual(body["status"], "uploading")
        self.assertIsNone(
            body["preview_url"],
            "preview_url must stay None until short_uri is populated — "
            "the /preview.mp4 endpoint returns 404 otherwise",
        )

    def test_status_done_sets_preview_url_and_short_uri(self):
        jid = self._put_control_job()
        self.control_jobs.mark_done(
            jid,
            short_uri="gs://ytfactory-renders/jobs/x/out.mp4",
            youtube_url="https://youtu.be/abcd",
        )
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["state"], "done")
        self.assertEqual(body["preview_url"], f"/api/jobs/{jid}/preview.mp4")
        self.assertEqual(body["short_uri"], "gs://ytfactory-renders/jobs/x/out.mp4")
        self.assertEqual(body["youtube_url"], "https://youtu.be/abcd")

    def test_status_failed_carries_error(self):
        jid = self._put_control_job()
        self.control_jobs.mark_failed(jid, stage="render", error="boom")
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["state"], "failed")
        self.assertEqual(body["error"], "boom")
        self.assertIsNone(body["preview_url"])

    def test_iso_timestamps_from_datetimes(self):
        """control.core.jobs writes ``datetime`` objects (not epoch
        floats) — _isoformat_value must normalize them to the same
        ``…Z`` shape the legacy SCRIPT_JOBS path returns."""
        jid = self._put_control_job()
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertIsNotNone(body["created_at"])
        self.assertTrue(body["created_at"].endswith("Z"), body["created_at"])
        self.assertTrue(body["updated_at"].endswith("Z"))

    def test_timeline_passed_through(self):
        """The render-detail page renders body.timeline directly."""
        jid = self._put_control_job()
        timeline = [
            {"stage": "rewrite", "status": "done", "ts": 1700000000},
            {"stage": "images", "status": "running"},
        ]
        self.control_jobs.mark_stage(
            jid, status="rendering", stage="images", timeline=timeline
        )
        body = self.client.get(f"/api/jobs/{jid}").json()
        self.assertEqual(body["timeline"], timeline)

    # ---- 404 / 502 ---------------------------------------------------------

    def test_unknown_short_id_still_404s(self):
        """A typo'd legacy-shape ID (not 32-hex) keeps the cheap 404."""
        r = self.client.get("/api/jobs/short_typo")
        self.assertEqual(r.status_code, 404)

    def test_unknown_control_shape_id_404s(self):
        """An unknown but 32-hex ID also 404s when Firestore's healthy
        — only Firestore *failures* surface as 502."""
        import uuid
        r = self.client.get(f"/api/jobs/{uuid.uuid4().hex}")
        self.assertEqual(r.status_code, 404)

    def test_control_lookup_failure_502s_for_control_shape_id(self):
        """If the backing store throws (e.g. Firestore outage), surface
        502 for IDs that LOOK control-plane so the operator can tell
        the difference between "unknown job" and "lookup broken"."""
        import uuid

        class _BoomJobs:
            def get(self, _job_id):
                raise RuntimeError("firestore unavailable")

            # The other methods aren't exercised by this code path.

        original = self.control_jobs._BACKEND
        self.control_jobs._BACKEND = _BoomJobs()
        try:
            r = self.client.get(f"/api/jobs/{uuid.uuid4().hex}")
            self.assertEqual(r.status_code, 502)
            self.assertIn("control-plane job lookup failed", r.text)
        finally:
            self.control_jobs._BACKEND = original

    def test_control_lookup_failure_keeps_404_for_legacy_shape_id(self):
        """A failing Firestore must NOT turn legacy 404s (typo'd short
        IDs) into 500s — the existing contract for those IDs is 404."""

        class _BoomJobs:
            def get(self, _job_id):
                raise RuntimeError("firestore unavailable")

        original = self.control_jobs._BACKEND
        self.control_jobs._BACKEND = _BoomJobs()
        try:
            r = self.client.get("/api/jobs/short_typo")
            self.assertEqual(r.status_code, 404)
        finally:
            self.control_jobs._BACKEND = original


class IsoformatValueTests(unittest.TestCase):
    """Pin the timestamp-normalizer contract for the new fall-through."""

    def test_none_passes_through(self):
        self.assertIsNone(server._isoformat_value(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(server._isoformat_value(""))

    def test_naive_datetime_assumed_utc(self):
        import datetime as dt
        out = server._isoformat_value(dt.datetime(2026, 1, 2, 3, 4, 5))
        self.assertEqual(out, "2026-01-02T03:04:05Z")

    def test_aware_datetime_converted_to_utc(self):
        import datetime as dt
        ny = dt.timezone(dt.timedelta(hours=-5))
        out = server._isoformat_value(dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=ny))
        self.assertEqual(out, "2026-01-02T08:04:05Z")

    def test_epoch_int_normalized(self):
        out = server._isoformat_value(1700000000)
        self.assertTrue(out.endswith("Z"))
        # Sanity: same shape the legacy _isoformat returns for the same input.
        self.assertEqual(out, server._isoformat(1700000000))

    def test_epoch_float_normalized(self):
        self.assertTrue(server._isoformat_value(1700000000.5).endswith("Z"))

    def test_string_with_offset_swapped_to_z(self):
        self.assertEqual(
            server._isoformat_value("2026-01-02T03:04:05+00:00"),
            "2026-01-02T03:04:05Z",
        )


if __name__ == "__main__":
    unittest.main()
