"""P5.2 (Q73) — caption density gate regression pin.

The gate raises ``RenderFailedError`` when ``spec.captions_enabled`` is
True but the caption layer (layer 40) covers < 80% of the narrated
audio window. Pre-fix, broken caption producers that returned an empty
list (out-of-band font, unmapped script, malformed timeline) silently
shipped captionless mp4s — the operator only noticed when watching the
final video.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from pipeline.render.contracts import OverlayElement, RenderFailedError
from pipeline.render.short_engine import _enforce_caption_density


def _spec(captions_enabled: bool = True):
    return SimpleNamespace(captions_enabled=captions_enabled)


def _caption_el(start_s: float, end_s: float) -> OverlayElement:
    return OverlayElement(
        start_s=start_s,
        end_s=end_s,
        layer=40,  # caption layer per the contract convention
        asset_path=Path("/tmp/cap.png"),
    )


def _other_el(start_s: float, end_s: float, layer: int) -> OverlayElement:
    return OverlayElement(
        start_s=start_s,
        end_s=end_s,
        layer=layer,
        asset_path=Path("/tmp/x.png"),
    )


class CaptionDensityGateTest(unittest.TestCase):
    def test_full_coverage_passes(self):
        overlays = [_caption_el(0.0, 60.0)]
        _enforce_caption_density(_spec(True), overlays, 60.0)  # no raise

    def test_85_percent_coverage_passes(self):
        overlays = [_caption_el(0.0, 51.0)]
        _enforce_caption_density(_spec(True), overlays, 60.0)

    def test_50_percent_coverage_fails(self):
        overlays = [_caption_el(0.0, 30.0)]
        with self.assertRaises(RenderFailedError) as ctx:
            _enforce_caption_density(_spec(True), overlays, 60.0)
        self.assertIn("caption coverage", str(ctx.exception).lower())

    def test_zero_caption_elements_fails(self):
        with self.assertRaises(RenderFailedError) as ctx:
            _enforce_caption_density(_spec(True), [], 60.0)
        self.assertIn("zero layer-40 caption elements", str(ctx.exception))

    def test_captions_disabled_skips_gate(self):
        # When the operator opts out, no caption elements is fine.
        _enforce_caption_density(_spec(False), [], 60.0)  # no raise

    def test_non_caption_layer_elements_ignored(self):
        # lower-thirds (layer 20) + chapter cards (layer 30) MUST NOT
        # count toward caption coverage.
        overlays = [
            _other_el(0.0, 60.0, layer=20),
            _other_el(0.0, 60.0, layer=30),
        ]
        with self.assertRaises(RenderFailedError):
            _enforce_caption_density(_spec(True), overlays, 60.0)

    def test_zero_duration_audio_is_a_noop(self):
        # Defensive: a zero-duration audio (test fixture, unlikely in
        # prod) should not divide-by-zero.
        _enforce_caption_density(_spec(True), [], 0.0)  # no raise


if __name__ == "__main__":
    unittest.main()
