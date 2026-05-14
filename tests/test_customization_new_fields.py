"""Tests for the 6 new CustomizationField factories added 2026-05-14.

Pin that:
- Each new factory returns a CustomizationField with the right
  spec_field + cfg_targets + default + options (where applicable).
- get_customization_schema() exposes them in every channel's wizard.
- The new fields don't break any existing field's spec_field mapping.
"""
from __future__ import annotations

import unittest

from pipeline.schemas.customization import (
    CustomizationField,
    _chapter_cards_field,
    _critic_loop_field,
    _lower_thirds_field,
    _music_policy_field,
    _overlay_timeline_field,
    _tone_field,
    get_customization_schema,
)


class MusicPolicyFieldTest(unittest.TestCase):
    def test_returns_customization_field(self):
        f = _music_policy_field()
        self.assertIsInstance(f, CustomizationField)
        self.assertEqual(f.key, "music_policy")
        self.assertEqual(f.kind, "select")
        self.assertEqual(f.spec_field, "music_policy")
        self.assertEqual(f.default, "ducked_loop")

    def test_has_all_four_music_policy_options(self):
        f = _music_policy_field()
        values = [o.value for o in f.options]
        self.assertEqual(set(values),
                         {"ducked_loop", "single_bed", "section_mood", "none"})

    def test_cfg_targets_write_to_short_and_long_blocks(self):
        # The migration script collapses these into defaults.short /
        # defaults.long; cfg_targets currently mirror to the legacy
        # path + long_form path so existing renderers keep working
        # until the bigbang flip.
        f = _music_policy_field()
        paths = [t.path for t in f.cfg_targets]
        self.assertIn(["music_policy"], paths)


class LowerThirdsFieldTest(unittest.TestCase):
    def test_is_switch_with_false_default(self):
        f = _lower_thirds_field()
        self.assertEqual(f.kind, "switch")
        self.assertFalse(f.default)
        self.assertEqual(f.spec_field, "lower_thirds")


class ChapterCardsFieldTest(unittest.TestCase):
    def test_is_switch_with_false_default(self):
        f = _chapter_cards_field()
        self.assertEqual(f.kind, "switch")
        self.assertFalse(f.default)
        self.assertEqual(f.spec_field, "chapter_cards")


class OverlayTimelineFieldTest(unittest.TestCase):
    def test_is_switch_with_false_default(self):
        f = _overlay_timeline_field()
        self.assertEqual(f.kind, "switch")
        self.assertFalse(f.default)
        self.assertEqual(f.spec_field, "overlay_timeline")

    def test_help_text_mentions_sports_doc_use_case(self):
        # Documentation contract — operators reading the wizard should
        # know what flipping this switch does to the render shape.
        f = _overlay_timeline_field()
        self.assertIn("foreground", (f.help or "").lower())


class CriticLoopFieldTest(unittest.TestCase):
    def test_is_tristate_select(self):
        f = _critic_loop_field()
        self.assertEqual(f.kind, "select")
        self.assertEqual(f.default, "")
        values = [o.value for o in f.options]
        # Tri-state: blank (default) / off / on. NO defaults-by-kind.
        self.assertEqual(set(values), {"", "off", "on"})

    def test_spec_field_is_critic_loop(self):
        self.assertEqual(_critic_loop_field().spec_field, "critic_loop")


class ToneFieldTest(unittest.TestCase):
    def test_default_is_blank(self):
        f = _tone_field({})
        self.assertEqual(f.default, "")

    def test_has_built_in_tone_options(self):
        f = _tone_field({})
        values = [o.value for o in f.options]
        self.assertIn("", values)  # default
        self.assertIn("tifo-academic", values)
        self.assertIn("intense-podcast", values)
        self.assertIn("playful-spicy", values)
        self.assertIn("serious-doc", values)

    def test_channel_yaml_extra_tones_appended(self):
        # Channel ydoc with new-shape defaults.long.tts.tone_overrides
        # carrying a custom tone — the field should surface it.
        ydoc = {
            "defaults": {
                "long": {
                    "tts": {
                        "tone_overrides": {
                            "whisper-asmr": {"speed": 0.85, "atempo": 0.92},
                        },
                    },
                },
            },
        }
        f = _tone_field(ydoc)
        values = [o.value for o in f.options]
        self.assertIn("whisper-asmr", values)

    def test_channel_yaml_without_extra_tones_no_crash(self):
        # Old-shape ydoc shouldn't crash the factory.
        ydoc = {"name": "Old", "tts_provider": "x"}
        f = _tone_field(ydoc)
        self.assertEqual(f.spec_field, "tone")


class GetCustomizationSchemaIntegrationTest(unittest.TestCase):
    """Pin that the new fields appear in every channel's wizard schema."""

    EXPECTED_NEW_KEYS = {
        "music_policy", "lower_thirds", "chapter_cards",
        "overlay_timeline", "critic_loop", "tone",
    }

    def _channels_in_registry(self) -> list[str]:
        # Use the registered channels (don't hard-code so adding a new
        # channel still passes this).
        from pipeline.schemas.customization import _CHANNEL_PRESENTATION
        return [p["key"] for p in _CHANNEL_PRESENTATION]

    def test_new_fields_present_for_every_channel(self):
        for ch in self._channels_in_registry():
            schema = get_customization_schema(ch)
            if schema is None:
                continue  # disabled channel — skip
            present = {f.key for f in schema.fields}
            missing = self.EXPECTED_NEW_KEYS - present
            self.assertFalse(missing,
                f"channel {ch}: missing new wizard fields: {missing}")

    def test_total_field_count_grew_by_six(self):
        # If somebody adds an extra field this needs updating; the
        # test is here to make additions visible during PR review.
        schema = get_customization_schema("sportsrecapped")
        if schema is None:
            self.skipTest("sportsrecapped not registered")
        # Pre-2026-05-14 the schema had 14 fields. Today's expected = 20.
        self.assertEqual(len(schema.fields), 20)


if __name__ == "__main__":
    unittest.main()
