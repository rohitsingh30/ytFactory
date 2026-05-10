"""End-to-end test of the customer happy path.

Drives:
  POST /api/render  → enqueue
  GET  /api/jobs/{id} (poll) → wait until status=done (sim mode)
  GET  /api/jobs/{id}/preview.mp4 → ensure mp4 lands
  POST /api/jobs/{id}/publish → fake YouTube URL

This test ALWAYS boots its own ephemeral control plane on a random
free port. It NEVER attaches to a developer's live control plane on
the canonical 8766 port — doing so was a 2026-05-09 regression that
polluted the dev dashboard with 20 rows of "E2E test render" each
time the auto-test rule fired after a code edit (see
docs/auto_test_rule.md).

Run:
    .venv/bin/python -m unittest tests.test_e2e_happy_path

To run against an explicitly-chosen URL (e.g., the deployed control
service for a smoke test), set ``YTFACTORY_E2E_BASE`` — only then
will the test skip booting its own server.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request


# Only set BASE from env if the operator EXPLICITLY opted in to a remote
# target. Otherwise we boot our own server on an ephemeral free port in
# setUpClass and overwrite BASE there. This is intentional: silently
# attaching to whatever happens to be on 8766 has historically meant
# polluting the developer's live in-memory dashboard.
BASE = os.environ.get("YTFACTORY_E2E_BASE", "")
TIMEOUT_S = 60  # sim mode usually completes in <15s


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _get_json(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def _get_status_code(path: str) -> int:
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.getcode()
    except urllib.error.HTTPError as e:
        return e.code


def _wait_for_status(job_id: str, target: str, *, timeout_s: int = TIMEOUT_S) -> dict:
    """Poll /api/jobs/<id> until status == target. Returns the final doc."""
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        try:
            last = _get_json(f"/api/jobs/{job_id}")
        except Exception:
            time.sleep(0.5)
            continue
        if last.get("status") == target:
            return last
        if last.get("status") == "failed":
            raise AssertionError(f"job failed: {last.get('error', '')[:300]}")
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for status={target}; last={last}")


def _is_listening(host: str, port: int) -> bool:
    s = socket.socket()
    try:
        s.settimeout(0.3)
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class HappyPathE2ETest(unittest.TestCase):
    """End-to-end against a live control plane."""

    @classmethod
    def setUpClass(cls):
        global BASE
        # Operator opted into a remote URL — use it as-is, no boot.
        if BASE:
            cls._spawned = False
            return

        # Always boot our own server on an ephemeral port. We DO NOT
        # probe 127.0.0.1:8766 — that is the developer's live control
        # plane and we must never write into it (regression 2026-05-09).
        host = "127.0.0.1"
        port = _free_port()
        BASE = f"http://{host}:{port}"

        os.environ.setdefault("YTFACTORY_AGENT_TOKEN", "dev-token")
        os.environ.setdefault("YTFACTORY_QUEUE_BACKEND", "memory")
        os.environ.setdefault("YTFACTORY_SIM_WORKER", "1")
        os.environ.setdefault("YTFACTORY_SIM_SPEED", "0.4")
        # Force the sim render backend even if a sibling test left
        # ``YTFACTORY_RENDER_BACKEND=cloudrun`` in the environ — the
        # e2e test boots its own in-process server with no Cloud Run
        # connectivity, so a real cloudrun dispatch would hang the
        # job in `dispatching` forever.
        cls._prev_render_backend = os.environ.get("YTFACTORY_RENDER_BACKEND")
        os.environ["YTFACTORY_RENDER_BACKEND"] = "sim"

        import uvicorn  # noqa: PLC0415

        # Force a fresh in-memory jobs backend for this test process so
        # we don't share state with anything else that imported control.
        from control.core import jobs as jobs_mod  # noqa: PLC0415
        jobs_mod.reset_jobs()

        from control.server_dev import app  # noqa: PLC0415

        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        cls._server = uvicorn.Server(config)
        cls._thread = threading.Thread(target=cls._server.run, daemon=True)
        cls._thread.start()
        # Wait for boot.
        for _ in range(40):
            if _is_listening(host, port):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("control plane failed to boot for e2e test")
        cls._spawned = True

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_spawned", False):
            cls._server.should_exit = True
        # Restore prior YTFACTORY_RENDER_BACKEND so the test doesn't
        # itself become a polluter for later tests that probe the
        # cloudrun dispatch path.
        prev = getattr(cls, "_prev_render_backend", None)
        if prev is None:
            os.environ.pop("YTFACTORY_RENDER_BACKEND", None)
        else:
            os.environ["YTFACTORY_RENDER_BACKEND"] = prev

    def test_health_ok(self):
        h = _get_json("/api/health")
        self.assertTrue(h["ok"])
        # Sim worker is the dev default; it must be running.
        self.assertEqual(h.get("sim_worker", {}).get("running"), True)

    def test_channels_listed(self):
        d = _get_json("/api/channels")
        # 7 production channels as of 2026-05-09. Use >=7 so adding a
        # new channel doesn't immediately break this test.
        self.assertGreaterEqual(len(d["channels"]), 7)
        keys = {c["key"] for c in d["channels"]}
        self.assertIn("mystoriesanimated", keys)
        self.assertIn("hindutavaanimated", keys)

    def test_customization_schema(self):
        s = _get_json("/api/channels/mystoriesanimated/customization_schema")
        self.assertEqual(s["channel"], "mystoriesanimated")
        # Required fields baseline
        keys = {f["key"] for f in s["fields"]}
        self.assertIn("topic", keys)
        self.assertIn("voice", keys)
        self.assertIn("length_s", keys)

    def test_render_to_done_to_publish(self):
        # 1. POST /api/render
        body = {
            "channel": "historyrecapped",
            "topic": "E2E test render",
            "length_s": 35,
            "channel_overrides": {
                "voice": "sarah",
                "captions_density": "standard",
                "music_bed": "ambient_low",
            },
        }
        r = _post("/api/render", body)
        job_id = r["job_id"]
        self.assertEqual(len(job_id), 32)

        # 2. Poll until done.
        final = _wait_for_status(job_id, "done")
        self.assertIn("timeline", final)
        timeline = final.get("timeline") or []
        self.assertEqual(len(timeline), 7)
        for stage in timeline:
            self.assertEqual(stage["status"], "done", f"stage {stage['stage']} not done")

        # 3. Preview mp4 returns 200.
        code = _get_status_code(f"/api/jobs/{job_id}/preview.mp4")
        self.assertEqual(code, 200)

        # 4. Publish (sim returns fake YouTube URL).
        pub = _post(
            f"/api/jobs/{job_id}/publish",
            {"visibility": "unlisted", "title": "E2E render", "tags": ["test"]},
        )
        self.assertEqual(pub["status"], "done")
        self.assertTrue(pub["youtube_url"].startswith("https://youtu.be/sim-"))

        # 5. Library reflects it.
        listing = _get_json("/api/jobs?channel=historyrecapped")
        ids = [j["job_id"] for j in listing["jobs"]]
        self.assertIn(job_id, ids)


if __name__ == "__main__":
    unittest.main()
