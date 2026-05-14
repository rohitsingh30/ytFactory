"""Tests for the Tier-2 quick-win refactors (2026-05-05):

* ``pipeline/probe.py`` — memoized ffprobe duration helper that
  replaces 5+ ad-hoc subprocess call sites
* ``pipeline.images.images.reset_image_state()`` — drops _PIPE / _FLUX_PIPE
  / _ZIMAGE_PIPE singletons (former mirror of the now-removed
  ``audio.reset_f5_state()``)
* libx264 ``-threads 3`` on parallel-fanned encodes — keeps the
  4-worker fanout from oversubscribing on M2 Max (12 perf cores)

Note: the F5RefCacheLruTests class tested the local ``pipeline.tts.f5``
LRU cache. Local TTS providers were removed 2026-05-09 (laptop nuclear
cleanup) so that class is gone too.
"""
from __future__ import annotations

import subprocess
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from pipeline.images import images
from pipeline.quality.probe import (
    clear_probe_cache,
    probe_duration,
    probe_duration_or_none,
    _probe_cached,
)


def _write_silence_wav(path: Path, duration_s: float, sample_rate: int = 24_000) -> None:
    n_frames = int(duration_s * sample_rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_frames)


class ProbeDurationTests(unittest.TestCase):
    def setUp(self):
        clear_probe_cache()

    def test_returns_duration_for_real_file(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "a.wav"
            _write_silence_wav(p, 2.5)
            d = probe_duration(p)
            # ffprobe returns slightly imprecise duration for tiny WAVs;
            # ±0.1 s is plenty for the assertion.
            self.assertAlmostEqual(d, 2.5, delta=0.1)

    def test_or_none_returns_duration_for_real_file(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "a.wav"
            _write_silence_wav(p, 1.0)
            d = probe_duration_or_none(p)
            self.assertIsNotNone(d)
            self.assertAlmostEqual(d, 1.0, delta=0.1)

    def test_or_none_returns_none_for_missing_file(self):
        self.assertIsNone(probe_duration_or_none(Path("/nonexistent/file.wav")))

    def test_raises_on_missing_file(self):
        with self.assertRaises(RuntimeError):
            probe_duration(Path("/nonexistent/file.wav"))

    def test_caches_repeat_probes(self):
        # Patch subprocess.check_output so we can count calls.
        with TemporaryDirectory() as td:
            p = Path(td) / "a.wav"
            _write_silence_wav(p, 1.0)
            with patch("pipeline.quality.probe.subprocess.check_output",
                       return_value="1.000000\n") as mock_run:
                probe_duration(p)
                probe_duration(p)
                probe_duration(p)
            self.assertEqual(
                mock_run.call_count, 1,
                "repeat probes of the same (path, mtime) should hit the lru_cache",
            )

    def test_mtime_change_invalidates_cache(self):
        # Different mtime → different cache key → fresh probe.
        with TemporaryDirectory() as td:
            p = Path(td) / "a.wav"
            _write_silence_wav(p, 1.0)
            with patch("pipeline.quality.probe.subprocess.check_output",
                       return_value="1.000000\n") as mock_run:
                probe_duration(p)
                # Touch the file (rewrite with same content but new mtime).
                _write_silence_wav(p, 1.0)
                probe_duration(p)
            self.assertEqual(
                mock_run.call_count, 2,
                "modifying the file should invalidate the cache",
            )

    def test_clear_probe_cache_works(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "a.wav"
            _write_silence_wav(p, 1.0)
            with patch("pipeline.quality.probe.subprocess.check_output",
                       return_value="1.000000\n") as mock_run:
                probe_duration(p)
                clear_probe_cache()
                probe_duration(p)
            self.assertEqual(mock_run.call_count, 2)


class ResetImageStateTests(unittest.TestCase):
    """``reset_image_state`` mirrors ``audio.reset_f5_state``: drops
    the diffusion singletons + clears Metal cache. Best-effort —
    must never raise even if MLX isn't loaded."""

    def setUp(self):
        # Defensive getattr: when the full test suite runs earlier tests
        # that touch pipeline.images.images (e.g. via shorts.py imports), the
        # module attributes may be present-but-mock or briefly missing
        # due to test-ordering quirks around MLX. We snapshot whatever's
        # there (or a sentinel) and restore it in tearDown.
        self._prev_pipe = getattr(images, "_PIPE", None)
        self._prev_ip = getattr(images, "_IP_ADAPTER_LOADED", False)
        self._prev_flux = getattr(images, "_FLUX_PIPE", None)
        self._prev_z = getattr(images, "_ZIMAGE_PIPE", None)

    def tearDown(self):
        images._PIPE = self._prev_pipe
        images._IP_ADAPTER_LOADED = self._prev_ip
        images._FLUX_PIPE = self._prev_flux
        images._ZIMAGE_PIPE = self._prev_z

    def test_drops_all_singletons(self):
        images._PIPE = "fake-sdxl-pipe"
        images._IP_ADAPTER_LOADED = True
        images._FLUX_PIPE = "fake-flux-pipe"
        images._ZIMAGE_PIPE = "fake-z-pipe"
        images.reset_image_state()
        self.assertIsNone(images._PIPE)
        self.assertFalse(images._IP_ADAPTER_LOADED)
        self.assertIsNone(images._FLUX_PIPE)
        self.assertIsNone(images._ZIMAGE_PIPE)

    def test_no_raise_when_mlx_missing(self):
        # Even if mlx.core isn't importable, the function should clear
        # the singletons and return cleanly.
        with patch.dict("sys.modules", {"mlx.core": None}):
            try:
                images.reset_image_state()
            except Exception as e:  # noqa: BLE001
                self.fail(f"reset_image_state should swallow errors: {e}")

    def test_no_raise_when_no_singletons_loaded(self):
        # Fresh state — every singleton already None — must be a clean no-op.
        images._PIPE = None
        images._FLUX_PIPE = None
        images._ZIMAGE_PIPE = None
        images.reset_image_state()  # no raise


# F5RefCacheLruTests removed 2026-05-09 — pipeline.tts.f5 was deleted in
# the laptop nuclear cleanup. The bounded LRU lived inside that module.


class Libx264ThreadsCapWiringTests(unittest.TestCase):
    """Regression guard: parallel-fanned libx264 encodes must pass
    `-threads 3` so the 4-worker pool doesn't oversubscribe (each
    libx264 default ≈ 1.5×cores → 4×18 = 72 threads on M2 Max).

    Sources of truth checked:
    * pipeline/render/long_form.py::_trim_clip_letterbox (both branches)
    * pipeline/render/footage_only.py::_build_silent_video (image / passthrough / letterbox jobs)
    """

    def _read(self, mod) -> str:
        with open(mod.__file__) as f:
            return f.read()

    def test_long_form_trim_passes_threads_3(self):
        # Post-2026-05-14: the trim function moved out of long_form.py
        # into pipeline/render/shared/trim_letterbox.py as part of the
        # 4-renderer-to-2-engine consolidation. Source-level grep test
        # follows the code to its new home.
        from pipeline.render.shared import trim_letterbox
        src = self._read(trim_letterbox)
        # trim_clip_letterbox has two encode paths — both should be capped.
        # Count occurrences of the libx264 + threads 3 pair in the trim function.
        # (Both branches sit inside the function; loose count is fine — the
        # critical thing is that there's no naked libx264 in the trim path.)
        self.assertIn('"-threads", "3"', src)
        self.assertGreaterEqual(
            src.count('"-threads", "3"'), 2,
            "expected at least 2 trim-encode sites with -threads 3 in shared/trim_letterbox.py",
        )

    def test_footage_only_silent_video_jobs_pass_threads_3(self):
        from pipeline.render._legacy import footage_only
        src = self._read(footage_only)
        # _build_silent_video has 3 job variants (image ken-burns,
        # passthrough scale, letterbox). All run under run_parallel.
        self.assertGreaterEqual(
            src.count('"-threads", "3"'), 3,
            "expected at least 3 trim-encode sites with -threads 3 in footage_only.py",
        )


if __name__ == "__main__":
    unittest.main()
