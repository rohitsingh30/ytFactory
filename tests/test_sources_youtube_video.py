"""Tests for pipeline.sources.youtube_video."""

from __future__ import annotations

import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pipeline
import pipeline.transcribe as _pipeline_transcribe

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.sources import youtube_video


class ExtractVideoIdTest(unittest.TestCase):
    def test_watch_url(self):
        self.assertEqual(
            youtube_video._extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_youtu_be_url(self):
        self.assertEqual(
            youtube_video._extract_video_id("https://youtu.be/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_shorts_url(self):
        self.assertEqual(
            youtube_video._extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_embed_url(self):
        self.assertEqual(
            youtube_video._extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_v_path_url(self):
        self.assertEqual(
            youtube_video._extract_video_id("https://www.youtube.com/v/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_bare_id(self):
        self.assertEqual(
            youtube_video._extract_video_id("dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            youtube_video._extract_video_id("not-a-valid-id-at-all")

    def test_short_bare_id_raises(self):
        with self.assertRaises(ValueError):
            youtube_video._extract_video_id("tooshort")


class FetchOembedMetaTest(unittest.TestCase):
    def test_success(self):
        resp = Mock()
        resp.raise_for_status = Mock()
        resp.json.return_value = {"title": "My Video", "author_name": "Creator"}
        with patch.object(youtube_video.requests, "get", return_value=resp):
            meta = youtube_video._fetch_oembed_meta("dQw4w9WgXcQ")
        self.assertEqual(meta["title"], "My Video")
        self.assertEqual(meta["author_name"], "Creator")

    def test_failure_returns_empty_dict(self):
        from requests import RequestException
        with patch.object(youtube_video.requests, "get", side_effect=RequestException("boom")):
            meta = youtube_video._fetch_oembed_meta("dQw4w9WgXcQ")
        self.assertEqual(meta, {})

    def test_http_error_returns_empty_dict(self):
        resp = Mock()
        from requests import HTTPError
        resp.raise_for_status.side_effect = HTTPError("404")
        with patch.object(youtube_video.requests, "get", return_value=resp):
            meta = youtube_video._fetch_oembed_meta("dQw4w9WgXcQ")
        self.assertEqual(meta, {})


class CaptionsViaYouTubeTranscriptApiTest(unittest.TestCase):
    def test_import_error_returns_none(self):
        with patch.dict(sys.modules, {"youtube_transcript_api": None}):
            # Force re-import by removing the cached module
            result = youtube_video._captions_via_youtube_transcript_api("vid123", ["en"])
        self.assertIsNone(result)

    def test_v1_api_get_transcript_classmethod(self):
        """v1-style API: YouTubeTranscriptApi.get_transcript is a class method."""
        chunks = [{"text": "Hello world\n", "start": 0.0, "duration": 1.0},
                  {"text": "  Next line  ", "start": 1.0, "duration": 1.0},
                  {"text": "", "start": 2.0, "duration": 1.0}]  # empty text skipped

        mock_class = Mock()
        mock_class.get_transcript = Mock(return_value=chunks)
        mock_module = Mock()
        mock_module.YouTubeTranscriptApi = mock_class

        with patch.dict(sys.modules, {"youtube_transcript_api": mock_module}):
            result = youtube_video._captions_via_youtube_transcript_api("vid123", ["en"])

        self.assertIsNotNone(result)
        self.assertIn("Hello world", result)
        self.assertIn("Next line", result)

    def test_v0_api_instance_fetch(self):
        """v0-style API: instance method fetch() returning snippets."""
        snippet1 = Mock()
        snippet1.text = "First snippet"
        snippet2 = Mock()
        snippet2.text = "Second snippet"
        snippet3 = Mock()
        snippet3.text = ""  # empty → skipped

        transcript_obj = Mock()
        transcript_obj.snippets = [snippet1, snippet2, snippet3]

        # v0 class: no get_transcript attribute
        class FakeYTAv0:
            def fetch(self, video_id, languages=None):
                return transcript_obj

        mock_module = Mock()
        mock_module.YouTubeTranscriptApi = FakeYTAv0

        with patch.dict(sys.modules, {"youtube_transcript_api": mock_module}):
            result = youtube_video._captions_via_youtube_transcript_api("vid123", ["en"])

        self.assertIsNotNone(result)
        self.assertIn("First snippet", result)
        self.assertIn("Second snippet", result)

    def test_exception_returns_none(self):
        mock_class = Mock()
        mock_class.get_transcript = Mock(side_effect=Exception("no captions"))
        mock_module = Mock()
        mock_module.YouTubeTranscriptApi = mock_class

        with patch.dict(sys.modules, {"youtube_transcript_api": mock_module}):
            result = youtube_video._captions_via_youtube_transcript_api("vid123", ["en"])
        self.assertIsNone(result)


class AudioViaYtDlpTest(unittest.TestCase):
    def test_dest_already_exists_returns_dest(self):
        dest = Mock(spec=Path)
        dest.exists.return_value = True
        result = youtube_video._audio_via_yt_dlp("vid123", dest)
        self.assertEqual(result, dest)

    def test_download_success_file_written(self):
        dest = Mock(spec=Path)
        dest.exists.side_effect = [False, True]  # pre-download: missing; post-download: present
        dest.with_suffix.return_value = Mock()
        with patch("subprocess.run"):
            result = youtube_video._audio_via_yt_dlp("vid123", dest)
        self.assertEqual(result, dest)

    def test_download_success_file_not_written_returns_none(self):
        dest = Mock(spec=Path)
        dest.exists.side_effect = [False, False]  # missing both before and after
        dest.with_suffix.return_value = Mock()
        with patch("subprocess.run"):
            result = youtube_video._audio_via_yt_dlp("vid123", dest)
        self.assertIsNone(result)

    def test_yt_dlp_not_on_path_returns_none(self):
        dest = Mock(spec=Path)
        dest.exists.return_value = False
        dest.with_suffix.return_value = Mock()
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = youtube_video._audio_via_yt_dlp("vid123", dest)
        self.assertIsNone(result)

    def test_yt_dlp_process_error_returns_none(self):
        dest = Mock(spec=Path)
        dest.exists.return_value = False
        dest.with_suffix.return_value = Mock()
        err = subprocess.CalledProcessError(1, ["yt-dlp"])
        with patch("subprocess.run", side_effect=err):
            result = youtube_video._audio_via_yt_dlp("vid123", dest)
        self.assertIsNone(result)


class TranscribeWithWhisperTest(unittest.TestCase):
    def test_import_error_returns_none(self):
        """Make 'from pipeline import transcribe' raise by hiding the attribute."""
        saved = getattr(pipeline, "transcribe", None)
        try:
            if hasattr(pipeline, "transcribe"):
                delattr(pipeline, "transcribe")
            with patch.dict(sys.modules, {"pipeline.transcribe": None}):
                result = youtube_video._transcribe_with_whisper(Path("audio.mp3"))
        finally:
            if saved is not None:
                pipeline.transcribe = saved
        self.assertIsNone(result)

    def test_transcribe_success(self):
        mock_result = {
            "segments": [
                {"text": "Hello"},
                {"text": " world"},
                {"text": ""},   # empty → skipped in join
                {"text": "!"},
            ]
        }
        with patch.object(_pipeline_transcribe, "transcribe", return_value=mock_result):
            result = youtube_video._transcribe_with_whisper(Path("audio.mp3"))
        self.assertIsNotNone(result)
        self.assertIn("Hello", result)
        self.assertIn("world", result)

    def test_transcribe_exception_returns_none(self):
        with patch.object(_pipeline_transcribe, "transcribe",
                          side_effect=RuntimeError("whisper broke")):
            result = youtube_video._transcribe_with_whisper(Path("audio.mp3"))
        self.assertIsNone(result)


class FetchTest(unittest.TestCase):
    def _mock_oembed(self, title="My Video", author="Creator"):
        return patch.object(
            youtube_video, "_fetch_oembed_meta",
            return_value={"title": title, "author_name": author},
        )

    def test_success_with_captions(self):
        with self._mock_oembed():
            with patch.object(youtube_video, "_captions_via_youtube_transcript_api",
                               return_value="Hello world transcript text"):
                out = youtube_video.fetch("dQw4w9WgXcQ")
        self.assertEqual(len(out), 1)
        story = out[0]
        self.assertEqual(story.title, "My Video")
        self.assertIn("Hello world transcript text", story.body)
        self.assertEqual(story.source, "youtube_video")
        self.assertEqual(story.metadata["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(story.metadata["author"], "Creator")

    def test_no_captions_no_whisper_raises(self):
        with self._mock_oembed():
            with patch.object(youtube_video, "_captions_via_youtube_transcript_api",
                               return_value=None):
                with self.assertRaises(RuntimeError):
                    youtube_video.fetch("dQw4w9WgXcQ", use_whisper_fallback=False)

    def test_no_captions_with_whisper_fallback_success(self):
        audio_path = Mock(spec=Path)
        audio_path.exists.return_value = True

        with self._mock_oembed():
            with patch.object(youtube_video, "_captions_via_youtube_transcript_api",
                               return_value=None):
                with patch.object(youtube_video, "_audio_via_yt_dlp",
                                   return_value=audio_path):
                    with patch.object(youtube_video, "_transcribe_with_whisper",
                                       return_value="Whisper transcript here"):
                        out = youtube_video.fetch("dQw4w9WgXcQ",
                                                  use_whisper_fallback=True)
        self.assertEqual(len(out), 1)
        self.assertIn("Whisper transcript", out[0].body)

    def test_no_captions_whisper_no_audio_raises(self):
        with self._mock_oembed():
            with patch.object(youtube_video, "_captions_via_youtube_transcript_api",
                               return_value=None):
                with patch.object(youtube_video, "_audio_via_yt_dlp",
                                   return_value=None):
                    with self.assertRaises(RuntimeError):
                        youtube_video.fetch("dQw4w9WgXcQ", use_whisper_fallback=True)

    def test_oembed_missing_falls_back_to_video_id_title(self):
        with patch.object(youtube_video, "_fetch_oembed_meta", return_value={}):
            with patch.object(youtube_video, "_captions_via_youtube_transcript_api",
                               return_value="Some transcript"):
                out = youtube_video.fetch("dQw4w9WgXcQ")
        self.assertIn("dQw4w9WgXcQ", out[0].title)

    def test_whitespace_collapsed_in_transcript(self):
        with self._mock_oembed():
            with patch.object(youtube_video, "_captions_via_youtube_transcript_api",
                               return_value="Hello   world  \n  test"):
                out = youtube_video.fetch("dQw4w9WgXcQ")
        self.assertNotIn("   ", out[0].body)

    def test_url_from_url_string(self):
        with self._mock_oembed():
            with patch.object(youtube_video, "_captions_via_youtube_transcript_api",
                               return_value="transcript"):
                out = youtube_video.fetch("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(out[0].metadata["video_id"], "dQw4w9WgXcQ")


class MainTest(unittest.TestCase):
    def test_main_prints_result(self):
        from pipeline.sources.base import RawStory
        story = RawStory(
            slug="yt-test",
            title="Test Video",
            body="Test transcript",
            source="youtube_video",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            metadata={"transcript_chars": 15, "video_id": "dQw4w9WgXcQ"},
        )
        orig_argv = sys.argv[:]
        sys.argv = ["youtube_video", "https://youtu.be/dQw4w9WgXcQ",
                    "--out", "data/intermediate", "--channel", "reddit_video"]
        try:
            with patch.object(youtube_video, "fetch", return_value=[story]):
                with patch("pipeline.sources.youtube_video.save_raw", return_value="/fake"):
                    youtube_video.main()
        finally:
            sys.argv = orig_argv

    def test_main_with_whisper_flag(self):
        from pipeline.sources.base import RawStory
        story = RawStory(slug="yt-test", title="T", body="B", source="youtube_video",
                         url="http://u", metadata={"transcript_chars": 1})
        orig_argv = sys.argv[:]
        sys.argv = ["youtube_video", "dQw4w9WgXcQ",
                    "--whisper-fallback", "--languages", "en,fr",
                    "--out", "data/intermediate", "--channel", "reddit_video"]
        try:
            with patch.object(youtube_video, "fetch", return_value=[story]) as mock_fetch:
                with patch("pipeline.sources.youtube_video.save_raw", return_value="/fake"):
                    youtube_video.main()
            call_kwargs = mock_fetch.call_args
            self.assertTrue(call_kwargs.kwargs.get("use_whisper_fallback"))
        finally:
            sys.argv = orig_argv


if __name__ == "__main__":
    unittest.main()
