"""Tests for the spec.py extensions added 2026-05-14.

Pins the new typed knobs (music_policy, lower_thirds, chapter_cards,
overlay_timeline, critic_loop, tone) + nested config dataclasses
(TtsConfig, CaptionStyleConfig, MusicPolicyConfig, …) introduced as
part of the 4-renderer-to-2-engine consolidation.

Existing spec tests live in tests/test_render_spec.py — this file
ONLY covers the new surface so a future refactor can run just this
file to verify the additive-extension contract still holds.
"""
from __future__ import annotations

import unittest

from pipeline.render.spec import (
    CaptionStyleConfig,
    ChapterCardConfig,
    FillerConfig,
    LowerThirdConfig,
    MusicPolicy,
    MusicPolicyConfig,
    NetworkConfig,
    RenderSpec,
    TtsConfig,
    VideoGradeConfig,
    WatermarkConfig,
    WatermarkPosition,
    build_spec,
)


# ---------------------------------------------------------------------------
# Nested config dataclass invariants
# ---------------------------------------------------------------------------


class TtsConfigDefaultsTest(unittest.TestCase):
    def test_defaults_match_long_form_history(self):
        c = TtsConfig()
        self.assertAlmostEqual(c.speed_default, 0.98)
        self.assertAlmostEqual(c.post_atempo_default, 1.0)
        self.assertEqual(c.chunk_target_chars, 380)
        self.assertAlmostEqual(c.chunk_join_silence_s, 0.4)

    def test_default_tone_table_has_four_known_tones(self):
        c = TtsConfig()
        for tone in ("tifo-academic", "intense-podcast",
                     "playful-spicy", "serious-doc"):
            self.assertIn(tone, c.tone_overrides)
            self.assertIn("speed", c.tone_overrides[tone])
            self.assertIn("atempo", c.tone_overrides[tone])

    def test_tone_table_can_be_extended_per_render(self):
        c = TtsConfig()
        c.tone_overrides["whisper-asmr"] = {"speed": 0.85, "atempo": 0.92}
        self.assertEqual(c.tone_overrides["whisper-asmr"]["speed"], 0.85)


class CaptionStyleConfigDefaultsTest(unittest.TestCase):
    def test_defaults_match_long_form_yellow_italic(self):
        c = CaptionStyleConfig()
        # Sleepy Time History yellow #FFD93D + italic.
        self.assertEqual(c.text_rgba, (255, 217, 61, 255))
        self.assertTrue(c.italic)
        self.assertEqual(c.font_size, 38)
        self.assertEqual(c.play_res_x, 1920)
        self.assertEqual(c.play_res_y, 1080)


class MusicPolicyConfigDefaultsTest(unittest.TestCase):
    def test_defaults_match_short_ducked_loop_history(self):
        c = MusicPolicyConfig()
        self.assertEqual(c.default_bed, "ambient_low")
        self.assertAlmostEqual(c.music_bed_db, -28.0)
        self.assertAlmostEqual(c.filler_level_db, -22.0)
        self.assertAlmostEqual(c.mix_default, 0.35)


class WatermarkConfigDefaultsTest(unittest.TestCase):
    def test_defaults_match_long_form_history(self):
        c = WatermarkConfig()
        self.assertEqual(c.font_size, 28)
        self.assertEqual(c.text_rgba, (255, 255, 255, 140))
        self.assertEqual(c.margin, 32)
        self.assertEqual(c.position, WatermarkPosition.TOP_RIGHT)


class ChapterCardConfigDefaultsTest(unittest.TestCase):
    def test_defaults_match_sports_doc_teal_orange(self):
        c = ChapterCardConfig()
        # Deep-teal slab + orange chapter number from sports_doc.
        self.assertEqual(c.bg_rgba, (10, 22, 38, 240))
        self.assertEqual(c.number_color, (255, 168, 0, 255))
        self.assertEqual(c.title_font_size, 72)
        self.assertEqual(c.duration_s, 3.0)


class LowerThirdConfigDefaultsTest(unittest.TestCase):
    def test_defaults_match_sports_doc(self):
        c = LowerThirdConfig()
        self.assertEqual(c.font_size, 36)
        self.assertEqual(c.handle_font_size, 24)
        self.assertEqual(c.hold_min_s, 2.5)


class FillerAndNetworkDefaultsTest(unittest.TestCase):
    def test_filler_bg_color(self):
        self.assertEqual(FillerConfig().bg_color, "0x0a1626")

    def test_network_timeout(self):
        self.assertEqual(NetworkConfig().timeout_s, 30)


class VideoGradeConfigDefaultsTest(unittest.TestCase):
    def test_blur_sigma_brightness_defaults(self):
        c = VideoGradeConfig()
        self.assertAlmostEqual(c.blur_sigma, 22.0)
        self.assertAlmostEqual(c.brightness, 0.0)


# ---------------------------------------------------------------------------
# RenderSpec carries the nested configs with sensible defaults
# ---------------------------------------------------------------------------


class RenderSpecNestedDefaultsTest(unittest.TestCase):
    def _spec(self) -> RenderSpec:
        # Build via the builder so we exercise the same path the cloud
        # worker uses. Empty proposal → all defaults.
        return build_spec(
            {"channel": "mystoriesanimated", "channel_overrides": {}},
            channel_yaml_path=None,
            variant_yaml_path=None,
        )

    def test_spec_carries_all_nine_nested_configs(self):
        s = self._spec()
        self.assertIsInstance(s.tts, TtsConfig)
        self.assertIsInstance(s.caption_style, CaptionStyleConfig)
        self.assertIsInstance(s.music, MusicPolicyConfig)
        self.assertIsInstance(s.video_grade, VideoGradeConfig)
        self.assertIsInstance(s.watermark, WatermarkConfig)
        self.assertIsInstance(s.chapter_card, ChapterCardConfig)
        self.assertIsInstance(s.lower_third, LowerThirdConfig)
        self.assertIsInstance(s.filler, FillerConfig)
        self.assertIsInstance(s.network, NetworkConfig)

    def test_default_top_level_knobs(self):
        s = self._spec()
        self.assertEqual(s.music_policy, MusicPolicy.DUCKED_LOOP)
        self.assertFalse(s.lower_thirds)
        self.assertFalse(s.chapter_cards)
        self.assertFalse(s.overlay_timeline)
        self.assertIsNone(s.critic_loop)
        self.assertIsNone(s.tone)


# ---------------------------------------------------------------------------
# build_spec honours the new top-level overrides
# ---------------------------------------------------------------------------


class BuildSpecOverridesTest(unittest.TestCase):
    def test_music_policy_override_from_form(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {"music_policy": "section_mood"}},
            channel_yaml_path=None,
            variant_yaml_path=None,
        )
        self.assertEqual(spec.music_policy, MusicPolicy.SECTION_MOOD)

    def test_music_policy_override_invalid_falls_back(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {"music_policy": "garbage"}},
            channel_yaml_path=None,
            variant_yaml_path=None,
        )
        # Invalid value falls back to ducked_loop, doesn't raise.
        self.assertEqual(spec.music_policy, MusicPolicy.DUCKED_LOOP)

    def test_lower_thirds_override(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {"lower_thirds": "true"}},
            channel_yaml_path=None,
            variant_yaml_path=None,
        )
        self.assertTrue(spec.lower_thirds)

    def test_chapter_cards_override(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {"chapter_cards": True}},
            channel_yaml_path=None,
            variant_yaml_path=None,
        )
        self.assertTrue(spec.chapter_cards)

    def test_overlay_timeline_override(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {"overlay_timeline": "yes"}},
            channel_yaml_path=None,
            variant_yaml_path=None,
        )
        self.assertTrue(spec.overlay_timeline)

    def test_critic_loop_tristate_unset_remains_none(self):
        # Per user direction (2026-05-14): no defaults-by-kind. None
        # means "let channel default decide", not "default on".
        spec = build_spec(
            {"channel": "x", "channel_overrides": {}},
            channel_yaml_path=None,
            variant_yaml_path=None,
        )
        self.assertIsNone(spec.critic_loop)

    def test_critic_loop_explicit_true(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {"critic_loop": "true"}},
            channel_yaml_path=None,
            variant_yaml_path=None,
        )
        self.assertIs(spec.critic_loop, True)

    def test_critic_loop_explicit_false(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {"critic_loop": "false"}},
            channel_yaml_path=None,
            variant_yaml_path=None,
        )
        self.assertIs(spec.critic_loop, False)

    def test_tone_override(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {"tone": "intense-podcast"}},
            channel_yaml_path=None,
            variant_yaml_path=None,
        )
        self.assertEqual(spec.tone, "intense-podcast")

    def test_tone_empty_string_normalises_to_none(self):
        spec = build_spec(
            {"channel": "x", "channel_overrides": {"tone": "  "}},
            channel_yaml_path=None,
            variant_yaml_path=None,
        )
        self.assertIsNone(spec.tone)


# ---------------------------------------------------------------------------
# to_dict serialises the new fields for Firestore + dashboard
# ---------------------------------------------------------------------------


class ToDictSerialisationTest(unittest.TestCase):
    def _spec(self, **overrides) -> RenderSpec:
        return build_spec(
            {"channel": "x", "channel_overrides": overrides},
            channel_yaml_path=None,
            variant_yaml_path=None,
        )

    def test_music_policy_serialised_as_str(self):
        d = self._spec(music_policy="section_mood").to_dict()
        self.assertEqual(d["music_policy"], "section_mood")
        self.assertIsInstance(d["music_policy"], str)

    def test_nested_dataclasses_flattened_to_dicts(self):
        d = self._spec().to_dict()
        # Each nested config should be a dict the dashboard can render
        # without a custom decoder.
        for key in ("tts", "caption_style", "music", "video_grade",
                    "watermark", "chapter_card", "lower_third",
                    "filler", "network"):
            self.assertIn(key, d)
            self.assertIsInstance(d[key], dict, f"{key} should serialise to dict")

    def test_watermark_position_enum_flattened(self):
        d = self._spec().to_dict()
        self.assertIsInstance(d["watermark"]["position"], str)
        self.assertEqual(d["watermark"]["position"], "tr")

    def test_rgba_tuples_become_lists(self):
        d = self._spec().to_dict()
        # All four-tuple RGBA values must be serialised as lists so
        # Firestore doesn't reject the doc.
        self.assertIsInstance(d["watermark"]["text_rgba"], list)
        self.assertEqual(len(d["watermark"]["text_rgba"]), 4)
        self.assertIsInstance(d["caption_style"]["text_rgba"], list)
        self.assertIsInstance(d["chapter_card"]["bg_rgba"], list)
        self.assertIsInstance(d["chapter_card"]["number_color"], list)
        self.assertIsInstance(d["lower_third"]["bg_rgba"], list)
        self.assertIsInstance(d["lower_third"]["text_rgba"], list)
        self.assertIsInstance(d["lower_third"]["accent_rgba"], list)

    def test_critic_loop_none_round_trips(self):
        # None must NOT be coerced to False in the dict — the dashboard
        # distinguishes "user explicitly opted out" from "user left it
        # to channel default".
        d = self._spec().to_dict()
        self.assertIsNone(d["critic_loop"])

    def test_critic_loop_true_round_trips(self):
        d = self._spec(critic_loop="true").to_dict()
        self.assertIs(d["critic_loop"], True)

    def test_overlay_timeline_round_trips(self):
        d = self._spec(overlay_timeline="true").to_dict()
        self.assertIs(d["overlay_timeline"], True)


if __name__ == "__main__":
    unittest.main()
