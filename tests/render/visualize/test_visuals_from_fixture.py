"""Tests for pipeline/render/visualize/visuals_from_fixture.py.

Test-only VisualProducer that copies a fixture mp4 into work_dir.
Pin Protocol + measured (not declared) duration + error handling.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline.render.contracts import (
    AudioResult,
    Timeline,
    VisualProducer,
    VisualTrack,
)
from pipeline.render.spec import build_spec
from pipeline.render.visualize.visuals_from_fixture import VisualsFromFixture


def _make_mp4(path: Path, duration_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration_s:.3f}",
        "-i", "color=c=0x141414:s=1080x1920:r=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-pix_fmt", "yuv420p",
        str(path),
    ], check=True, capture_output=True)


class VisualsFromFixtureProtocolTest(unittest.TestCase):
    def test_satisfies_visual_producer_protocol(self):
        self.assertIsInstance(VisualsFromFixture(), VisualProducer)


class VisualsFromFixtureProduceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-visuals-fixture-"))
        self.fixture_mp4 = self.tmp / "src" / "visuals.mp4"
        _make_mp4(self.fixture_mp4, duration_s=3.0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spec(self):
        return build_spec({
            "channel": "x",
            "channel_overrides": {"visuals_fixture_path": str(self.fixture_mp4)},
        }, channel_yaml_path=None, variant_yaml_path=None)

    def test_produce_copies_mp4_into_work_dir(self):
        work_dir = self.tmp / "work"
        result = VisualsFromFixture().produce(self._spec(), timeline=[], work_dir=work_dir)
        self.assertIsInstance(result, VisualTrack)
        self.assertEqual(result.video_path, work_dir / "visual_track.mp4")
        self.assertTrue(result.video_path.exists())

    def test_produce_returns_measured_duration(self):
        result = VisualsFromFixture().produce(self._spec(), timeline=[],
                                              work_dir=self.tmp / "work")
        self.assertAlmostEqual(result.duration_s, 3.0, delta=0.1)

    def test_produce_records_source_in_extras(self):
        result = VisualsFromFixture().produce(self._spec(), timeline=[],
                                              work_dir=self.tmp / "work")
        self.assertEqual(result.extras["source"], "fixture")
        self.assertEqual(result.extras["fixture_path"], str(self.fixture_mp4))


class VisualsFromFixtureErrorTest(unittest.TestCase):
    def test_missing_fixture_path_raises(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )
        with self.assertRaises(ValueError) as ctx:
            VisualsFromFixture().produce(spec, timeline=[], work_dir=Path("/tmp/x"))
        self.assertIn("visuals_fixture_path", str(ctx.exception))

    def test_nonexistent_fixture_raises(self):
        spec = build_spec({
            "channel": "x",
            "channel_overrides": {"visuals_fixture_path": "/no/such/file.mp4"},
        }, channel_yaml_path=None, variant_yaml_path=None)
        with self.assertRaises(FileNotFoundError):
            VisualsFromFixture().produce(spec, timeline=[], work_dir=Path("/tmp/x"))


if __name__ == "__main__":
    unittest.main()
