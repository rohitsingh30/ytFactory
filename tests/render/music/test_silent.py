"""Tests for pipeline/render/music/silent.py.

The SilentMusic plugin emits a silent wav of the requested duration.
Pin Protocol + ffmpeg-emitted output + duration accuracy.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.render.contracts import MusicComposer
from pipeline.render.music.silent import SilentMusic
from pipeline.render.spec import build_spec


class SilentMusicProtocolTest(unittest.TestCase):
    def test_satisfies_music_composer_protocol(self):
        self.assertIsInstance(SilentMusic(), MusicComposer)


class SilentMusicComposeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-silent-music-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spec(self):
        return build_spec(
            {"channel": "x", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )

    def test_compose_returns_a_path(self):
        # Patch the /tmp output path so the test doesn't pollute it.
        with patch("pipeline.render.music.silent.Path") as MockPath:
            MockPath.return_value = self.tmp
            # The plugin uses Path("/tmp") / f"silent_music_..." — patching
            # Path entirely is too aggressive. Instead let it write to /tmp
            # and just verify the file exists.
            pass
        # Simpler: let the plugin write to /tmp and probe the file.
        result = SilentMusic().compose(self._spec(), narration_duration_s=2.0)
        self.assertIsInstance(result, Path)
        self.assertTrue(result.exists())
        # Cleanup.
        result.unlink()

    def test_compose_duration_within_tolerance(self):
        result = SilentMusic().compose(self._spec(), narration_duration_s=1.5)
        try:
            self.assertTrue(result.exists())
            # Probe duration via ffprobe.
            dur = float(subprocess.check_output([
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nokey=1:noprint_wrappers=1",
                str(result),
            ]).decode().strip())
            self.assertAlmostEqual(dur, 1.5, delta=0.1)
        finally:
            result.unlink(missing_ok=True)

    def test_compose_ignores_sections_arg(self):
        # SilentMusic doesn't use sections — pass garbage and verify no crash.
        result = SilentMusic().compose(self._spec(), narration_duration_s=0.5,
                                       sections=["garbage"])  # type: ignore[arg-type]
        try:
            self.assertTrue(result.exists())
        finally:
            result.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
