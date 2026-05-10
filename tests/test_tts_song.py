"""Tests for pipeline/tts/song.py — 100 % branch coverage.

Mocks: subprocess.run, urllib.request, time.sleep.
No real network, no real audio I/O.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from tests._helpers import PROJECT_ROOT  # noqa: F401

import pipeline.tts.song as _mod


# ---------------------------------------------------------------------------
# _detect_leading_silence_s
# ---------------------------------------------------------------------------

class TestDetectLeadingSilence(unittest.TestCase):
    def _run_with_stderr(self, stderr_output: str):
        mock_result = MagicMock()
        mock_result.stderr = stderr_output

        with patch("subprocess.run", return_value=mock_result) as mock_sp:
            result = _mod._detect_leading_silence_s(Path("/fake/song.wav"))
        return result, mock_sp

    def test_no_silence_returns_zero(self):
        result, _ = self._run_with_stderr("")
        self.assertEqual(result, 0.0)

    def test_leading_silence_detected(self):
        # Simulate ffmpeg silencedetect output with leading silence ending at 2.5s
        stderr = (
            "[silencedetect] silence_start: 0.001\n"
            "[silencedetect] silence_end: 2.50 | silence_duration: 2.499\n"
        )
        result, _ = self._run_with_stderr(stderr)
        self.assertAlmostEqual(result, 2.5, places=1)

    def test_silence_not_at_start_ignored(self):
        # Silence starts after 0.05s → not leading
        stderr = (
            "[silencedetect] silence_end: 4.00 | silence_duration: 1.00\n"
        )
        # start_s = 4.0 - 1.0 = 3.0, not < 0.05
        result, _ = self._run_with_stderr(stderr)
        self.assertEqual(result, 0.0)

    def test_silence_at_exact_start(self):
        # silence_end: 3.0, silence_duration: 3.0 → start_s = 0.0 < 0.05
        stderr = (
            "[silencedetect] silence_end: 3.00 | silence_duration: 3.00\n"
        )
        result, _ = self._run_with_stderr(stderr)
        self.assertAlmostEqual(result, 3.0)

    def test_subprocess_called_with_correct_args(self):
        with patch("subprocess.run", return_value=MagicMock(stderr="")) as mock_sp:
            _mod._detect_leading_silence_s(Path("/audio/test.wav"), threshold_db=-30.0)
        cmd = mock_sp.call_args[0][0]
        self.assertIn("ffmpeg", cmd)
        self.assertIn("silencedetect", " ".join(cmd))


# ---------------------------------------------------------------------------
# trim_song_for_short
# ---------------------------------------------------------------------------

class TestTrimSongForShort(unittest.TestCase):
    def _run_trim(self, **kwargs):
        in_path = kwargs.pop("in_path", Path("/in.wav"))
        out_path = kwargs.pop("out_path", Path("/out.wav"))
        max_dur = kwargs.pop("max_duration_s", 55.0)

        with patch("subprocess.run") as mock_sp, \
             patch("pathlib.Path.mkdir"):
            result = _mod.trim_song_for_short(
                in_path, out_path, max_dur, **kwargs
            )
        return result, mock_sp

    def test_basic_trim_calls_ffmpeg(self):
        (start, end), mock_sp = self._run_trim()
        self.assertEqual(start, 0.0)
        self.assertAlmostEqual(end, 55.0)
        mock_sp.assert_called_once()
        cmd = mock_sp.call_args[0][0]
        self.assertIn("ffmpeg", cmd)

    def test_explicit_trim_start(self):
        (start, end), _ = self._run_trim(trim_start_s=2.5, max_duration_s=50.0)
        self.assertAlmostEqual(start, 2.5)
        self.assertAlmostEqual(end, 52.5)

    def test_drop_leading_silence_calls_detect(self):
        with patch("subprocess.run") as mock_sp, \
             patch("pathlib.Path.mkdir"), \
             patch.object(_mod, "_detect_leading_silence_s", return_value=1.8) as mock_det:
            result = _mod.trim_song_for_short(
                Path("/in.wav"), Path("/out.wav"), 55.0,
                drop_leading_silence=True,
            )
        mock_det.assert_called_once()
        start, end = result
        self.assertAlmostEqual(start, 1.8)

    def test_drop_leading_silence_ignored_when_explicit_start(self):
        """explicit trim_start_s > 0 → skip auto-detect."""
        with patch("subprocess.run"), \
             patch("pathlib.Path.mkdir"), \
             patch.object(_mod, "_detect_leading_silence_s") as mock_det:
            _mod.trim_song_for_short(
                Path("/in.wav"), Path("/out.wav"), 55.0,
                trim_start_s=3.0,
                drop_leading_silence=True,
            )
        # detect should NOT be called because explicit_start > 0
        mock_det.assert_not_called()

    def test_drop_vocal_pickup_adds_silenceremove_filter(self):
        _, mock_sp = self._run_trim(drop_vocal_pickup=True)
        cmd = mock_sp.call_args[0][0]
        self.assertIn("-af", cmd)
        af_str = cmd[cmd.index("-af") + 1]
        self.assertIn("silenceremove", af_str)

    def test_fade_out_adds_afade_filter(self):
        _, mock_sp = self._run_trim(fade_out_s=2.0)
        cmd = mock_sp.call_args[0][0]
        self.assertIn("-af", cmd)
        af_str = cmd[cmd.index("-af") + 1]
        self.assertIn("afade", af_str)

    def test_both_filters_chained(self):
        _, mock_sp = self._run_trim(drop_vocal_pickup=True, fade_out_s=1.5)
        cmd = mock_sp.call_args[0][0]
        af_str = cmd[cmd.index("-af") + 1]
        self.assertIn("silenceremove", af_str)
        self.assertIn("afade", af_str)

    def test_no_filter_when_neither_option(self):
        _, mock_sp = self._run_trim()
        cmd = mock_sp.call_args[0][0]
        self.assertNotIn("-af", cmd)

    def test_returns_tuple_of_start_end(self):
        result, _ = self._run_trim(trim_start_s=1.0, max_duration_s=50.0)
        self.assertEqual(len(result), 2)
        start, end = result
        self.assertAlmostEqual(start, 1.0)
        self.assertAlmostEqual(end, 51.0)


# ---------------------------------------------------------------------------
# synth_via_sunoapi
# ---------------------------------------------------------------------------

class TestSynthViaSunoapiNoKey(unittest.TestCase):
    def test_no_api_key_raises_runtime_error(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("SUNOAPI_API_KEY", None)
            with self.assertRaises(RuntimeError) as ctx:
                _mod.synth_via_sunoapi(
                    lyrics="Row row row your boat",
                    style="kids",
                    out_path=Path("/out.wav"),
                )
        self.assertIn("SUNOAPI_API_KEY", str(ctx.exception))


class TestSynthViaSunoapiGenerateError(unittest.TestCase):
    def setUp(self):
        os.environ["SUNOAPI_API_KEY"] = "fake-key"

    def tearDown(self):
        os.environ.pop("SUNOAPI_API_KEY", None)

    def test_http_error_on_generate_raises(self):
        err = urllib.error.HTTPError(
            url="u", code=401, msg="Unauthorized", hdrs=None,
            fp=io.BytesIO(b"bad auth"),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.synth_via_sunoapi(
                    lyrics="la la la",
                    style="pop",
                    out_path=Path("/out.wav"),
                )
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_no_task_id_in_response_raises(self):
        resp_mock = MagicMock()
        resp_mock.read.return_value = json.dumps({"data": {}}).encode()
        resp_mock.__enter__ = MagicMock(return_value=resp_mock)
        resp_mock.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=resp_mock):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.synth_via_sunoapi("la", "pop", Path("/out.wav"))
        self.assertIn("taskId", str(ctx.exception))


class TestSynthViaSunoapiPollSuccess(unittest.TestCase):
    def setUp(self):
        os.environ["SUNOAPI_API_KEY"] = "fake-key"

    def tearDown(self):
        os.environ.pop("SUNOAPI_API_KEY", None)

    def _make_generate_response(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"data": {"taskId": "task-123"}}
        ).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def _make_poll_response(self, status, audio_url=None):
        data = {
            "data": {
                "status": status,
                "response": {
                    "sunoData": [{"audioUrl": audio_url or "https://cdn.example.com/song.mp3"}]
                } if status == "SUCCESS" else {},
            }
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(data).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def _make_audio_response(self, content=b"ID3fake"):
        resp = MagicMock()
        resp.read.return_value = content
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_poll_success_wav_output(self):
        """Poll SUCCESS with a .wav out_path → ffmpeg transcode from mp3."""
        out = Path("/fake/song.wav")
        gen_resp = self._make_generate_response()
        poll_resp = self._make_poll_response("SUCCESS", "https://cdn.example.com/song.mp3")
        audio_resp = self._make_audio_response()

        urlopen_calls = [gen_resp, poll_resp, audio_resp]

        with patch("urllib.request.urlopen", side_effect=urlopen_calls), \
             patch("subprocess.run") as mock_sp, \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_bytes"), \
             patch("time.sleep"):
            result = _mod.synth_via_sunoapi("la la", "pop", out, poll_interval_s=0)

        self.assertEqual(result, out)
        # ffmpeg transcode called for wav output
        mock_sp.assert_called_once()

    def test_poll_success_mp3_output_renamed(self):
        """Poll SUCCESS with a .mp3 out_path → just rename."""
        out = Path("/fake/song.mp3")
        gen_resp = self._make_generate_response()
        poll_resp = self._make_poll_response("SUCCESS")
        audio_resp = self._make_audio_response()

        with patch("urllib.request.urlopen", side_effect=[gen_resp, poll_resp, audio_resp]), \
             patch("subprocess.run"), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_bytes"), \
             patch("pathlib.Path.rename"), \
             patch("time.sleep"):
            result = _mod.synth_via_sunoapi("la la", "pop", out, poll_interval_s=0)

        self.assertEqual(result, out)

    def test_poll_failed_raises(self):
        gen_resp = self._make_generate_response()
        poll_resp = self._make_poll_response("FAILED")

        with patch("urllib.request.urlopen", side_effect=[gen_resp, poll_resp]), \
             patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.synth_via_sunoapi("la", "pop", Path("/o.wav"), poll_interval_s=0)
        self.assertIn("failed", str(ctx.exception).lower())

    def test_poll_error_status_raises(self):
        gen_resp = self._make_generate_response()
        poll_resp = self._make_poll_response("ERROR")

        with patch("urllib.request.urlopen", side_effect=[gen_resp, poll_resp]), \
             patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                _mod.synth_via_sunoapi("la", "pop", Path("/o.wav"), poll_interval_s=0)

    def test_poll_create_task_failed(self):
        gen_resp = self._make_generate_response()
        poll_resp = self._make_poll_response("CREATE_TASK_FAILED")

        with patch("urllib.request.urlopen", side_effect=[gen_resp, poll_resp]), \
             patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                _mod.synth_via_sunoapi("la", "pop", Path("/o.wav"), poll_interval_s=0)

    def test_poll_http_error_continues_retrying(self):
        """A poll-time HTTP error is logged and retried."""
        gen_resp = self._make_generate_response()
        poll_err = urllib.error.HTTPError(
            url="u", code=500, msg="Server Error", hdrs=None,
            fp=io.BytesIO(b""),
        )
        poll_success = self._make_poll_response("SUCCESS")
        audio_resp = self._make_audio_response()

        with patch("urllib.request.urlopen",
                   side_effect=[gen_resp, poll_err, poll_success, audio_resp]), \
             patch("subprocess.run"), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_bytes"), \
             patch("time.sleep"):
            result = _mod.synth_via_sunoapi("la", "pop", Path("/o.wav"), poll_interval_s=0)
        # Should succeed after retrying past the poll error
        self.assertEqual(result, Path("/o.wav"))

    def test_timeout_raises(self):
        gen_resp = self._make_generate_response()
        # Never return SUCCESS — always PENDING
        pending_resp = MagicMock()
        pending_resp.read.return_value = json.dumps(
            {"data": {"status": "PENDING", "response": {}}}
        ).encode()
        pending_resp.__enter__ = MagicMock(return_value=pending_resp)
        pending_resp.__exit__ = MagicMock(return_value=False)

        # Use a very short timeout
        with patch("urllib.request.urlopen",
                   side_effect=[gen_resp] + [pending_resp] * 100), \
             patch("time.sleep"), \
             patch("time.time", side_effect=[0.0] + [i * 10 for i in range(200)]):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.synth_via_sunoapi("la", "pop", Path("/o.wav"),
                                       poll_timeout_s=5, poll_interval_s=0)
        self.assertIn("timed out", str(ctx.exception))

    def test_success_empty_sunodata_raises(self):
        gen_resp = self._make_generate_response()
        empty_data = {"data": {"status": "SUCCESS", "response": {"sunoData": []}}}
        poll_resp = MagicMock()
        poll_resp.read.return_value = json.dumps(empty_data).encode()
        poll_resp.__enter__ = MagicMock(return_value=poll_resp)
        poll_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", side_effect=[gen_resp, poll_resp]), \
             patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.synth_via_sunoapi("la", "pop", Path("/o.wav"), poll_interval_s=0)
        self.assertIn("empty sunoData", str(ctx.exception))

    def test_success_no_audio_url_raises(self):
        gen_resp = self._make_generate_response()
        no_url_data = {
            "data": {
                "status": "SUCCESS",
                "response": {"sunoData": [{"audioUrl": None}]}
            }
        }
        poll_resp = MagicMock()
        poll_resp.read.return_value = json.dumps(no_url_data).encode()
        poll_resp.__enter__ = MagicMock(return_value=poll_resp)
        poll_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", side_effect=[gen_resp, poll_resp]), \
             patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                _mod.synth_via_sunoapi("la", "pop", Path("/o.wav"), poll_interval_s=0)
        self.assertIn("audioUrl", str(ctx.exception))

    def test_log_elapsed_every_30s(self):
        """Elapsed % 30 == 0 → print statement runs."""
        gen_resp = self._make_generate_response()
        pending = MagicMock()
        pending.read.return_value = json.dumps(
            {"data": {"status": "PENDING"}}
        ).encode()
        pending.__enter__ = MagicMock(return_value=pending)
        pending.__exit__ = MagicMock(return_value=False)

        poll_success = self._make_poll_response("SUCCESS")
        audio_resp = self._make_audio_response()

        # time.time: first call sets deadline, then after one pending returns
        # elapsed that's divisible by 30
        time_seq = [0.0, 0.0, 270.0, 270.0, 270.0, 270.0, 270.0]

        with patch("urllib.request.urlopen",
                   side_effect=[gen_resp, pending, poll_success, audio_resp]), \
             patch("time.sleep"), \
             patch("time.time", side_effect=time_seq + [999.0] * 50), \
             patch("subprocess.run"), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.write_bytes"):
            # Should complete (eventually gets SUCCESS)
            _mod.synth_via_sunoapi("la", "pop", Path("/o.wav"),
                                   poll_timeout_s=300, poll_interval_s=0)


if __name__ == "__main__":
    unittest.main()
