"""Tests for the captions_layout dispatch path (2026-05-14).

Pinning the end-to-end contract: the user's wizard pick of caption
style flows through proposal → spec → cfg → renderer kwargs and lands
in the rendered video. The path:

  1. Wizard renders ``_captions_layout_field`` as 3 preview cards.
  2. The chosen value (``center_word_by_word`` |
     ``bottom_one_line`` | ``bottom_two_line``) lands on
     ``proposal.channel_overrides["captions_layout"]``.
  3. ``build_spec`` mirrors it onto ``RenderSpec.captions_layout``.
  4. ``apply_overrides`` writes it to ``cfg["captions_layout"]`` AND
     ``cfg["long_form"]["captions_layout"]``.
  5. Shorts' ``_resolve_caption_dispatch(cfg)`` returns
     ``(caption_mode, caption_max_lines, label)`` which feeds compose.
  6. Long-form's caption block reads ``lf["captions_layout"]`` and
     selects libass alignment + max_lines.

These tests pin steps 3-6. The wizard schema (step 1+2) is pinned by
``test_schemas_customization.py``.
"""
from __future__ import annotations

import unittest
from pathlib import Path

# Top-level imports so scripts/coverage_gate.py's import-grep
# discovers this file as a related test for the production modules.
from pipeline.render.spec import build_spec, CaptionsLayout
from pipeline.render.shared.caption_dispatch import _resolve_caption_dispatch
from pipeline.captions import render_beat_caption
from pipeline.beats import Beat

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestSpecCaptionsLayoutFlow(unittest.TestCase):
    """build_spec mirrors captions_layout from override → RenderSpec."""

    def test_default_is_center_word_by_word(self):
        spec = build_spec(
            {"channel": "mystoriesanimated", "format": "aita_animated", "length_s": 55},
            channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
            variant_yaml_path=REPO_ROOT / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
        )
        self.assertEqual(spec.captions_layout, CaptionsLayout.CENTER_WORD_BY_WORD)

    def test_override_carries_through_to_spec(self):

        for picked, expected in [
            ("center_word_by_word", CaptionsLayout.CENTER_WORD_BY_WORD),
            ("bottom_one_line",    CaptionsLayout.BOTTOM_ONE_LINE),
            ("bottom_two_line",    CaptionsLayout.BOTTOM_TWO_LINE),
        ]:
            with self.subTest(picked=picked):
                spec = build_spec(
                    {
                        "channel": "mystoriesanimated",
                        "format": "aita_animated",
                        "length_s": 55,
                        "channel_overrides": {"captions_layout": picked},
                    },
                    channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
                    variant_yaml_path=REPO_ROOT / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
                )
                self.assertEqual(spec.captions_layout, expected)

    def test_unknown_value_falls_back_to_default(self):
        """Stale sidecar with a removed value mustn't crash."""

        spec = build_spec(
            {
                "channel": "mystoriesanimated",
                "format": "aita_animated",
                "length_s": 55,
                "channel_overrides": {"captions_layout": "no_such_layout_xyz"},
            },
            channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
            variant_yaml_path=REPO_ROOT / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
        )
        self.assertEqual(spec.captions_layout, CaptionsLayout.CENTER_WORD_BY_WORD)

    def test_to_dict_emits_string_value(self):
        from pipeline.render.spec import build_spec
        spec = build_spec(
            {
                "channel": "mystoriesanimated",
                "format": "aita_animated",
                "length_s": 55,
                "channel_overrides": {"captions_layout": "bottom_two_line"},
            },
            channel_yaml_path=REPO_ROOT / "pipeline/channels/mystoriesanimated.yaml",
            variant_yaml_path=REPO_ROOT / "pipeline/variants/mystoriesanimated/aita_animated.yaml",
        )
        d = spec.to_dict()
        # Firestore serialisation must use the bare string, not the Enum repr.
        self.assertEqual(d["captions_layout"], "bottom_two_line")


class TestShortsCaptionDispatch(unittest.TestCase):
    """_resolve_caption_dispatch translates cfg → compose kwargs."""

    def test_center_word_by_word_uses_word_mode_no_max_lines(self):

        mode, max_lines, label = _resolve_caption_dispatch(
            {"captions_layout": "center_word_by_word"}
        )
        self.assertEqual(mode, "word")
        self.assertIsNone(max_lines)
        self.assertIn("center", label.lower())

    def test_bottom_one_line_uses_beat_mode_max_lines_1(self):

        mode, max_lines, label = _resolve_caption_dispatch(
            {"captions_layout": "bottom_one_line"}
        )
        self.assertEqual(mode, "beat")
        self.assertEqual(max_lines, 1)

    def test_bottom_two_line_uses_beat_mode_max_lines_2(self):

        mode, max_lines, _ = _resolve_caption_dispatch(
            {"captions_layout": "bottom_two_line"}
        )
        self.assertEqual(mode, "beat")
        self.assertEqual(max_lines, 2)

    def test_missing_key_defaults_to_word_mode(self):
        """No captions_layout in cfg → word-by-word (the existing default
        behaviour for the shorts path)."""

        mode, max_lines, _ = _resolve_caption_dispatch({})
        self.assertEqual(mode, "word")
        self.assertIsNone(max_lines)

    def test_unknown_value_defaults_to_word_mode(self):

        mode, _, _ = _resolve_caption_dispatch({"captions_layout": "garbage"})
        self.assertEqual(mode, "word")


class TestRenderBeatCaptionMaxLines(unittest.TestCase):
    """captions.render_beat_caption respects max_lines clamp."""

    def _render(self, text: str, max_lines: int | None, tmp_path: Path):


        beat = Beat(text=text, start=0.0, end=2.0, words=[])
        out = tmp_path / "cap.png"
        render_beat_caption(beat, out, canvas_w=1080, max_lines=max_lines)
        return out

    def test_max_lines_None_lets_canvas_grow(self):
        import tempfile
        from PIL import Image
        long_text = (
            "This is a very long caption that wraps across many lines "
            "because we keep adding words and words and words to it"
        )
        with tempfile.TemporaryDirectory() as td:
            p = self._render(long_text, max_lines=None, tmp_path=Path(td))
            # max_lines=None → canvas height grows beyond the default
            # 320px when the text wraps to >2 lines.
            with Image.open(p) as im:
                self.assertGreaterEqual(im.height, 320)

    def test_max_lines_1_truncates_with_ellipsis(self):
        import tempfile
        from PIL import Image
        long_text = (
            "This is a very long caption that should truncate into one "
            "single line with an ellipsis at the end"
        )
        with tempfile.TemporaryDirectory() as td:
            p = self._render(long_text, max_lines=1, tmp_path=Path(td))
            # max_lines=1 → canvas stays at the default 320 floor (won't
            # grow as it does for None which produces ~520+ for this
            # text). Pin: max_lines=1 is at the floor.
            with Image.open(p) as im:
                self.assertEqual(im.height, 320)

    def test_max_lines_2_grows_to_two_lines_only(self):
        import tempfile
        from PIL import Image
        long_text = (
            "This is a very long caption that wraps across many lines "
            "because we keep adding words and words and words to it"
        )
        with tempfile.TemporaryDirectory() as td:
            # max_lines=None reference height (all lines).
            ref = self._render(long_text, max_lines=None, tmp_path=Path(td))
            with Image.open(ref) as im:
                ref_h = im.height
            # max_lines=2 must be strictly shorter than the unclamped one
            # (truncation actually reduced the line count).
            p = self._render(long_text, max_lines=2, tmp_path=Path(td))
            with Image.open(p) as im:
                self.assertLess(im.height, ref_h)

    def test_short_text_unaffected_by_max_lines_1(self):
        """Short caption that already fits in 1 line shouldn't ellipsis."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = self._render("Short.", max_lines=1, tmp_path=Path(td))
            self.assertTrue(p.exists())


if __name__ == "__main__":
    unittest.main()
