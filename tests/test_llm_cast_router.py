from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import cast_router as cr


class WordBoundedTest(unittest.TestCase):
    def test_word_boundaries_and_empty_names(self):
        self.assertTrue(cr._word_bounded("Ryan", "Ryan thanked me."))
        self.assertTrue(cr._word_bounded("Ryan", "I called Ryan's mom."))
        self.assertFalse(cr._word_bounded("Ann", "announcement"))
        self.assertFalse(cr._word_bounded("", "anything"))


class RouteCharacterDescriptionTest(unittest.TestCase):
    def setUp(self):
        self.supporting = [
            {"name": "Ryan", "aliases": ["Ry", "Dr. Martin", "I"], "description": "Ryan desc"},
            {"name": "Amelia", "aliases": ["my sister"], "description": "Amelia desc"},
            {"name": "Ghost", "aliases": ["ghost"], "description": ""},
        ]

    def test_no_supporting_returns_narrator(self):
        self.assertEqual(
            cr.route_character_description(
                beat_text="Ryan waved", scene="", key_visual="", narrator_desc="narrator", supporting_full=[]
            ),
            ("narrator", None),
        )

    def test_scene_and_key_visual_win_before_beat_text(self):
        desc, name = cr.route_character_description(
            beat_text="Amelia spoke", scene="Ryan holds a phone", key_visual="", narrator_desc="narrator", supporting_full=self.supporting
        )
        self.assertEqual((desc, name), ("Ryan desc", "Ryan"))
        desc, name = cr.route_character_description(
            beat_text="Ryan spoke", scene="", key_visual="my sister points", narrator_desc="narrator", supporting_full=self.supporting
        )
        self.assertEqual((desc, name), ("Amelia desc", "Amelia"))

    def test_alias_and_beat_text_fallback(self):
        desc, name = cr.route_character_description(
            beat_text="Then Dr. Martin scolded me", scene="", key_visual="", narrator_desc="narrator", supporting_full=self.supporting
        )
        self.assertEqual((desc, name), ("Ryan desc", "Ryan"))

    def test_witness_beat_keeps_narrator_even_when_name_matches(self):
        if not hasattr(cr, "_is_witness_beat"):
            self.skipTest("witness-beat guard not present in this source version")
        desc, name = cr.route_character_description(
            beat_text="Then Ryan walks in", scene="Ryan at the doorway", key_visual="", narrator_desc="narrator", supporting_full=self.supporting
        )
        self.assertEqual((desc, name), ("narrator", None))
        self.assertTrue(cr._is_witness_beat("she sat down beside us"))
        self.assertFalse(cr._is_witness_beat("she yelled at me"))

    def test_unmatched_or_empty_descriptions_fall_back(self):
        self.assertEqual(
            cr.route_character_description(
                beat_text="Ghost appears", scene="Ghost in fog", key_visual="", narrator_desc="narrator", supporting_full=self.supporting
            ),
            ("narrator", None),
        )


class ObjectOnlyBeatTest(unittest.TestCase):
    def test_object_only_requires_scene_without_body_hints(self):
        self.assertFalse(cr.is_object_only_beat("", "", "calendar"))
        self.assertTrue(cr.is_object_only_beat("a wall calendar", "pink dots", "my schedule"))
        self.assertFalse(cr.is_object_only_beat("the narrator holding a phone", "", "phone"))
        self.assertFalse(cr.is_object_only_beat("", "my hands on the phone", "phone"))
        self.assertFalse(cr.is_object_only_beat("our kitchen table", "", "table"))


if __name__ == "__main__":
    unittest.main()
