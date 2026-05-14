"""Tests for pipeline/render/overlays/noop.py.

The noop OverlayProducer always returns []. Used by the engine's
overlay-collection logic when a spec flag is False.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from pipeline.render.contracts import AudioResult, OverlayProducer
from pipeline.render.overlays.noop import NoopOverlay
from pipeline.render.spec import build_spec


class NoopOverlayProtocolTest(unittest.TestCase):
    def test_satisfies_overlay_producer_protocol(self):
        self.assertIsInstance(NoopOverlay(), OverlayProducer)


class NoopOverlayProduceTest(unittest.TestCase):
    def _spec(self):
        return build_spec(
            {"channel": "x", "channel_overrides": {}},
            channel_yaml_path=None, variant_yaml_path=None,
        )

    def _audio(self):
        return AudioResult(narration_path=Path("/tmp/n.wav"), duration_s=10.0)

    def test_returns_empty_list(self):
        result = NoopOverlay().produce(self._spec(), timeline=[], audio=self._audio())
        self.assertEqual(result, [])

    def test_empty_for_any_timeline(self):
        # Even with a non-empty timeline, noop produces nothing.
        from pipeline.render.contracts import Segment
        timeline = [
            Segment(start_s=0, end_s=1, text="x", anchor_id="x_000"),
            Segment(start_s=1, end_s=2, text="y", anchor_id="y_001"),
        ]
        result = NoopOverlay().produce(self._spec(), timeline=timeline, audio=self._audio())
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
