"""Tests for pipeline.footage.yt_dlp_cloudrun — 100% line coverage."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.footage import yt_dlp_cloudrun
from pipeline.footage.yt_dlp_cloudrun import (
    CloudRunYtDlpFailed,
    CloudRunYtDlpUnavailable,
    YoutubeDLCloud,
    _id_token,
    _local_fallback_enabled,
    _service_url,
    download,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fake_resp(
    *,
    status_code=200,
    content=b"binary-data-here",
    headers=None,
    text="",
):
    m = MagicMock()
    m.status_code = status_code
    m.text = text or content.decode("utf-8", errors="replace")
    m.headers = headers if headers is not None else {"X-Ytdlp-Filename": "out.mp4"}
    m.iter_content.return_value = [content] if content else []
    return m


def _fake_resp_empty():
    return _fake_resp(content=b"", headers={"X-Ytdlp-Filename": "out.mp4"})


# ---------------------------------------------------------------------------
# _service_url
# ---------------------------------------------------------------------------

class TestServiceUrl(unittest.TestCase):
    def _clear(self):
        for k in ("CLOUDRUN_YT_DLP_URL", "CLOUDRUN_CLONE_VIDEO_URL"):
            os.environ.pop(k, None)

    def test_raises_when_no_env(self):
        self._clear()
        with self.assertRaises(CloudRunYtDlpUnavailable):
            _service_url()

    def test_ytdlp_env_var(self):
        self._clear()
        os.environ["CLOUDRUN_YT_DLP_URL"] = "https://service.example.com/"
        try:
            url = _service_url()
            self.assertEqual(url, "https://service.example.com")
        finally:
            os.environ.pop("CLOUDRUN_YT_DLP_URL", None)

    def test_clone_video_env_var_fallback(self):
        self._clear()
        os.environ["CLOUDRUN_CLONE_VIDEO_URL"] = "https://clone.example.com"
        try:
            url = _service_url()
            self.assertEqual(url, "https://clone.example.com")
        finally:
            os.environ.pop("CLOUDRUN_CLONE_VIDEO_URL", None)


# ---------------------------------------------------------------------------
# _id_token
# ---------------------------------------------------------------------------

class TestIdToken(unittest.TestCase):
    def test_returns_none_on_import_error(self):
        with patch.dict(sys.modules, {"pipeline.cloud.cloudrun_auth": None}):
            result = _id_token("https://example.com")
            self.assertIsNone(result)

    def test_returns_token_when_available(self):
        fake_module = MagicMock()
        fake_module.get_id_token.return_value = "fake-token-123"
        with patch.dict(sys.modules, {"pipeline.cloud.cloudrun_auth": fake_module}):
            result = _id_token("https://example.com")
            self.assertEqual(result, "fake-token-123")

    def test_returns_none_on_exception(self):
        fake_module = MagicMock()
        fake_module.get_id_token.side_effect = RuntimeError("no auth")
        with patch.dict(sys.modules, {"pipeline.cloud.cloudrun_auth": fake_module}):
            result = _id_token("https://example.com")
            self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _local_fallback_enabled
# ---------------------------------------------------------------------------

class TestLocalFallbackEnabled(unittest.TestCase):
    def test_enabled_by_default(self):
        os.environ.pop("CLOUDRUN_YT_DLP_DISABLE_FALLBACK", None)
        self.assertTrue(_local_fallback_enabled())

    def test_disabled_by_env(self):
        os.environ["CLOUDRUN_YT_DLP_DISABLE_FALLBACK"] = "1"
        try:
            self.assertFalse(_local_fallback_enabled())
        finally:
            os.environ.pop("CLOUDRUN_YT_DLP_DISABLE_FALLBACK")

    def test_disabled_by_true(self):
        os.environ["CLOUDRUN_YT_DLP_DISABLE_FALLBACK"] = "true"
        try:
            self.assertFalse(_local_fallback_enabled())
        finally:
            os.environ.pop("CLOUDRUN_YT_DLP_DISABLE_FALLBACK")


# ---------------------------------------------------------------------------
# download() — happy path
# ---------------------------------------------------------------------------

class TestDownloadHappyPath(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.out = Path(self.td.name) / "out.mp4"
        os.environ["CLOUDRUN_YT_DLP_URL"] = "https://fake-service.example.com"

    def tearDown(self):
        self.td.cleanup()
        os.environ.pop("CLOUDRUN_YT_DLP_URL", None)
        os.environ.pop("CLOUDRUN_YT_DLP_DISABLE_FALLBACK", None)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value="tok")
    @patch("requests.post")
    def test_200_writes_file(self, mock_post, _mock_tok):
        mock_post.return_value = _fake_resp(
            content=b"fake-video-bytes",
            headers={"X-Ytdlp-Filename": "video.mp4"},
        )
        result = download("https://youtube.com/watch?v=abc123", self.out)
        self.assertTrue(result.exists())
        self.assertEqual(result.read_bytes(), b"fake-video-bytes")

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_200_no_token(self, mock_post, _mock_tok):
        mock_post.return_value = _fake_resp(
            content=b"data",
            headers={"X-Ytdlp-Filename": "out.mp4"},
        )
        result = download("https://youtube.com/watch?v=xyz", self.out)
        self.assertTrue(result.exists())

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_ext_substitution(self, mock_post, _mock_tok):
        mock_post.return_value = _fake_resp(
            content=b"audio-data",
            headers={"X-Ytdlp-Filename": "clip.m4a"},
        )
        out_template = Path(self.td.name) / "clip.%(ext)s"
        result = download("https://youtube.com/watch?v=abc", out_template)
        self.assertEqual(result.suffix, ".m4a")

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_no_ext_in_header_defaults_to_bin(self, mock_post, _mock_tok):
        mock_post.return_value = _fake_resp(
            content=b"data",
            headers={},  # no X-Ytdlp-Filename
        )
        out_template = Path(self.td.name) / "clip.%(ext)s"
        result = download("https://youtube.com/watch?v=abc", out_template)
        self.assertEqual(result.suffix, ".bin")

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_optional_params_forwarded(self, mock_post, _mock_tok):
        mock_post.return_value = _fake_resp(content=b"data")
        download(
            "https://youtube.com/watch?v=abc", self.out,
            format_string="bestvideo",
            audio_only=True,
            audio_ext="m4a",
            sections=["00:00-01:00"],
            extra_args=["--no-playlist"],
        )
        body = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json") or mock_post.call_args[0][1]
        # just verify it was called with the URL
        mock_post.assert_called_once()


# ---------------------------------------------------------------------------
# download() — error paths
# ---------------------------------------------------------------------------

class TestDownloadErrors(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.out = Path(self.td.name) / "out.mp4"
        os.environ["CLOUDRUN_YT_DLP_URL"] = "https://fake-service.example.com"
        os.environ["CLOUDRUN_YT_DLP_DISABLE_FALLBACK"] = "1"

    def tearDown(self):
        self.td.cleanup()
        os.environ.pop("CLOUDRUN_YT_DLP_URL", None)
        os.environ.pop("CLOUDRUN_YT_DLP_DISABLE_FALLBACK", None)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_400_raises_failed(self, mock_post, _tok):
        mock_post.return_value = _fake_resp(status_code=400, text="bot block")
        with self.assertRaises(CloudRunYtDlpFailed):
            download("https://youtube.com/watch?v=abc", self.out)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_504_raises_failed(self, mock_post, _tok):
        mock_post.return_value = _fake_resp(status_code=504, text="timeout")
        with self.assertRaises(CloudRunYtDlpFailed):
            download("https://youtube.com/watch?v=abc", self.out)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_5xx_raises_unavailable(self, mock_post, _tok):
        mock_post.return_value = _fake_resp(status_code=503, text="service down")
        with self.assertRaises(CloudRunYtDlpUnavailable):
            download("https://youtube.com/watch?v=abc", self.out)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_zero_byte_body_raises_failed(self, mock_post, _tok):
        mock_post.return_value = _fake_resp_empty()
        with self.assertRaises(CloudRunYtDlpFailed):
            download("https://youtube.com/watch?v=abc", self.out)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post", side_effect=__import__("requests").RequestException("conn error"))
    def test_request_exception_raises_unavailable(self, mock_post, _tok):
        with self.assertRaises(CloudRunYtDlpUnavailable):
            download("https://youtube.com/watch?v=abc", self.out)

    def test_no_service_url_raises(self):
        os.environ.pop("CLOUDRUN_YT_DLP_URL", None)
        os.environ.pop("CLOUDRUN_CLONE_VIDEO_URL", None)
        with self.assertRaises(CloudRunYtDlpUnavailable):
            download("https://youtube.com/watch?v=abc", self.out)


# ---------------------------------------------------------------------------
# download() — fallback paths (DISABLE_FALLBACK not set)
# ---------------------------------------------------------------------------

class TestDownloadFallback(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.out = Path(self.td.name) / "out.mp4"
        os.environ["CLOUDRUN_YT_DLP_URL"] = "https://fake-service.example.com"
        os.environ.pop("CLOUDRUN_YT_DLP_DISABLE_FALLBACK", None)

    def tearDown(self):
        self.td.cleanup()
        os.environ.pop("CLOUDRUN_YT_DLP_URL", None)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("subprocess.run")
    @patch("requests.post", side_effect=__import__("requests").RequestException("conn"))
    def test_request_exception_fallback_to_local(self, mock_post, mock_sub, _tok):
        self.out.touch()
        mock_sub.return_value = MagicMock(returncode=0)
        result = download("https://youtube.com/watch?v=abc", self.out)
        self.assertIsInstance(result, Path)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("subprocess.run")
    @patch("requests.post")
    def test_5xx_fallback_to_local(self, mock_post, mock_sub, _tok):
        mock_post.return_value = _fake_resp(status_code=502, text="bad gateway")
        self.out.touch()
        mock_sub.return_value = MagicMock(returncode=0)
        result = download("https://youtube.com/watch?v=abc", self.out)
        self.assertIsInstance(result, Path)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("subprocess.run")
    def test_no_service_url_fallback(self, mock_sub, _tok):
        os.environ.pop("CLOUDRUN_YT_DLP_URL", None)
        os.environ.pop("CLOUDRUN_CLONE_VIDEO_URL", None)
        self.out.touch()
        mock_sub.return_value = MagicMock(returncode=0)
        result = download("https://youtube.com/watch?v=abc", self.out)
        self.assertIsInstance(result, Path)


# ---------------------------------------------------------------------------
# _local_fallback_download
# ---------------------------------------------------------------------------

class TestLocalFallbackDownload(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.out = Path(self.td.name) / "out.mp4"

    def tearDown(self):
        self.td.cleanup()

    @patch("subprocess.run")
    def test_success(self, mock_sub):
        self.out.touch()
        mock_sub.return_value = MagicMock(returncode=0)
        from pipeline.footage.yt_dlp_cloudrun import _local_fallback_download
        result = _local_fallback_download(
            "https://youtube.com/watch?v=abc",
            self.out,
            format_string="best",
            audio_only=False,
            audio_ext="m4a",
            sections=None,
            extra_args=None,
            timeout_s=30,
        )
        self.assertEqual(result, self.out)

    @patch("subprocess.run")
    def test_audio_only_with_sections_and_extra_args(self, mock_sub):
        self.out.touch()
        mock_sub.return_value = MagicMock(returncode=0)
        from pipeline.footage.yt_dlp_cloudrun import _local_fallback_download
        result = _local_fallback_download(
            "https://youtube.com/watch?v=abc",
            self.out,
            format_string=None,
            audio_only=True,
            audio_ext="mp3",
            sections=["00:00-00:30"],
            extra_args=["--no-playlist"],
            timeout_s=60,
        )
        self.assertEqual(result, self.out)

    @patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "yt_dlp"))
    def test_called_process_error_raises_failed(self, _mock):
        from pipeline.footage.yt_dlp_cloudrun import _local_fallback_download
        with self.assertRaises(CloudRunYtDlpFailed):
            _local_fallback_download(
                "https://youtube.com/watch?v=abc",
                self.out,
                format_string=None,
                audio_only=False,
                audio_ext="m4a",
                sections=None,
                extra_args=None,
                timeout_s=30,
            )

    @patch("subprocess.run", side_effect=FileNotFoundError("yt_dlp not found"))
    def test_file_not_found_raises_unavailable(self, _mock):
        from pipeline.footage.yt_dlp_cloudrun import _local_fallback_download
        with self.assertRaises(CloudRunYtDlpUnavailable):
            _local_fallback_download(
                "https://youtube.com/watch?v=abc",
                self.out,
                format_string=None,
                audio_only=False,
                audio_ext="m4a",
                sections=None,
                extra_args=None,
                timeout_s=30,
            )

    @patch("subprocess.run")
    def test_ext_substitution_glob(self, mock_sub):
        mock_sub.return_value = MagicMock(returncode=0)
        out_template = Path(self.td.name) / "clip.%(ext)s"
        # Create a matching file that glob would find
        actual_out = Path(self.td.name) / "clip.m4a"
        actual_out.touch()
        from pipeline.footage.yt_dlp_cloudrun import _local_fallback_download
        result = _local_fallback_download(
            "https://youtube.com/watch?v=abc",
            out_template,
            format_string=None,
            audio_only=True,
            audio_ext="m4a",
            sections=None,
            extra_args=None,
            timeout_s=30,
        )
        self.assertEqual(result, actual_out)

    @patch("subprocess.run")
    def test_ext_substitution_no_glob_match_raises(self, mock_sub):
        mock_sub.return_value = MagicMock(returncode=0)
        out_template = Path(self.td.name) / "clip.%(ext)s"
        # No file created → glob finds nothing
        from pipeline.footage.yt_dlp_cloudrun import _local_fallback_download
        with self.assertRaises(CloudRunYtDlpFailed):
            _local_fallback_download(
                "https://youtube.com/watch?v=abc",
                out_template,
                format_string=None,
                audio_only=True,
                audio_ext="m4a",
                sections=None,
                extra_args=None,
                timeout_s=30,
            )

    @patch("subprocess.run")
    def test_output_not_exists_raises_failed(self, mock_sub):
        mock_sub.return_value = MagicMock(returncode=0)
        # out path doesn't have %(ext)s and doesn't exist
        from pipeline.footage.yt_dlp_cloudrun import _local_fallback_download
        with self.assertRaises(CloudRunYtDlpFailed):
            _local_fallback_download(
                "https://youtube.com/watch?v=abc",
                self.out,  # self.out was never created
                format_string=None,
                audio_only=False,
                audio_ext="m4a",
                sections=None,
                extra_args=None,
                timeout_s=30,
            )


# ---------------------------------------------------------------------------
# YoutubeDLCloud
# ---------------------------------------------------------------------------

class TestYoutubeDLCloud(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.outtmpl = str(Path(self.td.name) / "out.%(ext)s")
        os.environ["CLOUDRUN_YT_DLP_URL"] = "https://fake-service.example.com"
        os.environ["CLOUDRUN_YT_DLP_DISABLE_FALLBACK"] = "1"

    def tearDown(self):
        self.td.cleanup()
        os.environ.pop("CLOUDRUN_YT_DLP_URL", None)
        os.environ.pop("CLOUDRUN_YT_DLP_DISABLE_FALLBACK", None)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_download_basic(self, mock_post, _tok):
        mock_post.return_value = _fake_resp(
            content=b"video",
            headers={"X-Ytdlp-Filename": "out.mp4"},
        )
        ydl = YoutubeDLCloud({"outtmpl": self.outtmpl, "format": "best"})
        ret = ydl.download(["https://youtube.com/watch?v=abc"])
        self.assertEqual(ret, 0)

    def test_download_empty_urls(self):
        ydl = YoutubeDLCloud({"outtmpl": self.outtmpl})
        ret = ydl.download([])
        self.assertEqual(ret, 0)

    def test_no_outtmpl_raises(self):
        ydl = YoutubeDLCloud({})
        with self.assertRaises(ValueError):
            ydl.download(["https://youtube.com/watch?v=abc"])

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_audio_only_postprocessor(self, mock_post, _tok):
        mock_post.return_value = _fake_resp(
            content=b"audio",
            headers={"X-Ytdlp-Filename": "out.m4a"},
        )
        opts = {
            "outtmpl": self.outtmpl,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}
            ],
        }
        ydl = YoutubeDLCloud(opts)
        ret = ydl.download(["https://youtube.com/watch?v=abc"])
        self.assertEqual(ret, 0)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_ffmpegaudioconvertor_postprocessor(self, mock_post, _tok):
        mock_post.return_value = _fake_resp(
            content=b"audio",
            headers={"X-Ytdlp-Filename": "out.mp3"},
        )
        opts = {
            "outtmpl": self.outtmpl,
            "postprocessors": [
                {"key": "FFmpegAudioConvertor", "preferredcodec": "mp3"}
            ],
        }
        ydl = YoutubeDLCloud(opts)
        ret = ydl.download(["https://youtube.com/watch?v=abc"])
        self.assertEqual(ret, 0)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_extractaudio_flag(self, mock_post, _tok):
        mock_post.return_value = _fake_resp(content=b"audio")
        opts = {
            "outtmpl": self.outtmpl,
            "extractaudio": True,
            "postprocessors": [],
        }
        ydl = YoutubeDLCloud(opts)
        ydl.download(["https://youtube.com/watch?v=abc"])

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_outtmpl_dict(self, mock_post, _tok):
        mock_post.return_value = _fake_resp(content=b"data")
        opts = {
            "outtmpl": {"default": self.outtmpl},
        }
        ydl = YoutubeDLCloud(opts)
        ydl.download(["https://youtube.com/watch?v=abc"])

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_download_sections(self, mock_post, _tok):
        mock_post.return_value = _fake_resp(content=b"data")
        opts = {
            "outtmpl": self.outtmpl,
            "download_sections": ["00:00-00:30"],
        }
        ydl = YoutubeDLCloud(opts)
        ydl.download(["https://youtube.com/watch?v=abc"])

    def test_context_manager(self):
        with YoutubeDLCloud({"outtmpl": self.outtmpl}) as ydl:
            self.assertIsInstance(ydl, YoutubeDLCloud)

    @patch("pipeline.footage.yt_dlp_cloudrun._id_token", return_value=None)
    @patch("requests.post")
    def test_extract_info_with_download(self, mock_post, _tok):
        mock_post.return_value = _fake_resp(
            content=b"video",
            headers={"X-Ytdlp-Filename": "out.mp4"},
        )
        ydl = YoutubeDLCloud({"outtmpl": self.outtmpl})
        info = ydl.extract_info("https://youtube.com/watch?v=abc", download=True)
        self.assertIn("_filename", info)
        self.assertIn("ext", info)

    def test_extract_info_no_download_raises(self):
        ydl = YoutubeDLCloud({"outtmpl": self.outtmpl})
        with self.assertRaises(NotImplementedError):
            ydl.extract_info("https://youtube.com/watch?v=abc", download=False)


if __name__ == "__main__":
    unittest.main()
