"""Tests for pipeline.footage.footage — 100% line coverage.

All external I/O (subprocess, filesystem, env) is mocked so no real
yt-dlp or ffmpeg binary is required.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
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
    probe_stdout: str = "120.0\n",
):
    """Return a subprocess.run side_effect that creates output files.

    Audit Q2.30 — default probe_stdout bumped from 10.0 → 120.0 so
    tests using ``in_s=10..15`` clips don't trip the new in/out vs
    source-duration validation. Override per-test when a specific
    duration is needed.
    """
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
            # Audit T1.18 — yt-dlp now writes to a per-process tempfile
            # which the caller atomically renames to dest. Materialise
            # the file at whatever -o path was passed so the rename
            # succeeds.
            cmd_list = list(cmd)
            if "-o" in cmd_list:
                out_path = Path(cmd_list[cmd_list.index("-o") + 1])
                out_path.write_bytes(b"downloaded")
            return MagicMock(returncode=0)

        env = {"YTFACTORY_YTDLP_COOKIES": "", "YTFACTORY_YTDLP_BROWSER": ""}
        with patch("subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, env, clear=False):
            result = _download_source(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
            )
        self.assertEqual(result, dest)
        # T1.18 — atomic rename means dest now exists with the downloaded bytes.
        self.assertTrue(dest.exists())

    def test_cookies_file_used_when_exists(self):
        """YTFACTORY_YTDLP_COOKIES points to existing file → --cookies arg used."""
        dest = self.cache / "dQw4w9WgXcQ.mp4"
        cookies = self.cache / "cookies.txt"
        cookies.write_text("# Netscape cookies")

        calls = []

        def fake_run(cmd, *args, **kwargs):
            cmd_list = list(cmd)
            calls.append(cmd_list)
            if "-o" in cmd_list:
                Path(cmd_list[cmd_list.index("-o") + 1]).write_bytes(b"downloaded")
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
            cmd_list = list(cmd)
            if "-o" in cmd_list:
                Path(cmd_list[cmd_list.index("-o") + 1]).write_bytes(b"downloaded")
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
            cmd_list = list(cmd)
            if "-o" in cmd_list:
                Path(cmd_list[cmd_list.index("-o") + 1]).write_bytes(b"downloaded")
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
            # Third call (anonymous) succeeds — write to whatever -o
            # path was passed (T1.18 atomic-rename pattern).
            cmd_list = list(cmd)
            if "-o" in cmd_list:
                Path(cmd_list[cmd_list.index("-o") + 1]).write_bytes(b"downloaded")
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
            # binary call succeeds — write to -o path (T1.18).
            cmd_list = list(cmd)
            if "-o" in cmd_list:
                Path(cmd_list[cmd_list.index("-o") + 1]).write_bytes(b"downloaded")
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

    def test_concurrent_download_lock_prevents_race(self):
        """Audit T1.18 — two parallel _download_source calls for the
        same video_id must NOT both spawn yt-dlp on the same dest;
        the second caller must block on the lock, then short-circuit
        when it sees dest.exists()."""
        import threading
        dest = self.cache / "dQw4w9WgXcQ.mp4"
        spawn_count = [0]
        spawn_lock = threading.Lock()

        def fake_run(cmd, *args, **kwargs):
            with spawn_lock:
                spawn_count[0] += 1
            cmd_list = list(cmd)
            if "-o" in cmd_list:
                Path(cmd_list[cmd_list.index("-o") + 1]).write_bytes(b"X" * 100)
            # Slow yt-dlp simulation so the second caller actually
            # waits on the lock.
            time.sleep(0.05)
            return MagicMock(returncode=0)

        env = {"YTFACTORY_YTDLP_COOKIES": "", "YTFACTORY_YTDLP_BROWSER": ""}
        results: list[Path] = []

        def worker():
            with patch("subprocess.run", side_effect=fake_run), \
                 patch.dict(os.environ, env, clear=False):
                results.append(_download_source(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    self.cache,
                ))

        # Two threads racing for the same video.
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], dest)
        self.assertEqual(results[1], dest)
        # Critical: only ONE spawn even though two threads raced. The
        # other observed dest.exists() inside the lock and short-circuited.
        self.assertEqual(
            spawn_count[0], 1,
            f"expected exactly 1 yt-dlp spawn for the racing pair; "
            f"got {spawn_count[0]} (the lock didn't fence the race)",
        )

    def test_atomic_rename_no_poisoned_cache_on_failure(self):
        """Audit T1.18 — yt-dlp crashing mid-download must NOT leave
        a partial mp4 at dest. The pre-fix mode wrote directly to
        dest, so a half-written file would survive in the cache and
        downstream renders would happily fetch-and-truncate it."""
        dest = self.cache / "dQw4w9WgXcQ.mp4"

        def fake_run(cmd, *args, **kwargs):
            cmd_list = list(cmd)
            if "-o" in cmd_list:
                # Write a partial file, then "crash" with non-zero rc.
                Path(cmd_list[cmd_list.index("-o") + 1]).write_bytes(b"PARTIAL")
            raise subprocess.CalledProcessError(1, cmd, stderr="crashed mid-stream")

        env = {"YTFACTORY_YTDLP_COOKIES": "", "YTFACTORY_YTDLP_BROWSER": ""}
        with patch("subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError):
                _download_source(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", self.cache
                )
        # dest must NOT exist — the partial file was a tempfile,
        # cleaned up after the failure, never atomically renamed in.
        self.assertFalse(
            dest.exists(),
            f"{dest} exists after failed download — atomic rename "
            f"didn't fence the partial-file failure",
        )


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

    def test_audit_q230_in_s_past_source_duration_raises(self):
        """Audit Q2.30 — pre-fix `-ss 200 -t 10` on a 180s source
        produced 0 frames; output mp4 existed but was empty.
        Now refuse upfront with a clear error.
        """
        fake_run = _fake_run_factory(probe_stdout="180.0\n")
        with patch("pipeline.footage.footage._download_source", return_value=self.src), \
             patch("pipeline.footage.footage._has_audio_stream", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(ValueError) as ctx:
                fetch_clip(
                    url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    in_s=200.0, out_s=210.0,
                    out_path=self.out,
                    cache_dir=self.root / "sources",
                )
        self.assertIn("at or past source duration", str(ctx.exception))

    def test_audit_q230_out_s_well_past_source_duration_raises(self):
        """out_s far past src_dur (>0.1s tolerance) → refused."""
        fake_run = _fake_run_factory(probe_stdout="180.0\n")
        with patch("pipeline.footage.footage._download_source", return_value=self.src), \
             patch("pipeline.footage.footage._has_audio_stream", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(ValueError) as ctx:
                fetch_clip(
                    url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    in_s=170.0, out_s=200.0,
                    out_path=self.out,
                    cache_dir=self.root / "sources",
                )
        self.assertIn("exceeds source duration", str(ctx.exception))

    def test_audit_q230_out_s_within_tolerance_clamped(self):
        """out_s slightly past src_dur (<0.1s, e.g. one-frame rounding)
        → clamped to source duration, render proceeds."""
        fake_run = _fake_run_factory(probe_stdout="180.0\n")
        with patch("pipeline.footage.footage._download_source", return_value=self.src), \
             patch("pipeline.footage.footage._has_audio_stream", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            result = fetch_clip(
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                in_s=170.0, out_s=180.05,  # 0.05s past src_dur — tolerated
                out_path=self.out,
                cache_dir=self.root / "sources",
            )
        # Clamp succeeds; clip exists.
        self.assertIsInstance(result, FootageClip)

    def test_audit_q230_unknown_source_duration_skips_check(self):
        """If ffprobe returns 0 duration (unknown), the validation
        falls through — backward-compatible with sources whose
        duration ffprobe can't determine."""
        fake_run = _fake_run_factory(probe_stdout="0.0\n")
        with patch("pipeline.footage.footage._download_source", return_value=self.src), \
             patch("pipeline.footage.footage._has_audio_stream", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            # in_s=200 would fail with known duration; but probe_stdout=0
            # → src_dur=0 → check skipped.
            result = fetch_clip(
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                in_s=200.0, out_s=210.0,
                out_path=self.out,
                cache_dir=self.root / "sources",
            )
        self.assertIsInstance(result, FootageClip)

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
