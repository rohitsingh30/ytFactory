"""P5.1 — CloserPanel OverlayProducer regression pin.

Q72 (cofounder onboarding): the closer-panel was dead-coded inside
``pipeline/compose.py`` after the 2026-05-02 caption rework. The
2026-05-23 port re-exposes it as an opt-in OverlayProducer plugin so
channels that still want an explicit CTA card (sports docs, kids'
rhymes, history long-forms) can flip ``spec.closer_panel=True`` and
get one.

The tests below pin:
- Plugin registers under slot ``overlays`` with name ``closer_panel``.
- Empty / missing ``closer_format`` → no elements (safe degradation).
- A real ``closer_format`` → one ``OverlayElement`` at the tail of the
  timeline, layer 40 (above chapter_card 30, below caption 50).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pipeline.render.contracts import (
    AudioResult,
    Segment,
    get_plugin,
)
# Import for side-effect: registers the plugin.
from pipeline.render.overlays import closer_panel as _cp  # noqa: F401


def _make_spec(*, closer_format: str = "", closer_hold_s: float = 1.0):
    return SimpleNamespace(
        output_resolution=(1080, 1920),
        closer_panel=True,
        extra={"closer_format": closer_format, "closer_hold_s": closer_hold_s},
    )


def _make_timeline_with_tail(tail_end_s: float = 30.0):
    return [
        Segment(
            start_s=0.0,
            end_s=tail_end_s - 2.0,
            text="beat one",
            anchor_id="beat_000",
            kind="beat",
        ),
        Segment(
            start_s=tail_end_s - 2.0,
            end_s=tail_end_s,
            text="beat two",
            anchor_id="beat_001",
            kind="beat",
        ),
    ]


def _make_audio(tmpdir: Path) -> AudioResult:
    nar = tmpdir / "narration.wav"
    nar.write_bytes(b"RIFF" + b"\x00" * 40)
    return AudioResult(narration_path=nar, duration_s=30.0)


class CloserPanelPluginTest(unittest.TestCase):
    def test_plugin_registers_under_overlays_closer_panel(self):
        plugin = get_plugin("overlays", "closer_panel")
        self.assertIsNotNone(plugin)
        self.assertTrue(hasattr(plugin, "produce"))

    def test_collect_overlays_invokes_closer_panel_when_spec_flag_true(self):
        """R4 regression pin: a registered plugin is worthless if no engine
        dispatches it. Verifies ``_collect_overlays`` reads ``spec.closer_panel``
        and yields the panel's elements."""
        from pipeline.render import short_engine as _se
        spec = _make_spec(closer_format="LIKE if YTA, COMMENT if NTA")
        # Fill out the spec attrs the rest of _collect_overlays touches.
        spec.captions_enabled = False
        spec.lower_thirds = False
        spec.chapter_cards = False
        spec.overlay_timeline = False
        with tempfile.TemporaryDirectory() as td, \
             mock.patch("pipeline.captions.render_closer_panel") as mock_render:
            mock_render.side_effect = lambda out_path, **kw: out_path.write_bytes(b"PNG\x00") or out_path
            overlays = _se._collect_overlays(
                spec, _make_timeline_with_tail(30.0), _make_audio(Path(td))
            )
        layers = [el.layer for el in overlays]
        self.assertIn(35, layers,
                      f"_collect_overlays must invoke closer_panel when "
                      f"spec.closer_panel=True; got layers {layers}")

    def test_missing_closer_format_yields_no_elements(self):
        plugin = get_plugin("overlays", "closer_panel")
        spec = _make_spec(closer_format="")
        with tempfile.TemporaryDirectory() as td:
            elements = plugin.produce(spec, _make_timeline_with_tail(), _make_audio(Path(td)))
        self.assertEqual(elements, [])

    def test_present_closer_format_yields_one_element_at_tail(self):
        plugin = get_plugin("overlays", "closer_panel")
        spec = _make_spec(closer_format="LIKE if YTA, COMMENT if NTA", closer_hold_s=1.0)
        with tempfile.TemporaryDirectory() as td, \
             mock.patch("pipeline.captions.render_closer_panel") as mock_render:
            mock_render.side_effect = lambda out_path, **kw: out_path.write_bytes(b"PNG\x00") or out_path
            elements = plugin.produce(spec, _make_timeline_with_tail(30.0), _make_audio(Path(td)))
        self.assertEqual(len(elements), 1)
        el = elements[0]
        self.assertAlmostEqual(el.end_s, 30.0, places=2)
        self.assertAlmostEqual(el.start_s, 29.0, places=2)
        # Layer 35 sits above chapter_card (30) and below captions (40).
        self.assertEqual(el.layer, 35)


if __name__ == "__main__":
    unittest.main()
