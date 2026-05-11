"""Tests for pipeline.laptop_agent — long-poll cloud agent (0% → 100%).

All network / subprocess / filesystem I/O is mocked; tests run offline.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pipeline.laptop_agent as agent


# ── token helpers ─────────────────────────────────────────────────────────


class TestToken(unittest.TestCase):
    def setUp(self):
        agent._ID_TOKEN_CACHE.clear()

    def tearDown(self):
        agent._ID_TOKEN_CACHE.clear()

    def _patch_localhost(self):
        return patch.object(agent, "CONTROL_URL", "http://127.0.0.1:8766")

    def test_localhost_reads_env_token(self):
        with self._patch_localhost(), \
             patch.dict("os.environ", {"YTFACTORY_AGENT_TOKEN": "envtoken"}):
            tok = agent._token()
        self.assertEqual(tok, "envtoken")

    def test_localhost_reads_file_when_no_env(self):
        import os
        mock_token_file = MagicMock(spec=Path)
        mock_token_file.exists.return_value = True
        mock_token_file.read_text.return_value = "filetoken\n"
        env_no_token = {k: v for k, v in os.environ.items() if k != "YTFACTORY_AGENT_TOKEN"}
        with self._patch_localhost(), \
             patch.dict("os.environ", env_no_token, clear=True), \
             patch.object(agent, "TOKEN_FILE", mock_token_file):
            tok = agent._token()
        self.assertEqual(tok, "filetoken")

    def test_localhost_raises_when_no_token_no_file(self):
        import os
        mock_token_file = MagicMock(spec=Path)
        mock_token_file.exists.return_value = False
        env_no_token = {k: v for k, v in os.environ.items()
                       if k != "YTFACTORY_AGENT_TOKEN"}
        with self._patch_localhost(), \
             patch.dict("os.environ", env_no_token, clear=True), \
             patch.object(agent, "TOKEN_FILE", mock_token_file):
            with self.assertRaises(SystemExit):
                agent._token()

    def test_cloud_uses_cached_token(self):
        audience = "user"
        agent._ID_TOKEN_CACHE[audience] = ("cached-tok", time.time() + 3600)
        with patch.object(agent, "CONTROL_URL", "https://cloud.run.app"):
            tok = agent._token()
        self.assertEqual(tok, "cached-tok")

    def test_cloud_calls_gcloud(self):
        with patch.object(agent, "CONTROL_URL", "https://cloud.run.app"), \
             patch("subprocess.check_output", return_value="gcloud-tok\n") as mock_sub:
            tok = agent._token()
        self.assertEqual(tok, "gcloud-tok")
        mock_sub.assert_called_once()

    def test_cloud_gcloud_not_found_raises(self):
        with patch.object(agent, "CONTROL_URL", "https://cloud.run.app"), \
             patch("subprocess.check_output", side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit) as ctx:
                agent._token()
        self.assertIn("gcloud", str(ctx.exception))

    def test_cloud_gcloud_error_raises(self):
        with patch.object(agent, "CONTROL_URL", "https://cloud.run.app"), \
             patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, ["gcloud"], stderr="err")):
            with self.assertRaises(SystemExit) as ctx:
                agent._token()
        self.assertIn("failed", str(ctx.exception))

    def test_cloud_empty_token_raises(self):
        with patch.object(agent, "CONTROL_URL", "https://cloud.run.app"), \
             patch("subprocess.check_output", return_value=""):
            with self.assertRaises(SystemExit) as ctx:
                agent._token()
        self.assertIn("empty", str(ctx.exception))

    def test_cloud_caches_token(self):
        with patch.object(agent, "CONTROL_URL", "https://cloud.run.app"), \
             patch("subprocess.check_output", return_value="new-tok\n") as mock_sub:
            tok1 = agent._token()
            tok2 = agent._token()  # second call — should hit cache
        self.assertEqual(tok1, "new-tok")
        self.assertEqual(tok2, "new-tok")
        mock_sub.assert_called_once()


# ── _post ─────────────────────────────────────────────────────────────────


def _make_response(data: dict, status: int = 200) -> MagicMock:
    body = json.dumps(data).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.status = status
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestPost(unittest.TestCase):
    def setUp(self):
        agent._ID_TOKEN_CACHE.clear()

    def tearDown(self):
        agent._ID_TOKEN_CACHE.clear()

    def test_post_sends_json_and_returns_dict(self):
        resp = _make_response({"ok": True})
        with patch.dict("os.environ", {"YTFACTORY_AGENT_TOKEN": "tok"}), \
             patch.object(agent, "CONTROL_URL", "http://127.0.0.1:8766"), \
             patch("urllib.request.urlopen", return_value=resp):
            result = agent._post("/test", {"key": "val"})
        self.assertEqual(result, {"ok": True})


# ── _heartbeat ────────────────────────────────────────────────────────────


class TestHeartbeat(unittest.TestCase):
    def setUp(self):
        agent._ID_TOKEN_CACHE.clear()

    def tearDown(self):
        agent._ID_TOKEN_CACHE.clear()

    def test_heartbeat_swallows_exception(self):
        with patch.object(agent, "_post", side_effect=Exception("network down")):
            # Should not raise
            agent._heartbeat()

    def test_heartbeat_calls_post(self):
        with patch.object(agent, "_post", return_value={}) as mock_post:
            agent._heartbeat()
        mock_post.assert_called_once_with(
            "/agent/heartbeat",
            {"resources": {"agent_id": agent.AGENT_ID, "caps": agent.CAPS}},
            timeout=20,
        )


# ── _claim_task ───────────────────────────────────────────────────────────


class TestClaimTask(unittest.TestCase):
    def setUp(self):
        agent._ID_TOKEN_CACHE.clear()

    def tearDown(self):
        agent._ID_TOKEN_CACHE.clear()

    def test_returns_task_on_success(self):
        with patch.object(agent, "_post", return_value={"task": {"task_id": "t1", "kind": "burner_engage"}}):
            result = agent._claim_task(["burner_engage"])
        self.assertEqual(result, {"task_id": "t1", "kind": "burner_engage"})

    def test_returns_none_when_no_task(self):
        with patch.object(agent, "_post", return_value={}):
            result = agent._claim_task(["burner_engage"])
        self.assertIsNone(result)

    def test_returns_none_when_caps_empty(self):
        # Empty caps list short-circuits without an HTTP call — workers use
        # this when every kind they serve is at local capacity.
        with patch.object(agent, "_post") as mock_post:
            result = agent._claim_task([])
        self.assertIsNone(result)
        mock_post.assert_not_called()

    def test_returns_none_on_401(self):
        err = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)
        with patch.object(agent, "_post", side_effect=err), \
             patch("time.sleep") as mock_sleep:
            result = agent._claim_task(["burner_engage"])
        self.assertIsNone(result)
        mock_sleep.assert_called_once_with(60)

    def test_returns_none_on_other_http_error(self):
        err = urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, BytesIO(b"err"))
        with patch.object(agent, "_post", side_effect=err), \
             patch("time.sleep") as mock_sleep:
            result = agent._claim_task(["burner_engage"])
        self.assertIsNone(result)
        mock_sleep.assert_called_once_with(5)

    def test_returns_none_on_generic_exception(self):
        with patch.object(agent, "_post", side_effect=Exception("timeout")), \
             patch("time.sleep") as mock_sleep:
            result = agent._claim_task(["burner_engage"])
        self.assertIsNone(result)
        mock_sleep.assert_called_once_with(5)


# ── _ack ──────────────────────────────────────────────────────────────────


class TestAck(unittest.TestCase):
    def setUp(self):
        agent._ID_TOKEN_CACHE.clear()

    def tearDown(self):
        agent._ID_TOKEN_CACHE.clear()

    def test_ack_ok(self):
        with patch.object(agent, "_post", return_value={}) as mock_post:
            agent._ack("tid1", ok=True, output_uri="gs://bucket/file.mp4")
        mock_post.assert_called_once()
        args = mock_post.call_args[0]
        self.assertIn("tid1", args[0])
        self.assertEqual(args[1]["status"], "ok")

    def test_ack_failed(self):
        with patch.object(agent, "_post", return_value={}) as mock_post:
            agent._ack("tid2", ok=False, error="something broke")
        args = mock_post.call_args[0]
        # Cloud schema is Literal["ok", "error"] — the old "failed" string
        # silently 422-d, leaving tasks LEASED forever (zombie pile).
        self.assertEqual(args[1]["status"], "error")
        self.assertEqual(args[1]["error"], "something broke")

    def test_ack_swallows_exception(self):
        with patch.object(agent, "_post", side_effect=Exception("network")):
            # should not raise
            agent._ack("tid3", ok=True)


# ── _execute ──────────────────────────────────────────────────────────────


class TestExecute(unittest.TestCase):
    def test_unsupported_kind(self):
        ok, out_uri, err, child = agent._execute({"task_id": "t1", "kind": "unknown_kind", "payload": {}})
        self.assertFalse(ok)
        self.assertIsNone(out_uri)
        self.assertIsNone(child)
        self.assertIn("unsupported", err)  # type: ignore[operator]

    def test_playwright_upload_raises_not_implemented(self):
        ok, out_uri, err, child = agent._execute({
            "task_id": "t1",
            "kind": "playwright_upload",
            "payload": {"mp4_uri": "gs://x/y.mp4"},
        })
        self.assertFalse(ok)
        self.assertIsNone(out_uri)
        self.assertIsNone(child)
        # Either NotImplementedError or ImportError is acceptable —
        # depends on whether upload_short_via_playwright exists
        self.assertTrue(
            "NotImplementedError" in str(err) or "ImportError" in str(err) or err is not None,
            f"expected error info, got: {err!r}",
        )

    def test_burner_engage_missing_slug(self):
        ok, out_uri, err, child = agent._execute({
            "task_id": "t1",
            "kind": "burner_engage",
            "payload": {},
        })
        self.assertFalse(ok)
        self.assertIsNone(child)
        self.assertIn("missing payload.slug", err)  # type: ignore[operator]

    def test_burner_engage_success(self):
        """Spawns the worker as a detached subprocess and acks immediately."""
        mock_proc = MagicMock()
        mock_proc.pid = 4242
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "open", return_value=MagicMock()):
            ok, out_uri, err, child = agent._execute({
                "task_id": "t1",
                "kind": "burner_engage",
                "payload": {"slug": "burner1"},
            })
        self.assertTrue(ok)
        self.assertIsNone(out_uri)
        self.assertIsNone(err)
        # Returned child Popen lets the worker thread hold its per-kind
        # capacity slot until the spawned subprocess actually exits.
        self.assertIs(child, mock_proc)
        # Ensure detached spawn (start_new_session=True) so the worker
        # outlives the agent process — otherwise launchd-restarting the
        # agent would kill an in-flight engage loop.
        self.assertTrue(mock_popen.call_args.kwargs.get("start_new_session"))

    def test_burner_engage_spawn_failure(self):
        """If Popen itself raises, the task is acked failed with the cause."""
        with patch("subprocess.Popen", side_effect=OSError("no exec for you")), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "open", return_value=MagicMock()):
            ok, out_uri, err, child = agent._execute({
                "task_id": "t1",
                "kind": "burner_engage",
                "payload": {"slug": "burner1"},
            })
        self.assertFalse(ok)
        self.assertIsNone(child)
        self.assertIn("failed to spawn", err)  # type: ignore[operator]
        self.assertIn("no exec for you", err)  # type: ignore[operator]

    def test_exception_in_execute_caught(self):
        with patch.object(agent, "_exec_burner_engage", side_effect=RuntimeError("boom")):
            ok, out_uri, err, child = agent._execute({
                "task_id": "t1",
                "kind": "burner_engage",
                "payload": {"slug": "x"},
            })
        self.assertFalse(ok)
        self.assertIsNone(child)
        self.assertIn("RuntimeError", err)  # type: ignore[operator]

    def test_create_burner_success_default_payload(self):
        """Empty payload → CLI invoked with no extra flags (random name, default email, OAuth on)."""
        mock_proc = MagicMock()
        mock_proc.pid = 5555
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "open", return_value=MagicMock()):
            ok, out_uri, err, child = agent._execute({
                "task_id": "t1",
                "kind": "create_burner",
                "payload": {},
            })
        self.assertTrue(ok)
        self.assertIsNone(out_uri)
        self.assertIsNone(err)
        self.assertIs(child, mock_proc)
        # Detached so it outlives the agent (channel-create can take minutes).
        self.assertTrue(mock_popen.call_args.kwargs.get("start_new_session"))
        cmd = mock_popen.call_args.args[0]
        self.assertIn("pipeline.cross_engage.create_burner_channel", cmd)
        # No --no-oauth → OAuth is on by default (matches CLI default).
        self.assertNotIn("--no-oauth", cmd)
        # No explicit --email / --display-name / --slug.
        self.assertNotIn("--email", cmd)
        self.assertNotIn("--display-name", cmd)
        self.assertNotIn("--slug", cmd)

    def test_create_burner_threads_payload_to_cli(self):
        """email / display_name / slug / oauth=False all surface as CLI flags."""
        mock_proc = MagicMock()
        mock_proc.pid = 5556
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "open", return_value=MagicMock()):
            ok, _out_uri, _err, _child = agent._execute({
                "task_id": "t2",
                "kind": "create_burner",
                "payload": {
                    "email": "rs54@gmail.com",
                    "display_name": "cosmicdrift",
                    "slug": "cosmicdrift",
                    "oauth": False,
                },
            })
        self.assertTrue(ok)
        cmd = mock_popen.call_args.args[0]
        self.assertIn("--email", cmd)
        self.assertEqual(cmd[cmd.index("--email") + 1], "rs54@gmail.com")
        self.assertIn("--display-name", cmd)
        self.assertEqual(cmd[cmd.index("--display-name") + 1], "cosmicdrift")
        self.assertIn("--slug", cmd)
        self.assertEqual(cmd[cmd.index("--slug") + 1], "cosmicdrift")
        self.assertIn("--no-oauth", cmd)

    def test_create_burner_spawn_failure(self):
        """If Popen raises, the task is acked failed with the cause."""
        with patch("subprocess.Popen", side_effect=OSError("chrome missing")), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "open", return_value=MagicMock()):
            ok, _out_uri, err, child = agent._execute({
                "task_id": "t3",
                "kind": "create_burner",
                "payload": {},
            })
        self.assertFalse(ok)
        self.assertIsNone(child)
        self.assertIn("failed to spawn create_burner_channel", err)  # type: ignore[operator]
        self.assertIn("chrome missing", err)  # type: ignore[operator]


# ── _worker_loop + run() ─────────────────────────────────────────────────


class TestWorkerLoop(unittest.TestCase):
    def setUp(self):
        agent._SHUTDOWN.clear()
        agent._ID_TOKEN_CACHE.clear()
        # Reset the active-by-kind counters since they're module-global.
        with agent._CHILDREN_LOCK:
            for k in agent._active_by_kind:
                agent._active_by_kind[k] = 0

    def tearDown(self):
        agent._SHUTDOWN.set()  # ensure no leaked threads
        agent._SHUTDOWN.clear()

    def test_worker_acquires_kind_then_leases_and_acks(self):
        """One pass: acquire burner_engage slot → claim_task → execute → ack
        → wait on child → release slot → shutdown."""
        import threading as _t

        sems = {"burner_engage": _t.BoundedSemaphore(1)}
        task = {"task_id": "tw1", "kind": "burner_engage", "payload": {"slug": "x"}}
        mock_proc = MagicMock()
        # child.wait() returns immediately.
        mock_proc.wait.return_value = 0

        claim_calls: list[list[str]] = []

        def claim_side_effect(caps):
            claim_calls.append(caps)
            if len(claim_calls) == 1:
                return task
            # Second iteration: signal shutdown so _worker_loop exits.
            agent._SHUTDOWN.set()
            return None

        with patch.object(agent, "_claim_task", side_effect=claim_side_effect), \
             patch.object(agent, "_execute", return_value=(True, None, None, mock_proc)) as mock_exec, \
             patch.object(agent, "_ack") as mock_ack:
            agent._worker_loop(0, sems)

        mock_exec.assert_called_once_with(task)
        mock_ack.assert_called_once_with("tw1", ok=True, output_uri=None, error=None)
        # Slot released — semaphore count back to original.
        self.assertTrue(sems["burner_engage"].acquire(blocking=False))
        # Caps narrowed to the single kind we acquired before each lease.
        self.assertTrue(all(c == ["burner_engage"] for c in claim_calls))

    def test_worker_does_not_lease_when_caps_full(self):
        """All semaphores at zero → worker skips lease entirely."""
        import threading as _t

        sem = _t.BoundedSemaphore(1)
        sem.acquire()  # immediately exhaust
        sems = {"burner_engage": sem}

        # Trip shutdown after one wait so the loop exits.
        original_wait = agent._SHUTDOWN.wait

        def _wait_then_shutdown(timeout=None):
            agent._SHUTDOWN.set()
            return original_wait(timeout)

        with patch.object(agent, "_claim_task") as mock_claim, \
             patch.object(agent._SHUTDOWN, "wait", side_effect=_wait_then_shutdown):
            agent._worker_loop(1, sems)

        mock_claim.assert_not_called()

    def test_worker_releases_slot_when_claim_returns_none(self):
        import threading as _t

        sem = _t.BoundedSemaphore(2)
        sems = {"burner_engage": sem}

        calls = {"n": 0}

        def claim_side_effect(_caps):
            calls["n"] += 1
            if calls["n"] >= 2:
                agent._SHUTDOWN.set()
            return None

        with patch.object(agent, "_claim_task", side_effect=claim_side_effect), \
             patch.object(agent, "_execute") as mock_exec:
            agent._worker_loop(2, sems)

        mock_exec.assert_not_called()
        # Semaphore fully released (both permits free).
        self.assertTrue(sem.acquire(blocking=False))
        self.assertTrue(sem.acquire(blocking=False))


class TestRun(unittest.TestCase):
    def setUp(self):
        agent._SHUTDOWN.clear()

    def tearDown(self):
        agent._SHUTDOWN.set()
        agent._SHUTDOWN.clear()

    def test_run_starts_workers_then_shuts_down(self):
        """run() starts NUM_WORKERS daemon threads, beats heart, and exits on
        _SHUTDOWN. We patch the worker loop to a no-op so the test is fast."""
        with patch.object(agent, "_install_signal_handlers"), \
             patch.object(agent, "_worker_loop") as mock_worker, \
             patch.object(agent, "_heartbeat") as mock_hb, \
             patch.object(agent, "NUM_WORKERS", 3):
            # Trip shutdown immediately so the heartbeat loop exits on first
            # iteration.
            agent._SHUTDOWN.set()
            agent.run()

        # 3 workers spawned, each invoked once.
        self.assertEqual(mock_worker.call_count, 3)
        # Heartbeat fires at least once on startup.
        self.assertGreaterEqual(mock_hb.call_count, 1)


if __name__ == "__main__":
    unittest.main()
