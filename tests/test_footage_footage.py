"""Tests for pipeline.footage.footage — 100% line coverage.

All external I/O (subprocess, filesystem, env) is mocked so no real
yt-dlp or ffmpeg binary is required.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.footage.footage import (
    FootageClip,
    _download_source,
    _extract_video_id,
    _ffprobe_duration,
    _has_audio_stream,
    fetch_clip,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_run_factory(
    create_out: bool = True,
    trim_returncode: int = 0,
    intro_returncode: int = 0,
    concat_returncode: int = 0,
    probe_stdout: str = "10.0\n",
):
    """Return a subprocess.run side_effect that creates output files."""
    calls: list[list] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        first = cmd[0] if cmd else ""
        if first == "ffprobe":
            return MagicMock(returncode=0, stdout=probe_stdout, stderr="")
        # ffmpeg — figure out which call this is
        cmd_str = " ".join(cmd)
        if "anullsrc" in cmd_str:
            # black-intro generation
            if intro_returncode != 0:
                return MagicMock(returncode=intro_returncode, stdout="", stderr="intro err")
            if create_out:
                out = Path(cmd[-1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"intro-mp4")
            return MagicMock(returncode=0, stdout="", stderr="")
        if "concat" in cmd_str:
            # concat pass
            if concat_returncode != 0:
                return MagicMock(returncode=concat_returncode, stdout="", stderr="concat err")
            if create_out:
                out = Path(cmd[-1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"concat-mp4")
            return MagicMock(returncode=0, stdout="", stderr="")
        # main trim ffmpeg
        if trim_returncode != 0:
            return MagicMock(returncode=trim_returncode, stdout="", stderr="ffmpeg err")
        if create_out:
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fake-mp4-data")
        return MagicMock(returncode=0, stdout="", stderr="")

    return fake_run


# ---------------------------------------------------------------------------
# _extract_video_id
# ---------------------------------------------------------------------------

class TestExtractVideoId(unittest.TestCase):
    def test_v_param(self):
        vid = _extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_shorts_url(self):
        vid = _extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_youtu_be(self):
        vid = _extract_video_id("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_embed_url(self):
        vid = _extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_v_path(self):
        vid = _extract_video_id("https://www.youtube.com/v/dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_bare_id(self):
        vid = _extract_video_id("dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            _extract_video_id("https://example.com/not-a-youtube-url")

    def test_invalid_short_raises(self):
        with self.assertRaises(ValueError):
            _extract_video_id("short")


# ---------------------------------------------------------------------------
# _ffprobe_duration
# ---------------------------------------------------------------------------

class TestFfprobeDuration(unittest.TestCase):
    def test_basic(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="12.345\n")
            result = _ffprobe_duration(Path("/fake/video.mp4"))
        self.assertAlmostEqual(result, 12.345)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], "ffprobe")


# ---------------------------------------------------------------------------
# _has_audio_stream
# ---------------------------------------------------------------------------

class TestHasAudioStream(unittest.TestCase):
    def test_has_audio(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="0\n")
            result = _has_audio_stream(Path("/fake/video.mp4"))
        self.assertTrue(result)

    def test_no_audio(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = _has_audio_stream(Path("/fake/video.mp4"))
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# _download_source
# ---------------------------------------------------------------------------

class TestDownloadSource(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cache = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_cached_returns_immediately(self):
        dest = self.cache / "dQw4w9WgXcQ.mp4"
        dest.write_bytes(b"cached")
        result = _download_source(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
        )
        self.assertEqual(result, dest)

    def test_anonymous_download_success(self):
        """subprocess.run succeeds, file created → return path."""
        dest = self.cache / "dQw4w9WgXcQ.mp4"

        def fake_run(cmd, *args, **kwargs):
            dest.write_bytes(b"downloaded")
            return MagicMock(returncode=0)

        env = {"YTFACTORY_YTDLP_COOKIES": "", "YTFACTORY_YTDLP_BROWSER": ""}
        with patch("subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, env, clear=False):
            result = _download_source(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
            )
        self.assertEqual(result, dest)

    def test_cookies_file_used_when_exists(self):
        """YTFACTORY_YTDLP_COOKIES points to existing file → --cookies arg used."""
        dest = self.cache / "dQw4w9WgXcQ.mp4"
        cookies = self.cache / "cookies.txt"
        cookies.write_text("# Netscape cookies")

        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            dest.write_bytes(b"downloaded")
            return MagicMock(returncode=0)

        env = {
            "YTFACTORY_YTDLP_COOKIES": str(cookies),
            "YTFACTORY_YTDLP_BROWSER": "",
        }
        with patch("subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, env, clear=False):
            result = _download_source(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
            )
        self.assertEqual(result, dest)
        self.assertTrue(any("--cookies" in " ".join(c) for c in calls))

    def test_cookies_file_missing_falls_through(self):
        """YTFACTORY_YTDLP_COOKIES set but file missing → falls through to browser."""
        dest = self.cache / "dQw4w9WgXcQ.mp4"

        def fake_run(cmd, *args, **kwargs):
            dest.write_bytes(b"downloaded")
            return MagicMock(returncode=0)

        env = {
            "YTFACTORY_YTDLP_COOKIES": "/nonexistent/cookies.txt",
            "YTFACTORY_YTDLP_BROWSER": "",
        }
        with patch("subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, env, clear=False):
            result = _download_source(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
            )
        self.assertEqual(result, dest)

    def test_cookies_fail_retries_browser(self):
        """Cookies auth fails → retries with browser extraction."""
        dest = self.cache / "dQw4w9WgXcQ.mp4"
        cookies = self.cache / "cookies.txt"
        cookies.write_text("# Netscape cookies")
        call_count = [0]

        def fake_run(cmd, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise subprocess.CalledProcessError(1, cmd, stderr="auth fail")
            dest.write_bytes(b"downloaded")
            return MagicMock(returncode=0)

        env = {
            "YTFACTORY_YTDLP_COOKIES": str(cookies),
            "YTFACTORY_YTDLP_BROWSER": "chrome",
        }
        with patch("subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, env, clear=False):
            result = _download_source(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
            )
        self.assertEqual(result, dest)

    def test_all_methods_fail_raises(self):
        """All cookie strategies fail → RuntimeError."""
        def fake_run(cmd, *args, **kwargs):
            raise subprocess.CalledProcessError(1, cmd, stderr="blocked")

        env = {
            "YTFACTORY_YTDLP_COOKIES": "",
            "YTFACTORY_YTDLP_BROWSER": "",
        }
        with patch("subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                _download_source(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
                )
        self.assertIn("yt-dlp could not download", str(ctx.exception))

    def test_browser_cookies_fail_anon_succeeds(self):
        """Both cookies file and browser extraction fail → anonymous download succeeds (line 170)."""
        dest = self.cache / "dQw4w9WgXcQ.mp4"
        cookies = self.cache / "cookies.txt"
        cookies.write_text("# Netscape cookies")
        call_count = [0]

        def fake_run(cmd, *args, **kwargs):
            call_count[0] += 1
            # First two calls (cookies file + browser) both fail
            if call_count[0] <= 2:
                raise subprocess.CalledProcessError(1, cmd, stderr="auth fail")
            # Third call (anonymous) succeeds
            dest.write_bytes(b"downloaded")
            return MagicMock(returncode=0)

        env = {
            "YTFACTORY_YTDLP_COOKIES": str(cookies),
            "YTFACTORY_YTDLP_BROWSER": "chrome",
        }
        with patch("subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, env, clear=False):
            result = _download_source(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
            )
        self.assertEqual(result, dest)

    def test_no_module_falls_back_to_binary(self):
        """CalledProcessError with 'No module named' → tries binary yt-dlp."""
        dest = self.cache / "dQw4w9WgXcQ.mp4"
        call_count = [0]

        def fake_run(cmd, *args, **kwargs):
            call_count[0] += 1
            if cmd[0] == sys.executable:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr="No module named yt_dlp"
                )
            # binary call succeeds
            dest.write_bytes(b"downloaded")
            return MagicMock(returncode=0)

        env = {"YTFACTORY_YTDLP_COOKIES": "", "YTFACTORY_YTDLP_BROWSER": ""}
        with patch("subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, env, clear=False):
            result = _download_source(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
            )
        self.assertEqual(result, dest)

    def test_no_module_binary_not_found_raises(self):
        """'No module named' AND yt-dlp binary not found → RuntimeError."""
        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == sys.executable:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr="No module named yt_dlp"
                )
            raise FileNotFoundError("yt-dlp not found")

        env = {"YTFACTORY_YTDLP_COOKIES": "", "YTFACTORY_YTDLP_BROWSER": ""}
        with patch("subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                _download_source(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
                )
        self.assertIn("yt-dlp not available", str(ctx.exception))

    def test_no_module_binary_fails_returns_false(self):
        """'No module named' AND binary CalledProcessError → falls back → raises."""
        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == sys.executable:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr="No module named yt_dlp"
                )
            raise subprocess.CalledProcessError(1, cmd, stderr="download failed")

        env = {"YTFACTORY_YTDLP_COOKIES": "", "YTFACTORY_YTDLP_BROWSER": ""}
        with patch("subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError):
                _download_source(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
                )

    def test_run_succeeds_no_file_raises(self):
        """subprocess.run succeeds but file not created → RuntimeError."""
        def fake_run(cmd, *args, **kwargs):
            return MagicMock(returncode=0)  # no file created

        env = {"YTFACTORY_YTDLP_COOKIES": "", "YTFACTORY_YTDLP_BROWSER": ""}
        with patch("subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                _download_source(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
                )
        self.assertIn("did not produce", str(ctx.exception))


# ---------------------------------------------------------------------------
# fetch_clip
# ---------------------------------------------------------------------------

class TestFetchClip(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.src = self.root / "sources" / "dQw4w9WgXcQ.mp4"
        self.src.parent.mkdir(parents=True)
        self.src.write_bytes(b"src-data")
        self.out = self.root / "out.mp4"

    def tearDown(self):
        self.td.cleanup()

    def _run_fetch(self, url, in_s, out_s, pre_pad_s=0.0, **kwargs):
        fake_run = _fake_run_factory()
        with patch("pipeline.footage.footage._download_source", return_value=self.src), \
             patch("pipeline.footage.footage._has_audio_stream", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            return fetch_clip(
                url=url,
                in_s=in_s,
                out_s=out_s,
                out_path=self.out,
                cache_dir=self.root / "sources",
                pre_pad_s=pre_pad_s,
                **kwargs,
            )

    def test_basic_clip(self):
        result = self._run_fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 10.0, 15.0)
        self.assertIsInstance(result, FootageClip)
        self.assertTrue(self.out.exists())

    def test_out_leq_in_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._run_fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 15.0, 10.0)
        self.assertIn("out_s", str(ctx.exception))

    def test_equal_in_out_raises(self):
        with self.assertRaises(ValueError):
            self._run_fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 10.0, 10.0)

    def test_no_audio_stream(self):
        fake_run = _fake_run_factory()
        with patch("pipeline.footage.footage._download_source", return_value=self.src), \
             patch("pipeline.footage.footage._has_audio_stream", return_value=False), \
             patch("subprocess.run", side_effect=fake_run):
            result = fetch_clip(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                5.0, 10.0, self.out, cache_dir=self.root / "sources"
            )
        self.assertFalse(result.has_audio)

    def test_custom_resolution(self):
        result = self._run_fetch(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", 0.0, 5.0,
            target_resolution=(720, 1280)
        )
        self.assertEqual(result.width, 720)
        self.assertEqual(result.height, 1280)

    def test_ffmpeg_failure_raises(self):
        fake_run = _fake_run_factory(trim_returncode=1)
        with patch("pipeline.footage.footage._download_source", return_value=self.src), \
             patch("pipeline.footage.footage._has_audio_stream", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_clip(
                    "dQw4w9WgXcQ", 0.0, 5.0, self.out,
                    cache_dir=self.root / "sources"
                )
        self.assertIn("ffmpeg failed", str(ctx.exception))

    def test_pre_pad_creates_intro(self):
        """pre_pad_s > 0.05 triggers black-intro + concat pass."""
        # Need a fake broadcast file so out_path.rename() works
        fake_run = _fake_run_factory()
        with patch("pipeline.footage.footage._download_source", return_value=self.src), \
             patch("pipeline.footage.footage._has_audio_stream", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            result = fetch_clip(
                "dQw4w9WgXcQ", 0.0, 5.0, self.out,
                cache_dir=self.root / "sources",
                pre_pad_s=1.0,
            )
        self.assertIsInstance(result, FootageClip)

    def test_pre_pad_stale_broadcast_unlinked(self):
        """Pre-existing broadcast_path file is unlinked before rename (line 329)."""
        # Pre-create the broadcast file to trigger the .unlink() branch
        bcast = self.out.parent / f"{self.out.stem}_bcast.mp4"
        bcast.write_bytes(b"stale")
        fake_run = _fake_run_factory()
        with patch("pipeline.footage.footage._download_source", return_value=self.src), \
             patch("pipeline.footage.footage._has_audio_stream", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            result = fetch_clip(
                "dQw4w9WgXcQ", 0.0, 5.0, self.out,
                cache_dir=self.root / "sources",
                pre_pad_s=1.0,
            )
        self.assertIsInstance(result, FootageClip)

    def test_pre_pad_below_threshold_skipped(self):
        """pre_pad_s <= 0.05 → no intro/concat pass."""
        call_cmds = []
        orig_factory = _fake_run_factory()

        def tracking_run(cmd, *args, **kwargs):
            call_cmds.append(list(cmd))
            return orig_factory(cmd, *args, **kwargs)

        with patch("pipeline.footage.footage._download_source", return_value=self.src), \
             patch("pipeline.footage.footage._has_audio_stream", return_value=True), \
             patch("subprocess.run", side_effect=tracking_run):
            fetch_clip(
                "dQw4w9WgXcQ", 0.0, 5.0, self.out,
                cache_dir=self.root / "sources",
                pre_pad_s=0.03,
            )
        concat_calls = [c for c in call_cmds if "concat" in " ".join(c)]
        self.assertEqual(len(concat_calls), 0)

    def test_pre_pad_intro_failure_raises(self):
        """Intro ffmpeg failing raises RuntimeError."""
        fake_run = _fake_run_factory(intro_returncode=1)
        with patch("pipeline.footage.footage._download_source", return_value=self.src), \
             patch("pipeline.footage.footage._has_audio_stream", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_clip(
                    "dQw4w9WgXcQ", 0.0, 5.0, self.out,
                    cache_dir=self.root / "sources",
                    pre_pad_s=1.0,
                )
        self.assertIn("black intro", str(ctx.exception))

    def test_pre_pad_concat_failure_raises(self):
        """Concat ffmpeg failing raises RuntimeError."""
        fake_run = _fake_run_factory(concat_returncode=1)
        with patch("pipeline.footage.footage._download_source", return_value=self.src), \
             patch("pipeline.footage.footage._has_audio_stream", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_clip(
                    "dQw4w9WgXcQ", 0.0, 5.0, self.out,
                    cache_dir=self.root / "sources",
                    pre_pad_s=1.0,
                )
        self.assertIn("concat", str(ctx.exception))

    def test_footageclip_fields(self):
        result = self._run_fetch("dQw4w9WgXcQ", 0.0, 5.0)
        self.assertIsInstance(result.path, Path)
        self.assertIsInstance(result.duration_s, float)
        self.assertIsInstance(result.width, int)
        self.assertIsInstance(result.height, int)
        self.assertIsInstance(result.has_audio, bool)


# ---------------------------------------------------------------------------
# main() — CLI entrypoint
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):
    def test_main_invokes_fetch_clip(self):
        from pipeline.footage import footage as footage_mod
        called_with = {}

        def fake_fetch(url, in_s, out_s, out_path, **kwargs):
            called_with["url"] = url
            called_with["in_s"] = in_s
            called_with["out_s"] = out_s
            return MagicMock()

        with patch.object(footage_mod, "fetch_clip", side_effect=fake_fetch), \
             patch("sys.argv", [
                 "footage", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                 "--in", "1.5", "--out", "4.5",
                 "--out-path", "data/scratch/test.mp4",
             ]):
            footage_mod.main()

        self.assertEqual(called_with["in_s"], 1.5)
        self.assertEqual(called_with["out_s"], 4.5)

    def test_main_custom_resolution(self):
        from pipeline.footage import footage as footage_mod
        called_with = {}

        def fake_fetch(url, in_s, out_s, out_path, *, target_resolution=(1080, 1920), **kwargs):
            called_with["res"] = target_resolution
            return MagicMock()

        with patch.object(footage_mod, "fetch_clip", side_effect=fake_fetch), \
             patch("sys.argv", [
                 "footage", "dQw4w9WgXcQ",
                 "--in", "0.0", "--out", "5.0",
                 "--out-path", "data/scratch/test.mp4",
                 "--width", "720", "--height", "1280",
             ]):
            footage_mod.main()

        self.assertEqual(called_with["res"], (720, 1280))
