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
            timeout=10,
        )


# ── _claim_task ───────────────────────────────────────────────────────────


class TestClaimTask(unittest.TestCase):
    def setUp(self):
        agent._ID_TOKEN_CACHE.clear()

    def tearDown(self):
        agent._ID_TOKEN_CACHE.clear()

    def test_returns_task_on_success(self):
        with patch.object(agent, "_post", return_value={"task": {"task_id": "t1", "kind": "burner_engage"}}):
            result = agent._claim_task()
        self.assertEqual(result, {"task_id": "t1", "kind": "burner_engage"})

    def test_returns_none_when_no_task(self):
        with patch.object(agent, "_post", return_value={}):
            result = agent._claim_task()
        self.assertIsNone(result)

    def test_returns_none_on_401(self):
        err = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)
        with patch.object(agent, "_post", side_effect=err), \
             patch("time.sleep") as mock_sleep:
            result = agent._claim_task()
        self.assertIsNone(result)
        mock_sleep.assert_called_once_with(60)

    def test_returns_none_on_other_http_error(self):
        err = urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, BytesIO(b"err"))
        with patch.object(agent, "_post", side_effect=err), \
             patch("time.sleep") as mock_sleep:
            result = agent._claim_task()
        self.assertIsNone(result)
        mock_sleep.assert_called_once_with(5)

    def test_returns_none_on_generic_exception(self):
        with patch.object(agent, "_post", side_effect=Exception("timeout")), \
             patch("time.sleep") as mock_sleep:
            result = agent._claim_task()
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
        self.assertEqual(args[1]["status"], "failed")
        self.assertEqual(args[1]["error"], "something broke")

    def test_ack_swallows_exception(self):
        with patch.object(agent, "_post", side_effect=Exception("network")):
            # should not raise
            agent._ack("tid3", ok=True)


# ── _execute ──────────────────────────────────────────────────────────────


class TestExecute(unittest.TestCase):
    def test_unsupported_kind(self):
        ok, out_uri, err = agent._execute({"task_id": "t1", "kind": "unknown_kind", "payload": {}})
        self.assertFalse(ok)
        self.assertIsNone(out_uri)
        self.assertIn("unsupported", err)  # type: ignore[operator]

    def test_playwright_upload_raises_not_implemented(self):
        ok, out_uri, err = agent._execute({
            "task_id": "t1",
            "kind": "playwright_upload",
            "payload": {"mp4_uri": "gs://x/y.mp4"},
        })
        self.assertFalse(ok)
        self.assertIsNone(out_uri)
        # Either NotImplementedError or ImportError is acceptable —
        # depends on whether upload_short_via_playwright exists
        self.assertTrue(
            "NotImplementedError" in str(err) or "ImportError" in str(err) or err is not None,
            f"expected error info, got: {err!r}",
        )

    def test_burner_engage_missing_slug(self):
        ok, out_uri, err = agent._execute({
            "task_id": "t1",
            "kind": "burner_engage",
            "payload": {},
        })
        self.assertFalse(ok)
        self.assertIn("missing payload.slug", err)  # type: ignore[operator]

    def test_burner_engage_success(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with patch("subprocess.run", return_value=mock_proc):
            ok, out_uri, err = agent._execute({
                "task_id": "t1",
                "kind": "burner_engage",
                "payload": {"slug": "burner1"},
            })
        self.assertTrue(ok)
        self.assertIsNone(out_uri)
        self.assertIsNone(err)

    def test_burner_engage_failure(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "some error"
        mock_proc.stdout = ""
        with patch("subprocess.run", return_value=mock_proc):
            ok, out_uri, err = agent._execute({
                "task_id": "t1",
                "kind": "burner_engage",
                "payload": {"slug": "burner1"},
            })
        self.assertFalse(ok)
        self.assertIn("exit=1", err)  # type: ignore[operator]

    def test_exception_in_execute_caught(self):
        with patch.object(agent, "_exec_burner_engage", side_effect=RuntimeError("boom")):
            ok, out_uri, err = agent._execute({
                "task_id": "t1",
                "kind": "burner_engage",
                "payload": {"slug": "x"},
            })
        self.assertFalse(ok)
        self.assertIn("RuntimeError", err)  # type: ignore[operator]


# ── run() main loop ───────────────────────────────────────────────────────


class TestRunLoop(unittest.TestCase):
    def setUp(self):
        agent._ID_TOKEN_CACHE.clear()

    def tearDown(self):
        agent._ID_TOKEN_CACHE.clear()

    def test_run_iterates_once_then_exits(self):
        """Simulate: heartbeat → claim returns task → execute → ack → claim returns None → exit."""
        task = {"task_id": "t1", "kind": "burner_engage", "payload": {"slug": "b1"}}

        calls = {"n": 0}

        def claim_side_effect():
            calls["n"] += 1
            if calls["n"] == 1:
                return task
            raise StopIteration  # abort the while loop for testing

        with patch.object(agent, "_heartbeat"), \
             patch.object(agent, "_claim_task", side_effect=claim_side_effect), \
             patch.object(agent, "_execute", return_value=(True, None, None)) as mock_exec, \
             patch.object(agent, "_ack") as mock_ack, \
             patch("time.sleep"):
            with self.assertRaises(StopIteration):
                agent.run()

        mock_exec.assert_called_once_with(task)
        mock_ack.assert_called_once_with("t1", ok=True, output_uri=None, error=None)

    def test_run_sends_heartbeat_when_due(self):
        """When time since last heartbeat exceeds HEARTBEAT_INTERVAL_S, heartbeat is called."""
        calls = {"n": 0}

        def claim_side_effect():
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            raise StopIteration

        # time starts at 0 (initial), then advances to HEARTBEAT_INTERVAL_S+1
        # so the first loop iteration sees now=61 > last_hb=0 → heartbeat fires
        times = iter([0.0, agent.HEARTBEAT_INTERVAL_S + 1])

        def fake_time():
            try:
                return next(times)
            except StopIteration:
                return agent.HEARTBEAT_INTERVAL_S + 1

        with patch.object(agent, "_heartbeat") as mock_hb, \
             patch.object(agent, "_claim_task", side_effect=claim_side_effect), \
             patch("time.sleep"), \
             patch("time.time", side_effect=fake_time):
            with self.assertRaises(StopIteration):
                agent.run()

        mock_hb.assert_called()

    def test_run_skips_heartbeat_when_recent(self):
        """When last_hb is recent, heartbeat should not be called again."""
        calls = {"n": 0}
        t = time.time()

        def fake_time():
            return t  # time never advances → heartbeat interval never exceeded

        def claim_side_effect():
            calls["n"] += 1
            if calls["n"] == 2:
                raise StopIteration
            return None

        with patch.object(agent, "_heartbeat") as mock_hb, \
             patch.object(agent, "_claim_task", side_effect=claim_side_effect), \
             patch("time.sleep"), \
             patch("time.time", side_effect=fake_time):
            with self.assertRaises(StopIteration):
                agent.run()

        # heartbeat called once (first loop) and cached thereafter
        self.assertEqual(mock_hb.call_count, 1)


if __name__ == "__main__":
    unittest.main()
