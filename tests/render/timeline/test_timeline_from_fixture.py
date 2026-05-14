"""Tests for pipeline/render/timeline/timeline_from_fixture.py.

Test-only TimelineBuilder that loads segments from a JSON file.
Pin its Protocol contract + JSON parsing + error handling.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline.render.contracts import (
    AudioResult,
    Segment,
    Timeline,
    TimelineBuilder,
)
from pipeline.render.spec import build_spec
from pipeline.render.timeline.timeline_from_fixture import TimelineFromFixture


class TimelineFromFixtureProtocolTest(unittest.TestCase):
    def test_satisfies_timeline_builder_protocol(self):
        self.assertIsInstance(TimelineFromFixture(), TimelineBuilder)


class TimelineFromFixtureBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".test-timeline-fixture-"))
        self.fixture = self.tmp / "beats.json"
        self.fixture.write_text(json.dumps([
            {"start_s": 0.0, "end_s": 1.5, "text": "first",
             "anchor_id": "beat_000", "kind": "beat"},
            {"start_s": 1.5, "end_s": 3.0, "text": "second",
             "anchor_id": "beat_001", "kind": "beat"},
        ]))
        self.audio = AudioResult(narration_path=Path("/tmp/n.wav"), duration_s=3.0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spec(self, **extras):
        return build_spec({
            "channel": "x",
            "channel_overrides": {**extras, "timeline_fixture_path": str(self.fixture)},
        }, channel_yaml_path=None, variant_yaml_path=None)

    def test_build_returns_timeline_of_segments(self):
        spec = self._spec()
        timeline = TimelineFromFixture().build(spec, script={}, audio=self.audio)
        self.assertEqual(len(timeline), 2)
        for seg in timeline:
            self.assertIsInstance(seg, Segment)

    def test_segment_fields_round_trip_from_json(self):
        spec = self._spec()
        timeline = TimelineFromFixture().build(spec, script={}, audio=self.audio)
        self.assertEqual(timeline[0].start_s, 0.0)
        self.assertEqual(timeline[0].end_s, 1.5)
        self.assertEqual(timeline[0].text, "first")
        self.assertEqual(timeline[0].anchor_id, "beat_000")
        self.assertEqual(timeline[0].kind, "beat")
        self.assertEqual(timeline[1].start_s, 1.5)

    def test_anchor_id_defaults_when_missing(self):
        # Fixture entry without anchor_id falls back to seg_<i>.
        self.fixture.write_text(json.dumps([
            {"start_s": 0.0, "end_s": 1.0, "text": "no-id"}
        ]))
        spec = self._spec()
        timeline = TimelineFromFixture().build(spec, script={}, audio=self.audio)
        self.assertEqual(timeline[0].anchor_id, "seg_000")

    def test_kind_defaults_to_beat(self):
        # Fixture entry without kind defaults to "beat".
        self.fixture.write_text(json.dumps([
            {"start_s": 0.0, "end_s": 1.0, "text": "x", "anchor_id": "x_000"}
        ]))
        spec = self._spec()
        timeline = TimelineFromFixture().build(spec, script={}, audio=self.audio)
        self.assertEqual(timeline[0].kind, "beat")


class TimelineFromFixtureErrorTest(unittest.TestCase):
    def test_missing_fixture_path_raises(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )
        audio = AudioResult(narration_path=Path("/tmp/n.wav"), duration_s=1.0)
        with self.assertRaises(ValueError) as ctx:
            TimelineFromFixture().build(spec, script={}, audio=audio)
        self.assertIn("timeline_fixture_path", str(ctx.exception))

    def test_nonexistent_fixture_raises(self):
        spec = build_spec({
            "channel": "x",
            "channel_overrides": {"timeline_fixture_path": "/no/such/file.json"},
        }, channel_yaml_path=None, variant_yaml_path=None)
        audio = AudioResult(narration_path=Path("/tmp/n.wav"), duration_s=1.0)
        with self.assertRaises(FileNotFoundError):
            TimelineFromFixture().build(spec, script={}, audio=audio)


if __name__ == "__main__":
    unittest.main()
