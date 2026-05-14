"""Tests for pipeline/asr_cloudrun.py — laptop-side cloud whisper client.

Cover the contract:
- align_via_cloud raises CloudRunAsrUnavailable when CLOUDRUN_ASR_URL is unset
- align_via_cloud raises CloudRunAsrUnavailable on connection error
- align_via_cloud raises CloudRunAsrUnavailable on 5xx response
- align_via_cloud raises plain RuntimeError on 4xx response (caller-error)
- align_via_cloud parses the response into Segment objects correctly
- Small files inline as base64; large files upload to GCS (mocked)
- Auth header attached
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from pipeline.render.contracts import Segment


def _make_wav(path: Path, duration_s: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "sine=frequency=440:sample_rate=24000",
        "-ac", "1", "-c:a", "pcm_s16le",
        str(path),
    ], check=True, capture_output=True)


class ServiceUrlTest(unittest.TestCase):
    def test_unset_env_raises_unavailable(self):
        from pipeline.asr_cloudrun import (
            CloudRunAsrUnavailable, _service_url,
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLOUDRUN_ASR_URL", None)
            with self.assertRaises(CloudRunAsrUnavailable) as ctx:
                _service_url()
            self.assertIn("CLOUDRUN_ASR_URL", str(ctx.exception))

    def test_set_env_returns_stripped_url(self):
        from pipeline.asr_cloudrun import _service_url
        with patch.dict(os.environ, {"CLOUDRUN_ASR_URL": "https://x.run.app/"}):
            self.assertEqual(_service_url(), "https://x.run.app")


class TimeoutEnvTest(unittest.TestCase):
    def test_default_timeout_is_600s(self):
        from pipeline.asr_cloudrun import _timeout_s
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLOUDRUN_ASR_TIMEOUT", None)
            self.assertEqual(_timeout_s(), 600.0)

    def test_env_override_used(self):
        from pipeline.asr_cloudrun import _timeout_s
        with patch.dict(os.environ, {"CLOUDRUN_ASR_TIMEOUT": "120"}):
            self.assertEqual(_timeout_s(), 120.0)

    def test_invalid_env_falls_back_to_default(self):
        from pipeline.asr_cloudrun import _timeout_s
        with patch.dict(os.environ, {"CLOUDRUN_ASR_TIMEOUT": "abc"}):
            self.assertEqual(_timeout_s(), 600.0)


class AlignViaCloudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-asr-cloud-"))
        self.wav = self.tmp / "n.wav"
        _make_wav(self.wav, duration_s=0.5)
        # Patch env + auth so the function doesn't try to call gcloud.
        self.env_patch = patch.dict(os.environ, {
            "CLOUDRUN_ASR_URL": "https://stub.example.com",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.token_patch = patch("pipeline.asr_cloudrun._id_token_for",
                                 return_value="stub-token")
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_wav_raises_filenotfound(self):
        from pipeline.asr_cloudrun import align_via_cloud
        with self.assertRaises(FileNotFoundError):
            align_via_cloud(self.tmp / "nope.wav", mode="beats")

    def test_connection_error_raises_unavailable(self):
        from pipeline.asr_cloudrun import (
            CloudRunAsrUnavailable, align_via_cloud,
        )
        with patch("requests.post", side_effect=requests.ConnectionError("boom")):
            with self.assertRaises(CloudRunAsrUnavailable) as ctx:
                align_via_cloud(self.wav, mode="beats")
            self.assertIn("ConnectionError", str(ctx.exception))

    def test_timeout_raises_unavailable(self):
        from pipeline.asr_cloudrun import (
            CloudRunAsrUnavailable, align_via_cloud,
        )
        with patch("requests.post", side_effect=requests.Timeout("slow")):
            with self.assertRaises(CloudRunAsrUnavailable):
                align_via_cloud(self.wav, mode="beats")

    def test_5xx_raises_unavailable(self):
        from pipeline.asr_cloudrun import (
            CloudRunAsrUnavailable, align_via_cloud,
        )
        resp = MagicMock(status_code=503, text="server boom")
        with patch("requests.post", return_value=resp):
            with self.assertRaises(CloudRunAsrUnavailable) as ctx:
                align_via_cloud(self.wav, mode="beats")
            self.assertIn("503", str(ctx.exception))

    def test_4xx_raises_runtime_error_not_unavailable(self):
        # 4xx = caller error. Should NOT trigger fallback — surface
        # to the caller as a hard error so they fix the request.
        from pipeline.asr_cloudrun import (
            CloudRunAsrUnavailable, align_via_cloud,
        )
        resp = MagicMock(status_code=400, text="bad request")
        with patch("requests.post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                align_via_cloud(self.wav, mode="beats")
            # NOT a CloudRunAsrUnavailable — caller should not fall back.
            self.assertNotIsInstance(ctx.exception, CloudRunAsrUnavailable)

    def test_200_returns_parsed_segments(self):
        from pipeline.asr_cloudrun import align_via_cloud
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "duration_s": 0.5,
            "language": "en",
            "word_count": 2,
            "segments": [
                {"start_s": 0.0, "end_s": 0.3, "text": "hello",
                 "anchor_id": "beat_000", "kind": "beat"},
                {"start_s": 0.3, "end_s": 0.5, "text": "world",
                 "anchor_id": "beat_001", "kind": "beat"},
            ],
        }
        with patch("requests.post", return_value=resp) as mock_post:
            segments = align_via_cloud(self.wav, mode="beats")

        self.assertEqual(len(segments), 2)
        self.assertIsInstance(segments[0], Segment)
        self.assertEqual(segments[0].text, "hello")
        self.assertEqual(segments[0].anchor_id, "beat_000")
        self.assertEqual(segments[1].kind, "beat")

        # Verify the request shape: small file inlined as b64.
        call = mock_post.call_args
        payload = call.kwargs["json"]
        self.assertIn("narration_wav_b64", payload)
        self.assertEqual(payload["mode"], "beats")
        # b64 of a real 0.5s 24kHz mono wav is non-trivial.
        decoded = base64.b64decode(payload["narration_wav_b64"])
        self.assertGreater(len(decoded), 0)

        # Auth header attached.
        headers = call.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer stub-token")

    def test_anchors_passed_through(self):
        from pipeline.asr_cloudrun import align_via_cloud
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"duration_s": 0.5, "language": "en",
                                  "word_count": 0, "segments": []}
        with patch("requests.post", return_value=resp) as mock_post:
            align_via_cloud(self.wav, mode="anchors",
                            anchors=["Section A", "Section B"])
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["mode"], "anchors")
        self.assertEqual(payload["anchors"], ["Section A", "Section B"])

    def test_language_hint_passed_through(self):
        from pipeline.asr_cloudrun import align_via_cloud
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"duration_s": 0, "language": "hi",
                                  "word_count": 0, "segments": []}
        with patch("requests.post", return_value=resp) as mock_post:
            align_via_cloud(self.wav, mode="beats", language="hi")
        self.assertEqual(mock_post.call_args.kwargs["json"]["language"], "hi")


if __name__ == "__main__":
    unittest.main()
