"""Pin the 2026-05-17 (round 4) prompts-uniqueness fail-loud gate.

Background
----------

Render ``/tmp/render-41d3233a/short.mp4`` shipped a 42-second video that
was the SAME kitchen + shrugging-girl + soup-bowl image looping for the
entire body. Root cause: the LLM author had emitted a ``prompts.json``
whose entries shared 1-2 unique ``key_visual`` strings across all beats.

The existing bijectivity guard in ``AiBeatSlideshow.produce`` only
catches the *anchor matcher* collapsing distinct prompts to fewer unique
*objects* — it cannot catch a prompts.json whose entries were never
distinct to begin with.

This file pins the new fail-loud gate that fires BEFORE the matcher
runs: when ``prompts_path`` is set AND there are ≥ 4 beats AND the
``key_visual`` uniqueness ratio drops below 50%, raise RenderFailedError
so the cloud worker marks ``stage=images_failed`` rather than shipping
a single-image-loop mp4.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests._helpers import PROJECT_ROOT  # noqa: F401 — sets sys.path

from pipeline.render.contracts import RenderFailedError, Segment
from pipeline.render.visualize.ai_beat_slideshow import AiBeatSlideshow


def _spec(prompts_path: str | None) -> MagicMock:
    """Minimal spec mock — populates only what the produce path reads
    before the uniqueness gate fires."""
    s = MagicMock()
    s.output_resolution = (1080, 1920)
    s.output_fps = 30
    s.extra = {
        "image_provider": "cloudrun_z_image_turbo",
        "image_style_prefix": "",
        "image_seed": 42,
        "image_steps": 4,
    }
    if prompts_path is not None:
        s.extra["prompts_path"] = prompts_path
    return s


def _timeline(n: int) -> list[Segment]:
    return [
        Segment(start_s=i, end_s=i + 1, text=f"line {i}", anchor_id=f"a{i}")
        for i in range(n)
    ]


def _write_prompts(path: Path, key_visuals: list[str]) -> None:
    payload = [
        {
            "key_visual": kv,
            "scene": f"supporting detail for {kv}",
            "narration_line": f"line {i}",
        }
        for i, kv in enumerate(key_visuals)
    ]
    path.write_text(json.dumps(payload))


class PromptsUniquenessGateTest(unittest.TestCase):

    def test_raises_when_ratio_below_threshold(self):
        # 6 beats, 2 unique key_visual strings → ratio = 33% (< 50%).
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "prompts.json"
            _write_prompts(p, [
                "shrugging girl at kitchen table",
                "shrugging girl at kitchen table",
                "shrugging girl at kitchen table",
                "shrugging girl at kitchen table",
                "soup bowl on the counter",
                "soup bowl on the counter",
            ])
            spec = _spec(prompts_path=str(p))
            with self.assertRaises(RenderFailedError) as ctx:
                AiBeatSlideshow().produce(spec, _timeline(6), Path(td))
            msg = str(ctx.exception)
            self.assertIn("unique key_visual", msg)
            self.assertIn("ratio=33%", msg)

    def test_raises_when_all_visuals_identical(self):
        # The literal render-41d3233a case: every beat carries the same
        # key_visual. Ratio = 1/8 = 12.5%.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "prompts.json"
            _write_prompts(p, [
                "girl shrugging in kitchen with soup bowl",
            ] * 8)
            spec = _spec(prompts_path=str(p))
            with self.assertRaises(RenderFailedError):
                AiBeatSlideshow().produce(spec, _timeline(8), Path(td))

    def test_passes_when_all_visuals_distinct(self):
        # Allow distinct key_visuals to proceed past the gate. We don't
        # want to assert the FULL produce() succeeds (would call out to
        # image-gen) — only that the uniqueness gate doesn't raise. Mock
        # the image-gen call so produce() returns instead of raising for
        # an unrelated reason.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "prompts.json"
            _write_prompts(p, [
                "a runner crossing the finish line",
                "a coach holding a stopwatch",
                "two athletes high-fiving",
                "a stadium scoreboard close-up",
                "a referee blowing a whistle",
            ])
            spec = _spec(prompts_path=str(p))
            # Patch image-gen to a no-op that writes a 1-byte file so
            # the produce() loop doesn't fail on missing PNGs. We only
            # care that the uniqueness gate let us through.
            def _fake_generate(*, out_path: Path, **kwargs):
                Path(out_path).write_bytes(b"\x00")
            with patch(
                "pipeline.images.images.generate",
                side_effect=_fake_generate,
            ), patch(
                "pipeline.render.visualize.ai_beat_slideshow."
                "AiBeatSlideshow._stitch_images",
            ) as stitch_mock, patch(
                "pipeline.render.visualize.ai_beat_slideshow.probe_duration",
                return_value=5.0,
            ):
                stitch_mock.return_value = None
                # Patch the slideshow output path to exist (probe_duration
                # is mocked so the file's contents don't matter).
                wd = Path(td)
                (wd / "slideshow.mp4").touch()
                # Should not raise the uniqueness error.
                track = AiBeatSlideshow().produce(spec, _timeline(5), wd)
                self.assertEqual(track.extras["source"], "ai_beat_slideshow")

    def test_gate_skipped_when_fewer_than_4_beats(self):
        # For 3 beats with 1 unique visual the ratio is 33% — same as
        # the failing case — but with only 3 entries the ratio is too
        # noisy to be reliable, so the gate should NOT fire.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "prompts.json"
            _write_prompts(p, ["same visual"] * 3)
            spec = _spec(prompts_path=str(p))
            # The uniqueness gate is skipped — proceeding to the
            # count-mismatch / image-gen path. Mock those out to keep
            # the test focused on the gate behaviour.
            def _fake_generate(*, out_path: Path, **kwargs):
                Path(out_path).write_bytes(b"\x00")
            with patch(
                "pipeline.images.images.generate",
                side_effect=_fake_generate,
            ), patch(
                "pipeline.render.visualize.ai_beat_slideshow."
                "AiBeatSlideshow._stitch_images",
            ), patch(
                "pipeline.render.visualize.ai_beat_slideshow.probe_duration",
                return_value=3.0,
            ):
                wd = Path(td)
                # Should not raise — the uniqueness gate was bypassed.
                try:
                    AiBeatSlideshow().produce(spec, _timeline(3), wd)
                except RenderFailedError as exc:
                    if "unique key_visual" in str(exc):
                        self.fail(
                            "uniqueness gate fired for n_beats=3 — "
                            "should have been skipped (gate requires "
                            "≥ 4 beats for a stable uniqueness ratio)"
                        )

    def test_gate_skipped_when_no_prompts_path(self):
        # Fixture renders with no prompts_path in spec.extra take the
        # bare-Segment.text legacy path. The uniqueness gate must NOT
        # fire in that case (it has no prompts.json to inspect).
        spec = _spec(prompts_path=None)
        # Patch image-gen + stitch so produce() runs to the end without
        # actually rendering. The point of this test is just that we
        # don't raise the uniqueness error.
        with tempfile.TemporaryDirectory() as td:
            def _fake_generate(*, out_path: Path, **kwargs):
                Path(out_path).write_bytes(b"\x00")
            with patch(
                "pipeline.images.images.generate",
                side_effect=_fake_generate,
            ), patch(
                "pipeline.render.visualize.ai_beat_slideshow."
                "AiBeatSlideshow._stitch_images",
            ), patch(
                "pipeline.render.visualize.ai_beat_slideshow.probe_duration",
                return_value=5.0,
            ):
                wd = Path(td)
                try:
                    AiBeatSlideshow().produce(spec, _timeline(5), wd)
                except RenderFailedError as exc:
                    if "unique key_visual" in str(exc):
                        self.fail(
                            "uniqueness gate fired when prompts_path "
                            "was not set — should be skipped on the "
                            "bare-Segment.text fallback lane"
                        )


if __name__ == "__main__":
    unittest.main()
