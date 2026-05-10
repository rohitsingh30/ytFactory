"""100% coverage for control/routes/clone_video_routes.py."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from fastapi import FastAPI

from tests._helpers import PROJECT_ROOT
import control.routes.clone_video_routes as clone_mod
from control.routes.clone_video_routes import router


SCRATCH = PROJECT_ROOT / "tests" / "_scratch_routes_clone_video"


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


class CloneBase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(SCRATCH, ignore_errors=True)
        self.workspace = SCRATCH / "workspace"
        self.workspace.mkdir(parents=True)
        self.patcher = patch.object(clone_mod, "WORKSPACE", self.workspace)
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def req_dir(self, request_id: str = "req_123") -> Path:
        p = self.workspace / request_id
        p.mkdir(parents=True, exist_ok=True)
        return p


class FilesystemHelperTest(CloneBase):
    def test_req_dir_valid_and_invalid(self) -> None:
        self.assertEqual(clone_mod._req_dir("abc-DEF_123"), self.workspace / "abc-DEF_123")
        with self.assertRaises(Exception) as ctx:
            clone_mod._req_dir("bad!")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_state_write_read_missing_corrupt_and_merge(self) -> None:
        req_dir = self.req_dir()
        first = clone_mod._write_state(req_dir, state="queued", error=None)
        self.assertEqual(first, {"state": "queued"})
        second = clone_mod._write_state(req_dir, state="done", error="x")
        self.assertEqual(second["error"], "x")
        third = clone_mod._write_state(req_dir, error=None)
        self.assertIn("error", third)
        self.assertEqual(clone_mod._read_state(req_dir)["state"], "done")
        self.assertEqual(clone_mod._read_state(self.workspace / "missing"), {"state": "missing"})
        (req_dir / "state.json").write_text("not json")
        self.assertEqual(clone_mod._read_state(req_dir), {"state": "corrupt"})
        clone_mod._write_state(req_dir, state="fresh")
        self.assertEqual(json.loads((req_dir / "state.json").read_text())["state"], "fresh")

    def test_log_and_tail(self) -> None:
        req_dir = self.req_dir()
        self.assertEqual(clone_mod._read_log_tail(req_dir), "")
        for i in range(5):
            clone_mod._log(req_dir, f"line {i}")
        tail = clone_mod._read_log_tail(req_dir, n=2)
        self.assertIn("line 3", tail)
        self.assertIn("line 4", tail)
        with patch.object(Path, "read_text", side_effect=RuntimeError("boom")):
            self.assertEqual(clone_mod._read_log_tail(req_dir), "")


class PipelineStepTest(CloneBase):
    def test_run_subproc_delegates_to_subprocess_run(self) -> None:
        cp = subprocess.CompletedProcess(["cmd"], 0, stdout="ok", stderr="")
        with patch.object(clone_mod.subprocess, "run", return_value=cp) as run:
            self.assertIs(clone_mod._run_subproc(["cmd"], timeout=7), cp)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["timeout"], 7)

    def test_ydl_download_success_failure_and_no_file(self) -> None:
        req_dir = self.req_dir("ydl")
        (req_dir / "video.mp4").write_bytes(b"mp4")
        ok = subprocess.CompletedProcess(["yt-dlp"], 0, stdout="", stderr="")
        with patch.object(clone_mod, "_run_subproc", return_value=ok):
            self.assertEqual(clone_mod._ydl_download("https://example.com/v", req_dir), req_dir / "video.mp4")
        fail = subprocess.CompletedProcess(["yt-dlp"], 1, stdout="out", stderr="err")
        with patch.object(clone_mod, "_run_subproc", return_value=fail):
            with self.assertRaises(RuntimeError) as ctx:
                clone_mod._ydl_download("url", self.req_dir("ydl_fail"))
            self.assertIn("yt-dlp failed", str(ctx.exception))
        with patch.object(clone_mod, "_run_subproc", return_value=ok):
            with self.assertRaises(RuntimeError) as ctx:
                clone_mod._ydl_download("url", self.req_dir("ydl_empty"))
            self.assertIn("produced no video", str(ctx.exception))

    def test_ffprobe_duration_success_and_failure(self) -> None:
        good = subprocess.CompletedProcess(["ffprobe"], 0, stdout="9.75\n", stderr="")
        bad = subprocess.CompletedProcess(["ffprobe"], 0, stdout="nope", stderr="")
        with patch.object(clone_mod, "_run_subproc", return_value=good):
            self.assertEqual(clone_mod._ffprobe_duration(Path("v.mp4")), 9.75)
        with patch.object(clone_mod, "_run_subproc", return_value=bad):
            self.assertEqual(clone_mod._ffprobe_duration(Path("v.mp4")), 0.0)

    def test_extract_frames_duration_fallback_and_partial_success(self) -> None:
        req_dir = self.req_dir("frames")
        calls = [0]

        def run(cmd: list[str], timeout: int = 0):  # noqa: ARG001
            calls[0] += 1
            if calls[0] == 1:
                Path(cmd[-1]).write_bytes(b"jpg")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        with patch.object(clone_mod, "_ffprobe_duration", return_value=0.0), \
             patch.object(clone_mod, "_run_subproc", side_effect=run):
            frames = clone_mod._extract_frames(Path("video.mp4"), req_dir, count=2)
        self.assertEqual(frames, [req_dir / "frame_01.jpg"])

    def test_extract_audio_success_and_failure(self) -> None:
        req_dir = self.req_dir("audio")

        def success(cmd: list[str], timeout: int = 0):  # noqa: ARG001
            Path(cmd[-1]).write_bytes(b"wav")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(clone_mod, "_run_subproc", side_effect=success):
            self.assertEqual(clone_mod._extract_audio(Path("video.mp4"), req_dir), req_dir / "audio.wav")
        fail_dir = self.req_dir("audio_fail")
        with patch.object(clone_mod, "_run_subproc", return_value=subprocess.CompletedProcess([], 1)):
            self.assertIsNone(clone_mod._extract_audio(Path("video.mp4"), fail_dir))

    def test_azure_whisper_paths(self) -> None:
        audio = self.req_dir("whisper") / "audio.wav"
        audio.write_bytes(b"wav")
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(clone_mod._azure_whisper_transcribe(audio))
        with patch.dict(os.environ, {"AZURE_OPENAI_WHISPER_DEPLOYMENT": "whisper"}, clear=True):
            self.assertIsNone(clone_mod._azure_whisper_transcribe(audio))

        client = MagicMock()
        client.audio.transcriptions.create.return_value = "hello transcript"
        fake_openai = types.SimpleNamespace(AzureOpenAI=MagicMock(return_value=client))
        env = {"AZURE_OPENAI_WHISPER": "whisper", "AZURE_OPENAI_ENDPOINT": "https://az", "AZURE_OPENAI_API_KEY": "key"}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {"openai": fake_openai}):
            self.assertEqual(clone_mod._azure_whisper_transcribe(audio), "hello transcript")

        client.audio.transcriptions.create.return_value = SimpleNamespace(text="object transcript")
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {"openai": fake_openai}):
            self.assertEqual(clone_mod._azure_whisper_transcribe(audio), "object transcript")

        fake_openai_err = types.SimpleNamespace(AzureOpenAI=MagicMock(side_effect=RuntimeError("boom")))
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {"openai": fake_openai_err}):
            self.assertIsNone(clone_mod._azure_whisper_transcribe(audio))

    def test_azure_gpt_analyze_paths(self) -> None:
        frame = self.req_dir("gpt") / "frame.jpg"
        frame.write_bytes(b"jpg")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                clone_mod._azure_gpt_analyze([frame], None, 5.0, "notes")

        client = MagicMock()
        fake_openai = types.SimpleNamespace(AzureOpenAI=MagicMock(return_value=client))
        env = {"AZURE_OPENAI_ENDPOINT": "https://az", "AZURE_OPENAI_API_KEY": "key", "AZURE_OPENAI_MODEL": "model"}
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"label": "Format"}'))]
        )
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {"openai": fake_openai}):
            result = clone_mod._azure_gpt_analyze([frame], "transcript", 12.5, "notes")
        self.assertEqual(result, {"label": "Format"})

        client.chat.completions.create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="not-json"))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))]),
        ]
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {"openai": fake_openai}):
            with self.assertRaises(RuntimeError):
                clone_mod._azure_gpt_analyze([frame], None, 1.0, "")
            with self.assertRaises(RuntimeError):
                clone_mod._azure_gpt_analyze([frame], None, 1.0, "")


class CloudRunDelegationTest(CloneBase):
    def test_run_via_cloudrun_requires_url_and_handles_http_error(self) -> None:
        self.req_dir("cloud1")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                clone_mod._run_via_cloudrun("cloud1", "url", "notes")

        self.req_dir("cloud2")
        resp = SimpleNamespace(status_code=500, text="bad", json=lambda: {})
        with patch.dict(os.environ, {"CLOUDRUN_CLONE_VIDEO_URL": "https://svc"}, clear=True), \
             patch("requests.post", return_value=resp), \
             patch.dict(sys.modules, {"pipeline.cloud.cloudrun_auth": types.SimpleNamespace(get_id_token=lambda _url: "tok")}):
            with self.assertRaises(RuntimeError) as ctx:
                clone_mod._run_via_cloudrun("cloud2", "url", "notes")
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_run_via_cloudrun_success_and_token_warning(self) -> None:
        req_dir = self.req_dir("cloudok")
        good_frame = base64.b64encode(b"jpg").decode("ascii")
        body = {
            "fingerprint": {"label": "Clone"},
            "frames_b64": [good_frame, "abc"],
            "duration_s": 42,
            "has_transcript": True,
            "log": ["one", "two"],
        }
        resp = SimpleNamespace(status_code=200, text="ok", json=lambda: body)
        with patch.dict(os.environ, {"CLOUDRUN_CLONE_VIDEO_URL": "https://svc/", "CLOUDRUN_CLONE_VIDEO_TIMEOUT": "3"}, clear=True), \
             patch("requests.post", return_value=resp) as post, \
             patch.dict(sys.modules, {"pipeline.cloud.cloudrun_auth": types.SimpleNamespace(get_id_token=MagicMock(side_effect=RuntimeError("auth")))}):
            clone_mod._run_via_cloudrun("cloudok", "https://video", "notes")
        post.assert_called_once()
        self.assertTrue((req_dir / "frame_01.jpg").exists())
        self.assertEqual(json.loads((req_dir / "fingerprint.json").read_text())["label"], "Clone")
        state = json.loads((req_dir / "state.json").read_text())
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["frames_count"], 2)
        log = (req_dir / "log.txt").read_text()
        self.assertIn("WARN: could not mint ID token", log)
        self.assertIn("[cloud] one", log)

    def test_run_pipeline_missing_request_missing_env_success_and_failure(self) -> None:
        clone_mod._run_pipeline("missing")

        noenv = self.req_dir("noenv")
        (noenv / "request.json").write_text(json.dumps({"url": "u", "notes": "n"}))
        with patch.dict(os.environ, {}, clear=True):
            clone_mod._run_pipeline("noenv")
        self.assertEqual(json.loads((noenv / "state.json").read_text())["state"], "failed")

        ok = self.req_dir("pipeok")
        (ok / "request.json").write_text(json.dumps({"url": "u", "notes": "n"}))
        err = self.req_dir("pipeerr")
        (err / "request.json").write_text(json.dumps({"url": "u"}))
        with patch.dict(os.environ, {"CLOUDRUN_CLONE_VIDEO_URL": "https://svc"}, clear=True), \
             patch.object(clone_mod, "_run_via_cloudrun", side_effect=[None, RuntimeError("boom")]):
            clone_mod._run_pipeline("pipeok")
            clone_mod._run_pipeline("pipeerr")
        self.assertEqual(json.loads((err / "state.json").read_text())["state"], "failed")
        self.assertIn("boom", (err / "log.txt").read_text())


class CloneVideoRoutesTest(CloneBase, unittest.IsolatedAsyncioTestCase):
    async def _client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app()), base_url="http://test")

    async def test_create_clone_and_get_status(self) -> None:
        with patch.object(clone_mod.uuid, "uuid4", return_value=SimpleNamespace(hex="abc123def4567890")), \
             patch.object(clone_mod._EXEC, "submit") as submit:
            async with await self._client() as c:
                created = await c.post("/api/clone_video", json={"url": "https://example.com/v", "notes": "copy pacing"})
                fetched = await c.get("/api/clone_video/abc123def456")
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["request_id"], "abc123def456")
        submit.assert_called_once()
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["state"], "queued")

    async def test_get_clone_missing_invalid_and_corrupt_state(self) -> None:
        bad = self.req_dir("badstate")
        (bad / "state.json").write_text("{")
        async with await self._client() as c:
            missing = await c.get("/api/clone_video/notthere")
            invalid = await c.get("/api/clone_video/bad!")
            corrupt = await c.get("/api/clone_video/badstate")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(corrupt.status_code, 200)
        self.assertEqual(corrupt.json()["state"], "corrupt")

    async def test_get_frame_validation_missing_and_success(self) -> None:
        req_dir = self.req_dir("framesreq")
        (req_dir / "frame_01.jpg").write_bytes(b"jpg")
        async with await self._client() as c:
            low = await c.get("/api/clone_video/framesreq/frames/0.jpg")
            high = await c.get("/api/clone_video/framesreq/frames/31.jpg")
            missing = await c.get("/api/clone_video/framesreq/frames/2.jpg")
            ok = await c.get("/api/clone_video/framesreq/frames/1.jpg")
        self.assertEqual(low.status_code, 400)
        self.assertEqual(high.status_code, 400)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.content, b"jpg")


if __name__ == "__main__":
    unittest.main()
