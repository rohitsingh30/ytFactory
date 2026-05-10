"""Tests for pipeline.cloud.skill_dispatch (19% → 100%)."""
from __future__ import annotations

import json
import sys
import time
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from unittest.mock import MagicMock, patch

import pipeline.cloud.skill_dispatch as sd


def _urlopen_ok(data: dict, status: int = 200):
    """Return a context-manager mock that yields a response with `data`."""
    body = json.dumps(data).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class _Base(unittest.TestCase):
    def setUp(self):
        sd._ID_TOKEN_CACHE.clear()
        # Point at localhost so _auth_headers returns no Bearer token
        self._url_patcher = patch.object(sd, "WEBSITE_URL", "http://localhost:8765")
        self._url_patcher.start()

    def tearDown(self):
        self._url_patcher.stop()
        sd._ID_TOKEN_CACHE.clear()


# ── _is_localhost ─────────────────────────────────────────────────────────

class TestIsLocalhost(unittest.TestCase):
    def test_localhost(self):
        self.assertTrue(sd._is_localhost("http://localhost:8765"))

    def test_127(self):
        self.assertTrue(sd._is_localhost("http://127.0.0.1:8765/path"))

    def test_cloud_url(self):
        self.assertFalse(sd._is_localhost("https://ytfactory-web.run.app"))


# ── _get_id_token ─────────────────────────────────────────────────────────

class TestGetIdToken(unittest.TestCase):
    def setUp(self):
        sd._ID_TOKEN_CACHE.clear()

    def tearDown(self):
        sd._ID_TOKEN_CACHE.clear()

    def test_calls_gcloud(self):
        with patch("subprocess.check_output", return_value="tok\n") as mock_sub:
            tok = sd._get_id_token("https://service.run.app")
        self.assertEqual(tok, "tok")

    def test_caches_token(self):
        with patch("subprocess.check_output", return_value="tok\n") as mock_sub:
            sd._get_id_token("https://service.run.app")
            sd._get_id_token("https://service.run.app")
        mock_sub.assert_called_once()

    def test_gcloud_not_found_raises(self):
        with patch("subprocess.check_output", side_effect=FileNotFoundError):
            with self.assertRaises(sd.WebsiteUnreachableError) as ctx:
                sd._get_id_token("https://service.run.app")
        self.assertIn("gcloud CLI not found", str(ctx.exception))

    def test_gcloud_error_raises(self):
        import subprocess
        with patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, ["gcloud"], stderr="err")):
            with self.assertRaises(sd.WebsiteUnreachableError) as ctx:
                sd._get_id_token("https://service.run.app")
        self.assertIn("failed", str(ctx.exception))

    def test_empty_token_raises(self):
        with patch("subprocess.check_output", return_value=""):
            with self.assertRaises(sd.WebsiteUnreachableError) as ctx:
                sd._get_id_token("https://service.run.app")
        self.assertIn("empty", str(ctx.exception))


# ── _auth_headers ─────────────────────────────────────────────────────────

class TestAuthHeaders(_Base):
    def test_localhost_returns_empty(self):
        headers = sd._auth_headers("http://localhost:8765/path")
        self.assertEqual(headers, {})

    def test_cloud_returns_bearer(self):
        with patch.object(sd, "_get_id_token", return_value="my-token"):
            headers = sd._auth_headers("https://service.run.app/path")
        self.assertIn("Authorization", headers)
        self.assertIn("my-token", headers["Authorization"])


# ── _post_json / _get_json ────────────────────────────────────────────────

class TestHttpHelpers(_Base):
    def test_post_json_success(self):
        resp = _urlopen_ok({"job_id": "j1"})
        with patch("urllib.request.urlopen", return_value=resp):
            result = sd._post_json("/api/test", {"key": "val"})
        self.assertEqual(result, {"job_id": "j1"})

    def test_post_json_url_error_raises_unreachable(self):
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("connection refused")):
            with self.assertRaises(sd.WebsiteUnreachableError):
                sd._post_json("/api/test", {})

    def test_post_json_http_error_raises_unreachable(self):
        """HTTPError is a subclass of URLError — caught by URLError handler."""
        err = urllib.error.HTTPError("http://x", 500, "Internal Server Error", {},
                                     BytesIO(b"error body"))
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(sd.WebsiteUnreachableError) as ctx:
                sd._post_json("/api/test", {})

    def test_get_json_success(self):
        resp = _urlopen_ok({"state": "done"})
        with patch("urllib.request.urlopen", return_value=resp):
            result = sd._get_json("/api/jobs/from_script/j1")
        self.assertEqual(result["state"], "done")

    def test_get_json_url_error_raises_unreachable(self):
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("timeout")):
            with self.assertRaises(sd.WebsiteUnreachableError):
                sd._get_json("/api/jobs/from_script/j1")


# ── submit_render ─────────────────────────────────────────────────────────

class TestSubmitRender(_Base):
    def test_shorts_mode(self):
        resp = _urlopen_ok({"job_id": "j1"})
        with patch("urllib.request.urlopen", return_value=resp):
            job_id = sd.submit_render(
                channel_yaml="mystoriesanimated/variants/aita.yaml",
                script_path="mystoriesanimated/scripts/slug.json",
            )
        self.assertEqual(job_id, "j1")

    def test_cmd_mode(self):
        resp = _urlopen_ok({"job_id": "j2"})
        with patch("urllib.request.urlopen", return_value=resp):
            job_id = sd.submit_render(cmd=["scripts/render.py"])
        self.assertEqual(job_id, "j2")

    def test_cmd_with_extra_args(self):
        resp = _urlopen_ok({"job_id": "j3"})
        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            sd.submit_render(cmd=["render.py"], extra_args=["--flag", "val"])
        # Verify the request body contains extra args
        req_data = json.loads(mock_open.call_args[0][0].data)
        self.assertIn("--flag", req_data.get("cmd", []))

    def test_no_args_raises(self):
        with self.assertRaises(ValueError):
            sd.submit_render()

    def test_missing_job_id_raises(self):
        resp = _urlopen_ok({"error": "bad"})
        with patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(RuntimeError):
                sd.submit_render(cmd=["render.py"])

    def test_with_label(self):
        resp = _urlopen_ok({"job_id": "j-labeled"})
        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            sd.submit_render(cmd=["render.py"], label="my label")
        req_data = json.loads(mock_open.call_args[0][0].data)
        self.assertEqual(req_data.get("label"), "my label")


# ── get_job ───────────────────────────────────────────────────────────────

class TestGetJob(_Base):
    def test_returns_job_dict(self):
        resp = _urlopen_ok({"state": "done", "mp4_path": "/path/to/video.mp4"})
        with patch("urllib.request.urlopen", return_value=resp):
            rec = sd.get_job("j1")
        self.assertEqual(rec["state"], "done")


# ── wait_for_job ──────────────────────────────────────────────────────────

class TestWaitForJob(_Base):
    def test_returns_done_immediately(self):
        resp = _urlopen_ok({"state": "done", "mp4_path": "/p/v.mp4"})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("time.sleep"):
            rec = sd.wait_for_job("j1", poll_s=0.01)
        self.assertEqual(rec["state"], "done")

    def test_polls_until_done(self):
        responses = [
            _urlopen_ok({"state": "running"}),
            _urlopen_ok({"state": "running"}),
            _urlopen_ok({"state": "done", "mp4_path": "/p/v.mp4"}),
        ]
        with patch("urllib.request.urlopen", side_effect=responses), \
             patch("time.sleep"):
            rec = sd.wait_for_job("j1", poll_s=0.01)
        self.assertEqual(rec["state"], "done")

    def test_done_no_mp4_found_state(self):
        resp = _urlopen_ok({"state": "done_no_mp4_found"})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("time.sleep"):
            rec = sd.wait_for_job("j1", poll_s=0.01)
        self.assertEqual(rec["state"], "done_no_mp4_found")

    def test_failed_state_returned(self):
        resp = _urlopen_ok({"state": "failed", "error": "oops"})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("time.sleep"):
            rec = sd.wait_for_job("j1", poll_s=0.01)
        self.assertEqual(rec["state"], "failed")

    def test_timeout_raises(self):
        resp = _urlopen_ok({"state": "running"})
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("time.sleep"):
            with self.assertRaises(TimeoutError):
                sd.wait_for_job("j1", poll_s=0.01, timeout_s=0.0)

    def test_print_progress(self):
        responses = [
            _urlopen_ok({"state": "running", "log_tail": "step 1\n"}),
            _urlopen_ok({"state": "done", "log_tail": "step 1\nstep 2\n"}),
        ]
        import io
        captured = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=responses), \
             patch("time.sleep"), \
             patch("sys.stdout", captured):
            sd.wait_for_job("j1", poll_s=0.01, print_progress=True)
        self.assertIn("step", captured.getvalue())


# ── render_via_website ────────────────────────────────────────────────────

class TestRenderViaWebsite(_Base):
    def test_submit_and_wait(self):
        submit_resp = _urlopen_ok({"job_id": "j1"})
        done_resp = _urlopen_ok({"state": "done", "mp4_path": "/p/v.mp4"})
        with patch("urllib.request.urlopen", side_effect=[submit_resp, done_resp]), \
             patch("time.sleep"):
            rec = sd.render_via_website(cmd=["render.py"], poll_s=0.01)
        self.assertEqual(rec["state"], "done")

    def test_with_print_progress(self):
        submit_resp = _urlopen_ok({"job_id": "j1"})
        done_resp = _urlopen_ok({"state": "done", "mp4_path": "/p/v.mp4", "log_tail": ""})
        import io
        captured = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=[submit_resp, done_resp]), \
             patch("time.sleep"), \
             patch("sys.stdout", captured):
            sd.render_via_website(cmd=["render.py"], poll_s=0.01, print_progress=True)
        self.assertIn("j1", captured.getvalue())


# ── CLI ───────────────────────────────────────────────────────────────────

class TestCLI(_Base):
    def _run(self, *argv):
        return sd.main(list(argv))

    def test_render_cmd_success(self):
        submit_resp = _urlopen_ok({"job_id": "j1"})
        done_resp = _urlopen_ok({"state": "done", "mp4_path": "/p/v.mp4"})
        with patch("urllib.request.urlopen", side_effect=[submit_resp, done_resp]), \
             patch("time.sleep"):
            rc = self._run("render", "--cmd", "render.py")
        self.assertEqual(rc, 0)

    def test_render_channel_script_success(self):
        submit_resp = _urlopen_ok({"job_id": "j1"})
        done_resp = _urlopen_ok({"state": "done", "mp4_path": "/p/v.mp4"})
        with patch("urllib.request.urlopen", side_effect=[submit_resp, done_resp]), \
             patch("time.sleep"):
            rc = self._run("render",
                           "--channel", "chan/variants/v.yaml",
                           "--script", "chan/scripts/s.json")
        self.assertEqual(rc, 0)

    def test_render_no_args_returns_2(self):
        rc = self._run("render")
        self.assertEqual(rc, 2)

    def test_render_website_unreachable_returns_2(self):
        with patch.object(sd, "render_via_website",
                          side_effect=sd.WebsiteUnreachableError("down")):
            rc = self._run("render", "--cmd", "render.py")
        self.assertEqual(rc, 2)

    def test_render_timeout_returns_3(self):
        with patch.object(sd, "render_via_website",
                          side_effect=TimeoutError("timed out")):
            rc = self._run("render", "--cmd", "render.py")
        self.assertEqual(rc, 3)

    def test_render_done_no_mp4_returns_1(self):
        submit_resp = _urlopen_ok({"job_id": "j1"})
        done_resp = _urlopen_ok({"state": "done_no_mp4_found", "log_path": "/log.txt"})
        with patch("urllib.request.urlopen", side_effect=[submit_resp, done_resp]), \
             patch("time.sleep"):
            rc = self._run("render", "--cmd", "render.py")
        self.assertEqual(rc, 1)

    def test_render_failed_returns_1(self):
        submit_resp = _urlopen_ok({"job_id": "j1"})
        done_resp = _urlopen_ok({"state": "failed", "error": "oops", "log_path": "/log.txt"})
        with patch("urllib.request.urlopen", side_effect=[submit_resp, done_resp]), \
             patch("time.sleep"):
            rc = self._run("render", "--cmd", "render.py")
        self.assertEqual(rc, 1)

    def test_status_cmd(self):
        resp = _urlopen_ok({"state": "done", "mp4_path": "/p/v.mp4"})
        with patch("urllib.request.urlopen", return_value=resp):
            rc = self._run("status", "j1")
        self.assertEqual(rc, 0)

    def test_list_cmd(self):
        resp = _urlopen_ok({"jobs": []})
        with patch("urllib.request.urlopen", return_value=resp):
            rc = self._run("list")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
