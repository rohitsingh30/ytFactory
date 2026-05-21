"""Pin the 2026-05-17 (round 4) caption-renderer fixes.

Three bugs render-41d3233a exposed in the ASS subtitle path of
``WordCaptionPngs._build_word_caption_ass``:

1. PlayRes was hardcoded to 1920×1080 (landscape) regardless of the
   output_resolution. Fix: PlayRes always equals spec.output_resolution
   so libass renders in real output pixels.

2. Alignment=5 (middle-center) drifted with the Ken-Burns zoom because
   captions tracked the visual subject. Fix: Alignment=2 (bottom-center)
   with MarginV = play_res_y * 0.15 anchors captions to the FRAME
   bottom — immune to anything happening in the visualize chain.

3. Per-event width overflow: a 7-char word at font_size=320 in a 1080-
   wide frame exceeded the canvas. Fix: per-event width-fit with an
   inline ``{\\fs<N>}`` override; fall through to a coarse heuristic
   when Pillow's font metrics aren't available.

Plus: punctuation-only tokens ("/", ",", "—") emitted by ASR as
separate words must NOT generate Dialogue events — otherwise the viewer
sees a giant floating glyph (render-41d3233a hook bug).
"""
from __future__ import annotations

import re
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from tests._helpers import PROJECT_ROOT  # noqa: F401 — sets sys.path

from pipeline.render.contracts import Segment
from pipeline.render.overlays.word_caption_pngs import WordCaptionPngs
from pipeline.render.spec import CaptionStyleConfig


@dataclass
class _FakeWord:
    text: str
    start: float
    end: float


@dataclass
class _FakeSegment:
    start_s: float
    end_s: float
    text: str
    anchor_id: str = "a0"
    words: list[_FakeWord] | None = None


@dataclass
class _FakeSpec:
    output_resolution: tuple[int, int]
    caption_style: CaptionStyleConfig = field(default_factory=CaptionStyleConfig)


class CaptionPlayResMatchesOutputResolutionTest(unittest.TestCase):
    """The ASS header's PlayResX/Y must equal spec.output_resolution
    — NOT the static landscape default from CaptionStyleConfig."""

    def test_portrait_shorts_get_portrait_play_res(self):
        spec = _FakeSpec(output_resolution=(1080, 1920))
        seg = _FakeSegment(
            start_s=0.0, end_s=1.0, text="Hi",
            words=[_FakeWord("Hi", 0.0, 1.0)],
        )
        with tempfile.TemporaryDirectory() as td:
            out = WordCaptionPngs()._build_word_caption_ass(
                spec, [seg], font_size=260, out_dir=Path(td),
            )
            body = out.read_text()
        self.assertIn("PlayResX: 1080", body)
        self.assertIn("PlayResY: 1920", body)
        # Anti-regression: the hardcoded landscape pair must not appear.
        self.assertNotIn("PlayResX: 1920\nPlayResY: 1080", body)

    def test_landscape_long_form_gets_landscape_play_res(self):
        # Engine could plausibly be used for a 16:9 short some day;
        # make sure the same fix works in that direction too.
        spec = _FakeSpec(output_resolution=(1920, 1080))
        seg = _FakeSegment(
            start_s=0.0, end_s=1.0, text="Hi",
            words=[_FakeWord("Hi", 0.0, 1.0)],
        )
        with tempfile.TemporaryDirectory() as td:
            out = WordCaptionPngs()._build_word_caption_ass(
                spec, [seg], font_size=260, out_dir=Path(td),
            )
            body = out.read_text()
        self.assertIn("PlayResX: 1920", body)
        self.assertIn("PlayResY: 1080", body)


class CaptionAnchoredToBottomTest(unittest.TestCase):
    """Alignment=2 + MarginV ≈ 15% of output_h pins captions to the
    bottom of the FRAME (immune to Ken-Burns zoom)."""

    def _style_line(self, ass_text: str) -> str:
        m = re.search(r"^Style: Default,.*$", ass_text, re.M)
        self.assertIsNotNone(m, f"no Style line in:\n{ass_text}")
        return m.group(0)

    def test_alignment_is_bottom_center(self):
        spec = _FakeSpec(output_resolution=(1080, 1920))
        seg = _FakeSegment(
            start_s=0, end_s=1, text="x",
            words=[_FakeWord("x", 0, 1)],
        )
        with tempfile.TemporaryDirectory() as td:
            out = WordCaptionPngs()._build_word_caption_ass(
                spec, [seg], font_size=200, out_dir=Path(td),
            )
            style = self._style_line(out.read_text())
        # Style fields: ...,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
        # Alignment is the 3rd-from-last numeric before MarginL/R/V (4th
        # from the end). Easier: split on comma and take field index 18
        # (0-indexed: Name=0, ..., Alignment=18). Use a more forgiving check.
        fields = style.split(",")
        # The format header is:
        # Name(0), Fontname(1), Fontsize(2), Primary(3), Secondary(4),
        # Outline(5), Back(6), Bold(7), Italic(8), Underline(9),
        # StrikeOut(10), ScaleX(11), ScaleY(12), Spacing(13), Angle(14),
        # BorderStyle(15), Outline(16), Shadow(17), Alignment(18),
        # MarginL(19), MarginR(20), MarginV(21), Encoding(22)
        self.assertEqual(fields[18].strip(), "2", f"Alignment field: {fields}")

    def test_margin_v_scales_with_output_height(self):
        # 1920-tall output → MarginV ≈ 288 (15%). Allow some slack
        # since the impl uses int() truncation.
        spec = _FakeSpec(output_resolution=(1080, 1920))
        seg = _FakeSegment(
            start_s=0, end_s=1, text="x",
            words=[_FakeWord("x", 0, 1)],
        )
        with tempfile.TemporaryDirectory() as td:
            out = WordCaptionPngs()._build_word_caption_ass(
                spec, [seg], font_size=200, out_dir=Path(td),
            )
            style = self._style_line(out.read_text())
        fields = [f.strip() for f in style.split(",")]
        margin_v = int(fields[21])
        self.assertGreaterEqual(margin_v, 240, f"MarginV={margin_v} too low")
        self.assertLessEqual(margin_v, 340, f"MarginV={margin_v} too high")


class PunctuationOnlyTokensSkippedTest(unittest.TestCase):
    """ASR sometimes emits "/", ",", "—" as standalone "words". These
    must NOT render as Dialogue events — they'd appear as a single
    giant glyph floating on screen (the render-41d3233a hook bug)."""

    def test_slash_glyph_skipped(self):
        spec = _FakeSpec(output_resolution=(1080, 1920))
        seg = _FakeSegment(
            start_s=0, end_s=3, text="/ hello world",
            words=[
                _FakeWord("/", 0.0, 0.5),
                _FakeWord("hello", 0.5, 1.5),
                _FakeWord("world", 1.5, 2.5),
            ],
        )
        with tempfile.TemporaryDirectory() as td:
            out = WordCaptionPngs()._build_word_caption_ass(
                spec, [seg], font_size=200, out_dir=Path(td),
            )
            body = out.read_text()
        # Two Dialogue events for "hello" + "world"; none for "/".
        events = [ln for ln in body.splitlines() if ln.startswith("Dialogue:")]
        self.assertEqual(len(events), 2)
        for line in events:
            self.assertNotIn(",/", line.split(",,")[-1])
        self.assertIn("hello", body)
        self.assertIn("world", body)

    def test_punctuation_pile_skipped(self):
        spec = _FakeSpec(output_resolution=(1080, 1920))
        seg = _FakeSegment(
            start_s=0, end_s=2, text="a -- b",
            words=[
                _FakeWord("a", 0.0, 0.3),
                _FakeWord("--", 0.3, 0.6),
                _FakeWord(",", 0.6, 0.9),
                _FakeWord("b", 0.9, 1.5),
            ],
        )
        with tempfile.TemporaryDirectory() as td:
            out = WordCaptionPngs()._build_word_caption_ass(
                spec, [seg], font_size=200, out_dir=Path(td),
            )
            body = out.read_text()
        events = [ln for ln in body.splitlines() if ln.startswith("Dialogue:")]
        self.assertEqual(len(events), 2, f"expected a + b only; got:\n{body}")


class PerEventFontShrinkTest(unittest.TestCase):
    """Long words at the configured font_size must auto-shrink via an
    inline ``{\\fs<N>}`` override so they fit ≤ 72% of frame width.

    On a 1080-wide canvas at font_size=320, a 9-char word would exceed
    the width budget — verify the shrink override fires.

    2026-05-18 (round 5): budget was tightened from 0.85 → 0.72 of
    frame width AND a 1.20× Pillow→libass safety multiplier was added
    after render-bd2d0848 still clipped "seconds." and "covered" with
    the 0.85 budget. Pillow's font measurement (Helvetica/DejaVu on the
    laptop) underestimates the width libass actually renders on Cloud
    Run (fontconfig picks a different bold-italic). Both regression
    tests below pin the tighter behavior.
    """

    def test_long_word_emits_fs_override(self):
        spec = _FakeSpec(output_resolution=(1080, 1920))
        # "PARTNERSHIP" is ~11 chars; at font_size=320 with Helvetica
        # bold the rendered width exceeds 1080 × 0.72 = 778px.
        seg = _FakeSegment(
            start_s=0, end_s=1, text="partnership",
            words=[_FakeWord("partnership", 0.0, 1.0)],
        )
        with tempfile.TemporaryDirectory() as td:
            out = WordCaptionPngs()._build_word_caption_ass(
                spec, [seg], font_size=320, out_dir=Path(td),
            )
            body = out.read_text()
        # Find the Dialogue line for "partnership".
        dialogue_lines = [
            ln for ln in body.splitlines() if ln.startswith("Dialogue:") and "partnership" in ln.lower()
        ]
        self.assertEqual(len(dialogue_lines), 1, body)
        # Must carry an inline font-size override.
        self.assertRegex(
            dialogue_lines[0],
            r"\{\\fs\d+\}partnership",
            f"expected {{\\fs<N>}} override on long word; got:\n{dialogue_lines[0]}",
        )

    def test_short_word_no_override(self):
        # "Hi" at font_size=200 fits comfortably — no override expected.
        spec = _FakeSpec(output_resolution=(1080, 1920))
        seg = _FakeSegment(
            start_s=0, end_s=1, text="Hi",
            words=[_FakeWord("Hi", 0.0, 1.0)],
        )
        with tempfile.TemporaryDirectory() as td:
            out = WordCaptionPngs()._build_word_caption_ass(
                spec, [seg], font_size=200, out_dir=Path(td),
            )
            body = out.read_text()
        dialogue_lines = [
            ln for ln in body.splitlines() if ln.startswith("Dialogue:") and "Hi" in ln
        ]
        self.assertEqual(len(dialogue_lines), 1)
        self.assertNotIn("\\fs", dialogue_lines[0])

    def test_eight_char_word_with_punctuation_shrinks(self):
        """Regression: render-bd2d0848 clipped 'seconds.' (8 chars
        including punct) on both edges. With the 0.72 budget + 1.20x
        safety multiplier the shrink override MUST fire — otherwise
        the next render clips again."""
        spec = _FakeSpec(output_resolution=(1080, 1920))
        seg = _FakeSegment(
            start_s=0, end_s=1, text="seconds.",
            words=[_FakeWord("seconds.", 0.0, 1.0)],
        )
        with tempfile.TemporaryDirectory() as td:
            out = WordCaptionPngs()._build_word_caption_ass(
                spec, [seg], font_size=320, out_dir=Path(td),
            )
            body = out.read_text()
        dialogue_lines = [
            ln for ln in body.splitlines()
            if ln.startswith("Dialogue:") and "seconds" in ln.lower()
        ]
        self.assertEqual(len(dialogue_lines), 1, body)
        self.assertRegex(
            dialogue_lines[0],
            r"\{\\fs\d+\}seconds\.",
            f"expected {{\\fs<N>}} override on 'seconds.'; got:\n{dialogue_lines[0]}",
        )

    def test_seven_char_word_shrinks(self):
        """Regression: 'covered' (7 chars) clipped the right edge in
        render-bd2d0848. Must shrink under the tightened budget."""
        spec = _FakeSpec(output_resolution=(1080, 1920))
        seg = _FakeSegment(
            start_s=0, end_s=1, text="covered",
            words=[_FakeWord("covered", 0.0, 1.0)],
        )
        with tempfile.TemporaryDirectory() as td:
            out = WordCaptionPngs()._build_word_caption_ass(
                spec, [seg], font_size=320, out_dir=Path(td),
            )
            body = out.read_text()
        dialogue_lines = [
            ln for ln in body.splitlines()
            if ln.startswith("Dialogue:") and "covered" in ln
        ]
        self.assertEqual(len(dialogue_lines), 1, body)
        self.assertRegex(
            dialogue_lines[0],
            r"\{\\fs\d+\}covered",
            f"expected {{\\fs<N>}} override on 'covered'; got:\n{dialogue_lines[0]}",
        )

    def test_fitted_size_within_budget(self):
        """The fitted font size for a long word at base=320 must
        produce a Pillow-measured width × 1.20 safety multiplier
        that fits within play_res_x × 0.72."""
        from pipeline.captions import _find_font  # noqa: PLC0415

        spec = _FakeSpec(output_resolution=(1080, 1920))
        seg = _FakeSegment(
            start_s=0, end_s=1, text="partnership",
            words=[_FakeWord("partnership", 0.0, 1.0)],
        )
        with tempfile.TemporaryDirectory() as td:
            out = WordCaptionPngs()._build_word_caption_ass(
                spec, [seg], font_size=320, out_dir=Path(td),
            )
            body = out.read_text()
        m = re.search(r"\{\\fs(\d+)\}partnership", body)
        self.assertIsNotNone(m, f"no \\fs override on 'partnership':\n{body}")
        fitted = int(m.group(1))
        # Re-measure to confirm the fitted size + safety multiplier
        # still fits in 0.72 of frame width.
        font = _find_font(fitted, text="partnership")
        bbox = font.getbbox("partnership")
        rendered_w = (bbox[2] - bbox[0]) * 1.20
        budget = 1080 * 0.72
        self.assertLessEqual(
            rendered_w, budget,
            f"fitted size {fitted} → width×safety {rendered_w:.0f} > budget {budget:.0f}",
        )


if __name__ == "__main__":
    unittest.main()
