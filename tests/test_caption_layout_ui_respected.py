"""Regression — UI's ``CaptionsLayout`` selection must be respected
end-to-end, and font sizing must be sensible for both 9:16 shorts
and 16:9 long-form.

Three UI-selectable layouts (``pipeline/render/spec.py:215``):

- ``center_word_by_word`` → ``word_caption_pngs`` overlay plugin
- ``bottom_one_line``     → ``sentence_caption_ass`` (max_lines=1)
- ``bottom_two_line``     → ``sentence_caption_ass`` (max_lines=2)

History — until 2026-05-24, ``sentence_caption_ass.produce`` called
``build_captions_ass(cues=…, out_path=…, total_duration_s=…)`` with
kwargs that didn't match the actual ``build_captions_ass(out_ass, *,
narration_text=…, chunk_wavs=…, max_lines=2, …)`` signature →
TypeError at runtime → every render that selected ``bottom_one_line``
or ``bottom_two_line`` crashed at the caption-overlay stage. Bug went
unobserved because the most-used layout is ``center_word_by_word``.

Separately, the word-caption font sizes (320/260/200 px) were
calibrated for a 1920-tall 9:16 shorts canvas; on a 1080-tall 16:9
long-form canvas those pixel values were ~30/24/19% of frame
height — dominating the picture once the word-by-word fix shipped
and the long-form path actually started emitting one word at a time.

These tests pin: (a) the routing per layout, (b) sentence-cap
``max_lines`` honors the layout selection, (c) sentence-cap doesn't
TypeError on a real Timeline, (d) word-cap font size scales down for
long-form, (e) word-cap font size for shorts is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline.render.short_engine import _captions_plugin_for_layout
from pipeline.render.spec import CaptionsDensity, CaptionsLayout, CaptionStyleConfig


# ---- (a) routing per layout --------------------------------------------


def test_center_word_by_word_routes_to_word_caption_pngs() -> None:
    assert (
        _captions_plugin_for_layout(CaptionsLayout.CENTER_WORD_BY_WORD)
        == "word_caption_pngs"
    )


def test_bottom_one_line_routes_to_sentence_caption_ass() -> None:
    assert (
        _captions_plugin_for_layout(CaptionsLayout.BOTTOM_ONE_LINE)
        == "sentence_caption_ass"
    )


def test_bottom_two_line_routes_to_sentence_caption_ass() -> None:
    assert (
        _captions_plugin_for_layout(CaptionsLayout.BOTTOM_TWO_LINE)
        == "sentence_caption_ass"
    )


# ---- (b)(c) sentence_caption_ass honors max_lines + doesn't crash ------


@dataclass
class _Seg:
    start_s: float
    end_s: float
    text: str
    words: list | None = None


@dataclass
class _Audio:
    narration_path: Path
    duration_s: float


class _FakeSpec:
    """Minimal stand-in for RenderSpec — the producer only reads
    captions_layout + caption_style."""

    def __init__(self, layout: CaptionsLayout, *, play_res_y: int = 1080):
        self.captions_layout = layout
        self.caption_style = CaptionStyleConfig(play_res_y=play_res_y)


def _timeline_two_segments() -> list[_Seg]:
    # Two short segments + one segment that's deliberately long enough
    # to force the wrapper to truncate when max_lines=1.
    return [
        _Seg(0.0, 2.5, "I boarded the plane at gate twelve."),
        _Seg(
            2.5,
            8.0,
            "The captain announced a routine flight as the cabin lights dimmed "
            "and the engines spooled up. Everything felt normal — until it "
            "didn't.",
        ),
    ]


def test_sentence_caption_ass_does_not_crash_on_bottom_one_line(
    tmp_path: Path,
) -> None:
    """The whole point of the 2026-05-24 rewrite: this used to TypeError."""
    from pipeline.render.overlays.sentence_caption_ass import SentenceCaptionAss

    spec = _FakeSpec(CaptionsLayout.BOTTOM_ONE_LINE)
    timeline = _timeline_two_segments()
    audio = _Audio(narration_path=tmp_path / "narration.wav", duration_s=8.0)
    out = SentenceCaptionAss().produce(spec, timeline, audio)
    assert len(out) == 1, "must emit exactly one OverlayElement"
    assert out[0].extras["max_lines"] == 1, (
        "BOTTOM_ONE_LINE must set max_lines=1"
    )
    ass = (tmp_path / "captions.ass").read_text(encoding="utf-8")
    assert "[V4+ Styles]" in ass
    assert "Dialogue:" in ass
    # The long segment must be truncated to a single line (no \N break).
    # libass uses \N for line breaks; with max_lines=1 the wrapper
    # truncates and appends an ellipsis instead of wrapping. So we
    # expect two Dialogue lines total (short cue + long cue), the
    # long-cue line must contain "captain" (early in the source text)
    # and end with "…" before the field separators.
    dialogue_lines = [
        line for line in ass.splitlines() if line.startswith("Dialogue:")
    ]
    assert len(dialogue_lines) == 2, (
        f"expected 2 Dialogue lines (one per cue); got {len(dialogue_lines)}"
    )
    long_line = next(
        (line for line in dialogue_lines if "captain" in line), None,
    )
    assert long_line is not None, (
        f"long cue should still surface its opening word 'captain' "
        f"after truncation; got: {dialogue_lines!r}"
    )
    assert "\\N" not in long_line, (
        f"max_lines=1 must not wrap with \\N; got: {long_line!r}"
    )
    assert "…" in long_line, (
        f"max_lines=1 should mark truncation with an ellipsis; "
        f"got: {long_line!r}"
    )


def test_sentence_caption_ass_does_not_crash_on_bottom_two_line(
    tmp_path: Path,
) -> None:
    from pipeline.render.overlays.sentence_caption_ass import SentenceCaptionAss

    spec = _FakeSpec(CaptionsLayout.BOTTOM_TWO_LINE)
    timeline = _timeline_two_segments()
    audio = _Audio(narration_path=tmp_path / "narration.wav", duration_s=8.0)
    out = SentenceCaptionAss().produce(spec, timeline, audio)
    assert len(out) == 1
    assert out[0].extras["max_lines"] == 2, (
        "BOTTOM_TWO_LINE must set max_lines=2"
    )
    ass = (tmp_path / "captions.ass").read_text(encoding="utf-8")
    long_dialogue_line = [
        line for line in ass.splitlines()
        if "Dialogue" in line and "engines" in line
    ]
    assert long_dialogue_line, "long segment must produce a Dialogue line"
    # Two-line layout SHOULD wrap with \N if the cue is too long for
    # one line.
    assert "\\N" in long_dialogue_line[0], (
        f"BOTTOM_TWO_LINE must allow wrap with \\N for long cues; "
        f"got: {long_dialogue_line[0]!r}"
    )


def test_sentence_caption_ass_font_size_scales_for_shorts(
    tmp_path: Path,
) -> None:
    """Same caption_style default (38 px), rendered on a 1920-tall
    9:16 shorts canvas, should scale up to ~67 px. Pre-fix it
    rendered as 38 px on a 1920-tall canvas (2% of frame height)
    which is essentially unreadable."""
    from pipeline.render.overlays.sentence_caption_ass import SentenceCaptionAss

    spec = _FakeSpec(CaptionsLayout.BOTTOM_TWO_LINE, play_res_y=1920)
    timeline = _timeline_two_segments()
    audio = _Audio(narration_path=tmp_path / "narration.wav", duration_s=8.0)
    out = SentenceCaptionAss().produce(spec, timeline, audio)
    scaled = out[0].extras["font_size"]
    assert 60 <= scaled <= 80, (
        f"font_size on 1920-tall canvas should be ~67 px (scaled from "
        f"38 px baseline); got {scaled}"
    )


# ---- (d)(e) word-cap aspect-aware sizing -------------------------------


class _FakeWordSpec:
    def __init__(
        self,
        density: CaptionsDensity,
        *,
        play_res_y: int = 1920,
    ):
        self.captions_density = density
        self.caption_style = CaptionStyleConfig(play_res_y=play_res_y)


def test_word_caption_font_size_unchanged_for_shorts() -> None:
    """9:16 shorts (1920-tall) must keep the existing per-density
    pixel values (scale = 1.0)."""
    from pipeline.render.overlays.word_caption_pngs import WordCaptionPngs

    plugin = WordCaptionPngs()
    style = CaptionStyleConfig(play_res_y=1920)
    spec = _FakeWordSpec(CaptionsDensity.STANDARD, play_res_y=1920)
    assert plugin._font_size_for_density(spec) == style.font_size_standard, (
        f"shorts (1920-tall) must use the standard pixel value "
        f"({style.font_size_standard}) verbatim"
    )


def test_word_caption_font_size_scales_down_for_long_form() -> None:
    """16:9 long-form (1080-tall) must scale word-cap font size
    down to ~56% of the shorts value — otherwise a 260 px word
    on a 1080-tall canvas would be 24% of frame height. The
    aspect multiplier targets the same ~14% of frame height
    regardless of canvas size."""
    from pipeline.render.overlays.word_caption_pngs import WordCaptionPngs

    plugin = WordCaptionPngs()
    style = CaptionStyleConfig(play_res_y=1080)
    spec = _FakeWordSpec(CaptionsDensity.STANDARD, play_res_y=1080)
    scaled = plugin._font_size_for_density(spec)
    # 260 * (1080/1920) = 146.25 → round to 146
    expected = max(48, int(round(style.font_size_standard * (1080 / 1920))))
    assert scaled == expected, (
        f"long-form (1080-tall) word-cap should scale standard "
        f"{style.font_size_standard} → {expected}; got {scaled}"
    )
    # And the scaled size shouldn't dominate the frame: < 17%.
    assert scaled / 1080.0 < 0.17, (
        f"long-form word-cap font_size {scaled} px is {scaled/1080*100:.1f}% "
        f"of 1080-tall frame — should be < 17%"
    )


def test_word_caption_font_size_floor() -> None:
    """The floor of max(48, …) guards against silly-small captions
    on aspect ratios with very small play_res_y."""
    from pipeline.render.overlays.word_caption_pngs import WordCaptionPngs

    plugin = WordCaptionPngs()
    spec = _FakeWordSpec(CaptionsDensity.DENSE, play_res_y=600)
    assert plugin._font_size_for_density(spec) >= 48
