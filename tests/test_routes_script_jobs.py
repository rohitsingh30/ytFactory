"""100% coverage for control/routes/script_jobs_routes.py."""
from __future__ import annotations
import asyncio, base64, json, time, unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from tests._helpers import PROJECT_ROOT  # noqa: F401
import control.routes.script_jobs_routes as jobs_mod

def _make_app():
    from fastapi import FastAPI
    from control.routes.script_jobs_routes import router
    app = FastAPI()
    app.include_router(router)
    return app

def _reset_jobs():
    jobs_mod.SCRIPT_JOBS.clear()

class GcsClientTest(unittest.TestCase):
    def test_returns_storage_client(self):
        # ``from google.cloud import storage`` first checks if
        # ``google.cloud`` has a ``storage`` attribute (gained once any
        # earlier test imports the real client). Patching sys.modules
        # alone isn't enough — also override the package attribute.
        mock_storage = MagicMock()
        mock_client_instance = MagicMock()
        mock_storage.Client.return_value = mock_client_instance
        try:
            import google.cloud as _gc  # type: ignore[import-not-found]
        except ImportError:
            _gc = None

        saved_attr = getattr(_gc, "storage", None) if _gc is not None else None
        had_attr = hasattr(_gc, "storage") if _gc is not None else False
        if _gc is not None:
            _gc.storage = mock_storage
        try:
            with patch.dict("sys.modules", {"google.cloud.storage": mock_storage}):
                import importlib
                mod = importlib.import_module("control.routes.script_jobs_routes")
                result = mod._gcs_client()
        finally:
            if _gc is not None:
                if had_attr:
                    _gc.storage = saved_attr
                else:
                    try:
                        delattr(_gc, "storage")
                    except AttributeError:
                        pass
        mock_storage.Client.assert_called_once()
        self.assertIs(result, mock_client_instance)

class GcsUploadTextTest(unittest.TestCase):
    def test_uploads_to_correct_bucket_and_blob(self):
        mock_client = MagicMock()
        mock_blob = MagicMock()
        mock_client.bucket.return_value.blob.return_value = mock_blob
        with patch.object(jobs_mod, "_gcs_client", return_value=mock_client):
            jobs_mod._gcs_upload_text("hello", "gs://mybucket/path/to/file.json")
        mock_client.bucket.assert_called_once_with("mybucket")
        mock_bucket = mock_client.bucket.return_value
        mock_bucket.blob.assert_called_once_with("path/to/file.json")
        mock_blob.upload_from_string.assert_called_once_with("hello", content_type="application/json")

    def test_custom_content_type(self):
        mock_client = MagicMock()
        mock_blob = MagicMock()
        mock_client.bucket.return_value.blob.return_value = mock_blob
        with patch.object(jobs_mod, "_gcs_client", return_value=mock_client):
            jobs_mod._gcs_upload_text("data", "gs://bucket/blob.txt", "text/plain")
        mock_blob.upload_from_string.assert_called_once_with("data", content_type="text/plain")

class GcsReadJsonTest(unittest.TestCase):
    def test_reads_and_parses_json(self):
        mock_client = MagicMock()
        mock_blob = MagicMock()
        mock_blob.download_as_bytes.return_value = b'{"key": "value"}'
        mock_client.bucket.return_value.blob.return_value = mock_blob
        with patch.object(jobs_mod, "_gcs_client", return_value=mock_client):
            result = jobs_mod._gcs_read_json("gs://bucket/state.json")
        self.assertEqual(result, {"key": "value"})

class GcsReadBytesIfExistsTest(unittest.TestCase):
    def test_returns_none_when_blob_missing(self):
        mock_client = MagicMock()
        mock_blob = MagicMock()
        mock_blob.exists.return_value = False
        mock_client.bucket.return_value.blob.return_value = mock_blob
        with patch.object(jobs_mod, "_gcs_client", return_value=mock_client):
            result = jobs_mod._gcs_read_bytes_if_exists("bucket", "missing.json")
        self.assertIsNone(result)

    def test_returns_bytes_when_blob_exists(self):
        mock_client = MagicMock()
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_bytes.return_value = b"binary data"
        mock_client.bucket.return_value.blob.return_value = mock_blob
        with patch.object(jobs_mod, "_gcs_client", return_value=mock_client):
            result = jobs_mod._gcs_read_bytes_if_exists("bucket", "exists.json")
        self.assertEqual(result, b"binary data")

class B64BytesTest(unittest.TestCase):
    def test_encodes_bytes(self):
        result = jobs_mod._b64_bytes(b"hello")
        self.assertEqual(result, base64.b64encode(b"hello").decode("ascii"))

class B64FileOrGcsTest(unittest.TestCase):
    def test_returns_workspace_file_if_exists(self):
        mock_path = MagicMock()
        mock_path.is_file.return_value = True
        mock_path.read_bytes.return_value = b"yaml content"
        with patch("control.routes.script_jobs_routes.Path") as mock_path_cls:
            mock_workspace = MagicMock()
            mock_workspace.__truediv__ = lambda self, x: mock_path
            mock_path_cls.return_value = mock_workspace
            result = jobs_mod._b64_file_or_gcs("channel/config.yaml")
        self.assertEqual(result, ("channel/config.yaml", b"yaml content"))

    def test_falls_back_to_gcs_when_not_in_workspace(self):
        mock_path = MagicMock()
        mock_path.is_file.return_value = False
        with patch("control.routes.script_jobs_routes.Path") as mock_path_cls:
            mock_workspace = MagicMock()
            mock_workspace.__truediv__ = lambda self, x: mock_path
            mock_path_cls.return_value = mock_workspace
            with patch.object(jobs_mod, "_gcs_read_bytes_if_exists", return_value=b"gcs bytes"):
                result = jobs_mod._b64_file_or_gcs("channel/narrations/slug.json")
        self.assertEqual(result, ("channel/narrations/slug.json", b"gcs bytes"))

    def test_returns_none_when_nowhere(self):
        mock_path = MagicMock()
        mock_path.is_file.return_value = False
        with patch("control.routes.script_jobs_routes.Path") as mock_path_cls:
            mock_workspace = MagicMock()
            mock_workspace.__truediv__ = lambda self, x: mock_path
            mock_path_cls.return_value = mock_workspace
            with patch.object(jobs_mod, "_gcs_read_bytes_if_exists", return_value=None):
                result = jobs_mod._b64_file_or_gcs("missing.yaml")
        self.assertIsNone(result)

class RunCloudrunnTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset_jobs()

    def _seed_job(self, job_id="testjob01"):
        rec = {"job_id": job_id, "label": "test", "cmd": [], "state": "running",
               "started_at": time.time(), "completed_at": None, "exit_code": None,
               "mp4_path": None, "error": None, "backend": "cloudrun"}
        jobs_mod.SCRIPT_JOBS[job_id] = rec
        return rec

    async def test_missing_channel_or_script_fails(self):
        rec = self._seed_job("j1")
        await jobs_mod._run_cloudrun("j1", ["scripts/make_shorts.py"])
        self.assertEqual(rec["state"], "failed")
        self.assertIn("--channel", rec["error"])

    async def test_channel_yaml_not_found_fails(self):
        rec = self._seed_job("j2")
        cmd = ["scripts/make_shorts.py", "--channel", "chan/config.yaml", "--script", "chan/scripts/slug.json"]
        with patch.object(jobs_mod, "_b64_file_or_gcs", return_value=None):
            await jobs_mod._run_cloudrun("j2", cmd)
        self.assertEqual(rec["state"], "failed")
        self.assertIn("channel YAML", rec["error"])

    async def test_script_not_found_fails(self):
        rec = self._seed_job("j3")
        cmd = ["scripts/make_shorts.py", "--channel", "chan/config.yaml", "--script", "chan/scripts/slug.json"]
        def side_effect(rel):
            return (rel, b"yaml: true") if "config.yaml" in rel else None
        with patch.object(jobs_mod, "_b64_file_or_gcs", side_effect=side_effect):
            await jobs_mod._run_cloudrun("j3", cmd)
        self.assertEqual(rec["state"], "failed")
        self.assertIn("script JSON", rec["error"])

    async def test_gcloud_nonzero_exit_fails(self):
        rec = self._seed_job("j4")
        cmd = ["scripts/make_shorts.py", "--channel", "chan/config.yaml", "--script", "chan/scripts/slug.json"]
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"AUTH_ERROR"))
        with patch.object(jobs_mod, "_b64_file_or_gcs", side_effect=lambda rel: (rel, b"content")), \
             patch.object(jobs_mod, "_gcs_upload_text"), \
             patch("asyncio.to_thread", new=AsyncMock(return_value=None)), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await jobs_mod._run_cloudrun("j4", cmd)
        self.assertEqual(rec["state"], "failed")
        self.assertIn("gcloud run jobs execute failed", rec["error"])

    async def test_polling_timeout_marks_failed(self):
        rec = self._seed_job("j5")
        cmd = ["scripts/make_shorts.py", "--channel", "chan/config.yaml", "--script", "chan/scripts/slug.json"]
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"exec-12345\n", b""))
        poll_state = {"state": "running"}
        async def fake_to_thread(fn, *args, **kw):
            if fn == jobs_mod._gcs_upload_text:
                return None
            return poll_state
        with patch.object(jobs_mod, "_b64_file_or_gcs", side_effect=lambda rel: (rel, b"content")), \
             patch("asyncio.to_thread", side_effect=fake_to_thread), \
             patch("asyncio.sleep", new=AsyncMock()), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch.object(jobs_mod, "CLOUDRUN_ARTIFACTS_BUCKET", "bucket"), \
             patch("builtins.range", return_value=range(2)):
            await jobs_mod._run_cloudrun("j5", cmd)
        self.assertEqual(rec["state"], "failed")
        self.assertIn("timed out", rec["error"])

    async def test_success_flow_with_transient_poll_error(self):
        rec = self._seed_job("j6")
        cmd = ["scripts/make_shorts.py", "--channel", "chan/config.yaml", "--script", "chan/narrations/slug.json"]
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"exec-xyz\n", b""))
        done_state = {"state": "done", "exit_code": 0, "error": None,
                      "mp4_uri": "gs://bucket/jobs/j6/out.mp4",
                      "completed_at": time.time(), "log_uri": "gs://bucket/jobs/j6/log.txt"}
        call_count = [0]
        async def fake_to_thread(fn, *args, **kw):
            if fn == jobs_mod._gcs_upload_text:
                return None
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("not ready yet")
            return done_state
        with patch.object(jobs_mod, "_b64_file_or_gcs", side_effect=lambda rel: (rel, b"content")), \
             patch("asyncio.to_thread", side_effect=fake_to_thread), \
             patch("asyncio.sleep", new=AsyncMock()), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch.object(jobs_mod, "CLOUDRUN_ARTIFACTS_BUCKET", "bucket"):
            await jobs_mod._run_cloudrun("j6", cmd)
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["mp4_path"], "gs://bucket/jobs/j6/out.mp4")
        self.assertEqual(rec["log_uri"], "gs://bucket/jobs/j6/log.txt")

    async def test_success_with_raw_file_found(self):
        rec = self._seed_job("j7")
        cmd = ["scripts/make_shorts.py", "--channel", "chan/config.yaml", "--script", "chan/scripts/slug.json"]
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"exec-abc\n", b""))
        done_state = {"state": "done", "exit_code": 0, "error": None,
                      "mp4_uri": "gs://b/j7.mp4", "completed_at": time.time(), "log_uri": None}
        async def fake_to_thread(fn, *args, **kw):
            if fn == jobs_mod._gcs_upload_text:
                return None
            return done_state
        with patch.object(jobs_mod, "_b64_file_or_gcs", side_effect=lambda rel: (rel, b"content")), \
             patch("asyncio.to_thread", side_effect=fake_to_thread), \
             patch("asyncio.sleep", new=AsyncMock()), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch.object(jobs_mod, "CLOUDRUN_ARTIFACTS_BUCKET", "bucket"):
            await jobs_mod._run_cloudrun("j7", cmd)
        self.assertEqual(rec["state"], "done")

class CreateScriptJobTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset_jobs()

    async def _client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app()), base_url="http://test")

    async def test_no_cmd_no_convenience_returns_400(self):
        async with await self._client() as c:
            r = await c.post("/api/jobs/from_script", json={})
        self.assertEqual(r.status_code, 400)

    async def test_bad_entry_point_returns_400(self):
        async with await self._client() as c:
            r = await c.post("/api/jobs/from_script", json={"cmd": ["evil_script.py"]})
        self.assertEqual(r.status_code, 400)

    async def test_valid_cmd_returns_running(self):
        with patch("asyncio.create_task"):
            async with await self._client() as c:
                r = await c.post("/api/jobs/from_script", json={"cmd": ["scripts/make_shorts.py", "--channel", "x.yaml", "--script", "y.json"]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["state"], "running")
        self.assertIn("job_id", body)

    async def test_convenience_form_channel_yaml_and_script_path(self):
        with patch("asyncio.create_task"):
            async with await self._client() as c:
                r = await c.post("/api/jobs/from_script", json={"channel_yaml": "chan/config.yaml", "script_path": "chan/scripts/slug.json", "extra_args": ["--dry-run"]})
        self.assertEqual(r.status_code, 200)
        job_id = r.json()["job_id"]
        self.assertIn("--dry-run", jobs_mod.SCRIPT_JOBS[job_id]["cmd"])

    async def test_custom_label_stored_in_job(self):
        with patch("asyncio.create_task"):
            async with await self._client() as c:
                r = await c.post("/api/jobs/from_script", json={"cmd": ["scripts/make_shorts.py", "--channel", "x.yaml", "--script", "y.json"], "label": "My label"})
        job_id = r.json()["job_id"]
        self.assertEqual(jobs_mod.SCRIPT_JOBS[job_id]["label"], "My label")

class GetScriptJobTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset_jobs()

    async def _client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app()), base_url="http://test")

    async def test_404_when_not_found(self):
        async with await self._client() as c:
            r = await c.get("/api/jobs/from_script/notexist")
        self.assertEqual(r.status_code, 404)

    async def test_200_when_exists_no_log(self):
        jobs_mod.SCRIPT_JOBS["abc123"] = {"job_id": "abc123", "label": "test", "cmd": [], "state": "running",
            "started_at": time.time(), "completed_at": None, "exit_code": None, "mp4_path": None, "error": None, "log_uri": None, "backend": "cloudrun"}
        async with await self._client() as c:
            r = await c.get("/api/jobs/from_script/abc123")
        self.assertEqual(r.status_code, 200)
        self.assertIn("elapsed_s", r.json())

    async def test_200_with_log_uri_blob_exists(self):
        jobs_mod.SCRIPT_JOBS["logtest"] = {"job_id": "logtest", "label": "test", "cmd": [], "state": "done",
            "started_at": time.time()-10, "completed_at": time.time(), "exit_code": 0, "mp4_path": None, "error": None,
            "log_uri": "gs://bucket/jobs/logtest/log.txt", "backend": "cloudrun"}
        mock_client = MagicMock()
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.size = 1024
        mock_blob.download_as_bytes.return_value = b"log output here"
        mock_client.bucket.return_value.blob.return_value = mock_blob
        with patch.object(jobs_mod, "_gcs_client", return_value=mock_client):
            async with await self._client() as c:
                r = await c.get("/api/jobs/from_script/logtest")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["log_tail"], "log output here")

    async def test_200_with_log_uri_blob_not_exists(self):
        jobs_mod.SCRIPT_JOBS["logmiss"] = {"job_id": "logmiss", "label": "test", "cmd": [], "state": "done",
            "started_at": time.time()-5, "completed_at": time.time(), "exit_code": 0, "mp4_path": None, "error": None,
            "log_uri": "gs://bucket/jobs/logmiss/log.txt", "backend": "cloudrun"}
        mock_client = MagicMock()
        mock_blob = MagicMock()
        mock_blob.exists.return_value = False
        mock_client.bucket.return_value.blob.return_value = mock_blob
        with patch.object(jobs_mod, "_gcs_client", return_value=mock_client):
            async with await self._client() as c:
                r = await c.get("/api/jobs/from_script/logmiss")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("log_tail", r.json())

    async def test_200_log_uri_gcs_exception_swallowed(self):
        jobs_mod.SCRIPT_JOBS["logerr"] = {"job_id": "logerr", "label": "test", "cmd": [], "state": "done",
            "started_at": time.time()-5, "completed_at": time.time(), "exit_code": 0, "mp4_path": None, "error": None,
            "log_uri": "gs://bucket/jobs/logerr/log.txt", "backend": "cloudrun"}
        with patch.object(jobs_mod, "_gcs_client", side_effect=RuntimeError("no auth")):
            async with await self._client() as c:
                r = await c.get("/api/jobs/from_script/logerr")
        self.assertEqual(r.status_code, 200)

    async def test_200_log_uri_zero_size_skips_download(self):
        jobs_mod.SCRIPT_JOBS["logzero"] = {"job_id": "logzero", "label": "test", "cmd": [], "state": "done",
            "started_at": time.time()-5, "completed_at": time.time(), "exit_code": 0, "mp4_path": None, "error": None,
            "log_uri": "gs://bucket/jobs/logzero/log.txt", "backend": "cloudrun"}
        mock_client = MagicMock()
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.size = 0
        mock_client.bucket.return_value.blob.return_value = mock_blob
        with patch.object(jobs_mod, "_gcs_client", return_value=mock_client):
            async with await self._client() as c:
                r = await c.get("/api/jobs/from_script/logzero")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("log_tail", r.json())

class GetScriptJobMp4Test(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset_jobs()

    async def _client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app()), base_url="http://test", follow_redirects=False)

    async def test_404_when_job_not_found(self):
        async with await self._client() as c:
            r = await c.get("/api/jobs/from_script/notfound/mp4")
        self.assertEqual(r.status_code, 404)

    async def test_404_when_no_mp4(self):
        jobs_mod.SCRIPT_JOBS["nump4"] = {"job_id": "nump4", "state": "running", "started_at": time.time(), "mp4_path": None}
        async with await self._client() as c:
            r = await c.get("/api/jobs/from_script/nump4/mp4")
        self.assertEqual(r.status_code, 404)

    async def test_404_when_mp4_not_gcs(self):
        jobs_mod.SCRIPT_JOBS["notgcs"] = {"job_id": "notgcs", "state": "done", "started_at": time.time(), "mp4_path": "/local/path/out.mp4"}
        async with await self._client() as c:
            r = await c.get("/api/jobs/from_script/notgcs/mp4")
        self.assertEqual(r.status_code, 404)

    async def test_302_redirect_to_signed_url(self):
        jobs_mod.SCRIPT_JOBS["hasmp4"] = {"job_id": "hasmp4", "state": "done", "started_at": time.time(), "mp4_path": "gs://bucket/jobs/hasmp4/out.mp4"}
        mock_client = MagicMock()
        mock_blob = MagicMock()
        mock_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed?tok=abc"
        mock_client.bucket.return_value.blob.return_value = mock_blob
        with patch.object(jobs_mod, "_gcs_client", return_value=mock_client):
            async with await self._client() as c:
                r = await c.get("/api/jobs/from_script/hasmp4/mp4")
        self.assertEqual(r.status_code, 302)
        self.assertIn("signed", r.headers["location"])

class ListScriptJobsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset_jobs()

    async def _client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app()), base_url="http://test")

    async def test_empty_list(self):
        async with await self._client() as c:
            r = await c.get("/api/jobs/from_script")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["jobs"], [])

    async def test_returns_sorted_newest_first(self):
        now = time.time()
        for i, t in enumerate([now-100, now-200, now-50]):
            jobs_mod.SCRIPT_JOBS[f"job{i}"] = {"job_id": f"job{i}", "state": "done", "started_at": t, "mp4_path": None}
        async with await self._client() as c:
            r = await c.get("/api/jobs/from_script")
        starts = [j["started_at"] for j in r.json()["jobs"]]
        self.assertEqual(starts, sorted(starts, reverse=True))

    async def test_caps_at_50_jobs(self):
        now = time.time()
        for i in range(60):
            jobs_mod.SCRIPT_JOBS[f"j{i:04d}"] = {"job_id": f"j{i:04d}", "state": "done", "started_at": now+i, "mp4_path": None}
        async with await self._client() as c:
            r = await c.get("/api/jobs/from_script")
        self.assertLessEqual(len(r.json()["jobs"]), 50)

if __name__ == "__main__":
    unittest.main()
