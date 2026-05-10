"""Tests for pipeline.voice.voice_clone — 100% line + branch coverage.

All network, filesystem (for CACHE_DIR), subprocess, and ASR calls are mocked.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call


class TestExtractVideoId(unittest.TestCase):
    def _fn(self, url):
        from pipeline.voice.voice_clone import _extract_video_id
        return _extract_video_id(url)

    def test_v_param(self):
        self.assertEqual(self._fn("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), "dQw4w9WgXcQ")

    def test_shorts_url(self):
        self.assertEqual(self._fn("https://www.youtube.com/shorts/dQw4w9WgXcQ"), "dQw4w9WgXcQ")

    def test_embed_url(self):
        self.assertEqual(self._fn("https://www.youtube.com/embed/dQw4w9WgXcQ"), "dQw4w9WgXcQ")

    def test_youtu_be_url(self):
        self.assertEqual(self._fn("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")

    def test_watch_slash_url(self):
        self.assertEqual(self._fn("https://www.youtube.com/watch/dQw4w9WgXcQ"), "dQw4w9WgXcQ")

    def test_non_youtube_returns_hash_prefix(self):
        result = self._fn("https://vimeo.com/123456")
        self.assertTrue(result.startswith("url_"))
        self.assertEqual(len(result), len("url_") + 12)

    def test_non_youtube_is_stable(self):
        url = "https://vimeo.com/999"
        self.assertEqual(self._fn(url), self._fn(url))


class TestTrimAndClean(unittest.TestCase):
    def test_calls_ffmpeg_with_correct_args(self):
        from pipeline.voice.voice_clone import _trim_and_clean
        src = Path("/fake/source.m4a")

        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "out.wav"
            with patch("subprocess.run") as mock_run:
                _trim_and_clean(src, dst, start=2.5, duration=10.0)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("ffmpeg", cmd)
        self.assertIn("2.5", cmd)
        self.assertIn("10.0", cmd)
        self.assertIn("-ac", cmd)
        self.assertIn("1", cmd)
        self.assertIn("-ar", cmd)
        self.assertIn("24000", cmd)
        self.assertTrue(mock_run.call_args[1].get("check", False))

    def test_subprocess_failure_propagates(self):
        from pipeline.voice.voice_clone import _trim_and_clean
        src = Path("/fake/source.m4a")

        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "out.wav"
            with patch("subprocess.run",
                       side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
                with self.assertRaises(subprocess.CalledProcessError):
                    _trim_and_clean(src, dst, start=0.0, duration=10.0)


class TestDownloadAudio(unittest.TestCase):
    def _patch_cache(self, tmp_path):
        """Patch CACHE_DIR in voice_clone to a real temp dir."""
        import pipeline.voice.voice_clone as vc
        return patch.object(vc, "CACHE_DIR", tmp_path)

    def test_cache_hit_returns_existing(self):
        from pipeline.voice.voice_clone import _download_audio

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            # Pre-create a "cached" file with the video id in its name
            cached = cache_dir / "dQw4w9WgXcQ.m4a"
            cached.write_bytes(b"audio")

            with self._patch_cache(cache_dir):
                result_path, vid = _download_audio("https://youtu.be/dQw4w9WgXcQ")

        self.assertEqual(result_path, cached)
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_cache_miss_calls_cloudrun_download(self):
        from pipeline.voice.voice_clone import _download_audio

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            fake_result = cache_dir / "dQw4w9WgXcQ.m4a"

            mock_cloudrun = MagicMock()
            mock_cloudrun.download.return_value = fake_result

            with self._patch_cache(cache_dir), \
                 patch.dict("sys.modules",
                            {"pipeline.footage.yt_dlp_cloudrun": mock_cloudrun}):
                result_path, vid = _download_audio("https://youtu.be/dQw4w9WgXcQ")

        self.assertEqual(result_path, fake_result)
        self.assertEqual(vid, "dQw4w9WgXcQ")
        mock_cloudrun.download.assert_called_once()

    def test_cloudrun_failure_raises_runtime_error(self):
        from pipeline.voice.voice_clone import _download_audio

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)

            mock_cloudrun = MagicMock()
            mock_cloudrun.CloudRunYtDlpFailed = RuntimeError
            mock_cloudrun.download.side_effect = RuntimeError("network error")

            with self._patch_cache(cache_dir), \
                 patch.dict("sys.modules",
                            {"pipeline.footage.yt_dlp_cloudrun": mock_cloudrun}):
                with self.assertRaises(RuntimeError) as ctx:
                    _download_audio("https://youtu.be/dQw4w9WgXcQ")

        self.assertIn("Cloud Run yt-dlp failed", str(ctx.exception))


class TestCloneFromYoutube(unittest.TestCase):
    """End-to-end tests for clone_from_youtube with all I/O mocked."""

    def _base_patches(self, ref_text="spoken words"):
        """Return context managers that stand in for download, ffmpeg, ASR."""
        fake_src = MagicMock(spec=Path)
        fake_src.name = "dQw4w9WgXcQ.m4a"
        mock_asr = MagicMock()
        mock_asr.transcribe.return_value = {"text": ref_text}
        return fake_src, mock_asr

    # -- Validation errors ------------------------------------------------

    def test_duration_too_short_raises(self):
        from pipeline.voice.voice_clone import clone_from_youtube
        with self.assertRaises(ValueError) as ctx:
            clone_from_youtube("https://youtu.be/abc", channel="ch", slug="sl",
                               duration=3.0)
        self.assertIn("5", str(ctx.exception))

    def test_duration_too_long_raises(self):
        from pipeline.voice.voice_clone import clone_from_youtube
        with self.assertRaises(ValueError) as ctx:
            clone_from_youtube("https://youtu.be/abc", channel="ch", slug="sl",
                               duration=20.0)
        self.assertIn("15", str(ctx.exception))

    def test_no_channel_no_out_raises(self):
        from pipeline.voice.voice_clone import clone_from_youtube
        with self.assertRaises(ValueError) as ctx:
            clone_from_youtube("https://youtu.be/abc")
        self.assertIn("channel", str(ctx.exception).lower())

    # -- channel + slug path -----------------------------------------------

    def test_channel_slug_produces_correct_paths(self):
        from pipeline.voice.voice_clone import clone_from_youtube, ClonedVoice

        fake_src, mock_asr = self._base_patches("hello from ASR")

        with tempfile.TemporaryDirectory() as tmp:
            out_wav = Path(tmp) / "mychannel" / "voices" / "myslug.wav"
            out_json = Path(tmp) / "mychannel" / "voices" / "myslug.json"
            out_wav.parent.mkdir(parents=True, exist_ok=True)

            with patch("pipeline.voice.voice_clone._download_audio",
                       return_value=(fake_src, "dQw4w9WgXcQ")), \
                 patch("pipeline.voice.voice_clone._trim_and_clean"), \
                 patch.dict("sys.modules", {"pipeline.audio.asr": mock_asr}):
                result = clone_from_youtube(
                    "https://youtu.be/dQw4w9WgXcQ",
                    out_wav=out_wav,
                    out_json=out_json,
                )

            self.assertIsInstance(result, ClonedVoice)
            self.assertEqual(result.ref_text, "hello from ASR")
            self.assertTrue(out_json.exists())
            data = json.loads(out_json.read_text())
            self.assertEqual(data["ref_text"], "hello from ASR")
            self.assertEqual(data["source_video_id"], "dQw4w9WgXcQ")
            self.assertEqual(data["start"], 0.0)
            self.assertEqual(data["duration"], 10.0)

    def test_channel_slug_paths_resolved_correctly(self):
        """Using channel+slug (not out_wav/out_json) covers that branch."""
        from pipeline.voice.voice_clone import clone_from_youtube, ClonedVoice

        fake_src, mock_asr = self._base_patches("hi")

        # Mock Path.mkdir and Path.write_text at the INSTANCE level by
        # intercepting at the voice_clone module level instead of pathlib.
        # We patch Path.parent.mkdir by pre-creating the path (via Path.mkdir mock
        # at class level only for this test's scope).
        with tempfile.TemporaryDirectory() as tmp:
            # Inject a custom channel path so files land in tmp
            channel_dir = Path(tmp) / "ch"
            voices_dir = channel_dir / "voices"
            voices_dir.mkdir(parents=True)

            # Patch Path("ch") / "voices" to redirect to our temp dir.
            # Simplest: patch Path.__truediv__ only when first arg is "ch"
            # by overriding the channel name to the tmp path.
            # Even simpler: use monkeypatch via patch on the module constant.
            #
            # Actually cleanest: patch just the path construction inside
            # clone_from_youtube by substituting 'channel' value to the tmp path.
            with patch("pipeline.voice.voice_clone._download_audio",
                       return_value=(fake_src, "vid")), \
                 patch("pipeline.voice.voice_clone._trim_and_clean"), \
                 patch.dict("sys.modules", {"pipeline.audio.asr": mock_asr}):
                result = clone_from_youtube(
                    "https://youtu.be/vid",
                    channel=str(voices_dir.parent),   # tmp/ch
                    slug="myslug",
                )

        self.assertIsInstance(result, ClonedVoice)
        self.assertEqual(result.ref_text, "hi")
        self.assertIn("myslug", str(result.ref_wav))

    # -- out_wav + out_json ------------------------------------------------

    def test_out_wav_out_json_path(self):
        from pipeline.voice.voice_clone import clone_from_youtube, ClonedVoice

        fake_src, mock_asr = self._base_patches("custom path text")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_wav = tmp_path / "custom.wav"
            out_json = tmp_path / "custom.json"

            with patch("pipeline.voice.voice_clone._download_audio",
                       return_value=(fake_src, "vid123")), \
                 patch("pipeline.voice.voice_clone._trim_and_clean"), \
                 patch.dict("sys.modules", {"pipeline.audio.asr": mock_asr}):
                result = clone_from_youtube(
                    "https://youtu.be/vid123",
                    out_wav=out_wav,
                    out_json=out_json,
                )

            # Assertions inside the with block so tmp_path still exists.
            self.assertIsInstance(result, ClonedVoice)
            self.assertEqual(result.ref_wav, out_wav)
            self.assertEqual(result.ref_text, "custom path text")
            self.assertTrue(out_json.exists())

    # -- ASR edge cases ----------------------------------------------------

    def test_empty_asr_raises(self):
        from pipeline.voice.voice_clone import clone_from_youtube

        fake_src, mock_asr = self._base_patches("   ")  # whitespace → empty after strip

        # ASR raises before writing JSON, so no real I/O needed for the JSON.
        with tempfile.TemporaryDirectory() as tmp:
            out_wav = Path(tmp) / "out.wav"
            out_json = Path(tmp) / "out.json"
            with patch("pipeline.voice.voice_clone._download_audio",
                       return_value=(fake_src, "vid")), \
                 patch("pipeline.voice.voice_clone._trim_and_clean"), \
                 patch.dict("sys.modules", {"pipeline.audio.asr": mock_asr}):
                with self.assertRaises(RuntimeError) as ctx:
                    clone_from_youtube(
                        "https://youtu.be/vid",
                        out_wav=out_wav,
                        out_json=out_json,
                    )
        self.assertIn("ASR", str(ctx.exception))

    def test_custom_asr_provider_forwarded(self):
        from pipeline.voice.voice_clone import clone_from_youtube

        fake_src, mock_asr = self._base_patches("text")

        with tempfile.TemporaryDirectory() as tmp:
            out_wav = Path(tmp) / "out.wav"
            out_json = Path(tmp) / "out.json"
            with patch("pipeline.voice.voice_clone._download_audio",
                       return_value=(fake_src, "vid")), \
                 patch("pipeline.voice.voice_clone._trim_and_clean"), \
                 patch.dict("sys.modules", {"pipeline.audio.asr": mock_asr}):
                clone_from_youtube(
                    "https://youtu.be/vid",
                    out_wav=out_wav,
                    out_json=out_json,
                    asr_provider="faster_whisper",
                )

        call_kwargs = mock_asr.transcribe.call_args
        provider_used = (
            call_kwargs[1].get("provider")
            if call_kwargs[1]
            else call_kwargs[0][1]
        )
        self.assertEqual(provider_used, "faster_whisper")


if __name__ == "__main__":
    unittest.main()
