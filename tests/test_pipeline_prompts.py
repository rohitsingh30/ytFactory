"""Tests for pipeline.prompts._validate_and_clean — schema enforcement
on the LLM's per-beat prompt array.

The actual `claude -p` call is integration; we exercise the validator.

`pipeline.prompts` imports `pipeline.images`, which transitively pulls
in torch + diffusers (~25s cold). We stub `pipeline.images` in
`sys.modules` BEFORE the import so this test stays fast.
"""

from __future__ import annotations

import sys
import types
import unittest

from tests._helpers import PROJECT_ROOT, FakeBeat  # noqa: F401


# Pre-stub pipeline.images so prompts.py's `from . import images, llm`
# resolves without triggering torch. _validate_and_clean uses both
# images.lint_prompt and images.strip_text_bait (the latter dedupes the
# text-bait stripper between the author path and the critic path —
# pipeline/llm/critic.py:regenerate_with_corrections also calls it).
#
# 2026-05-05: stub also exposes the singletons + reset_image_state so
# downstream tests that touch pipeline.images post-stub
# (test_refactor_quick_wins.ResetImageStateTests) don't crash. The
# stub for those is intentionally inert — no side effects, just the
# attribute surface.
if "pipeline.images.images" not in sys.modules:
    _stub = types.ModuleType("pipeline.images.images")
    _stub.lint_prompt = lambda scene: []
    _stub.strip_text_bait = lambda scene: (scene, [])
    _stub._PIPE = None
    _stub._IP_ADAPTER_LOADED = False
    _stub._FLUX_PIPE = None
    _stub._ZIMAGE_PIPE = None
    def _stub_reset_image_state():  # noqa: D401
        _stub._PIPE = None
        _stub._IP_ADAPTER_LOADED = False
        _stub._FLUX_PIPE = None
        _stub._ZIMAGE_PIPE = None
    _stub.reset_image_state = _stub_reset_image_state
    sys.modules["pipeline.images.images"] = _stub

from pipeline.llm.prompts import _validate_and_clean  # noqa: E402


def _beats(n: int) -> list:
    return [FakeBeat(text=f"beat {i}", start=i * 1.5, end=(i + 1) * 1.5)
            for i in range(n)]


class ValidateShapeTest(unittest.TestCase):
    def test_happy_path(self):
        beats = _beats(3)
        raw = [
            {"key_visual": "kv0", "scene": "s0"},
            {"key_visual": "kv1", "scene": "s1"},
            {"key_visual": "kv2", "scene": "s2"},
        ]
        out = _validate_and_clean(raw, beats, opening_directives=None)
        self.assertEqual(len(out), 3)
        for i, item in enumerate(out):
            self.assertEqual(item["scene"], f"s{i}")
            self.assertEqual(item["key_visual"], f"kv{i}")

    def test_strips_whitespace(self):
        beats = _beats(1)
        raw = [{"key_visual": "  punchline  ", "scene": "  scene  "}]
        out = _validate_and_clean(raw, beats, opening_directives=None)
        self.assertEqual(out[0]["key_visual"], "punchline")
        self.assertEqual(out[0]["scene"], "scene")


class ValidateFailureTest(unittest.TestCase):
    def test_non_array_raises(self):
        with self.assertRaises(ValueError):
            _validate_and_clean({"not": "a list"}, _beats(1), None)

    def test_count_mismatch_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(
                [{"key_visual": "x", "scene": "y"}],
                _beats(3),
                None,
            )
        self.assertIn("counts must match", str(ctx.exception))

    def test_non_dict_item_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(
                ["just a string"],
                _beats(1),
                None,
            )
        self.assertIn("not an object", str(ctx.exception))

    def test_empty_scene_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(
                [{"key_visual": "x", "scene": "   "}],
                _beats(1),
                None,
            )
        self.assertIn("empty 'scene'", str(ctx.exception))

    def test_missing_scene_raises(self):
        with self.assertRaises(ValueError):
            _validate_and_clean(
                [{"key_visual": "x"}],  # no scene
                _beats(1),
                None,
            )


class ValidateMissingKeyVisualTolerantTest(unittest.TestCase):
    """key_visual can be empty; only `scene` is required."""

    def test_missing_key_visual_falls_back_to_empty(self):
        beats = _beats(1)
        out = _validate_and_clean(
            [{"scene": "the only thing that matters"}],
            beats, None,
        )
        self.assertEqual(out[0]["key_visual"], "")
        self.assertEqual(out[0]["scene"], "the only thing that matters")


class OpeningDirectivesTest(unittest.TestCase):
    """Beat 0 directive check is warning-only — should not raise."""

    def test_directives_pass_with_matching_tokens(self):
        beats = _beats(2)
        # Beat 0's scene contains "kitchen" — matches example tokens
        raw = [
            {"key_visual": "salad bowl", "scene": "the character standing in the kitchen at a table"},
            {"key_visual": "x", "scene": "y"},
        ]
        directives = {
            "required_concrete_tokens": 2,
            "example_tokens": ["kitchen", "table"],
        }
        # Should not raise
        out = _validate_and_clean(raw, beats, directives)
        self.assertEqual(len(out), 2)

    def test_directives_warning_does_not_raise(self):
        beats = _beats(1)
        raw = [{"key_visual": "kv", "scene": "abstract feelings sadness"}]
        directives = {
            "required_concrete_tokens": 2,
            "example_tokens": ["restaurant table", "wedding cake"],
        }
        # Doesn't raise — only warns to stdout
        out = _validate_and_clean(raw, beats, directives)
        self.assertEqual(len(out), 1)


# ---- Additional coverage for prompt construction and lint helpers ----

import json
import shutil
from unittest.mock import patch

from pipeline.llm import prompts as pr

if not hasattr(pr.images, "lint_prompt"):
    pr.images.lint_prompt = lambda scene: []
if not hasattr(pr.images, "strip_text_bait"):
    pr.images.strip_text_bait = lambda scene: (scene, [])


SCRATCH_PROMPTS = PROJECT_ROOT / "tests" / ".scratch_prompts"


class PromptSubjectAndRankTest(unittest.TestCase):
    def test_extract_subject_trims_fillers_conjunctions_and_verbs(self):
        self.assertIsNone(pr._extract_subject("Eventually nothing matched."))
        self.assertEqual(pr._extract_subject("Then my husband and I walked away."), "my husband")
        self.assertEqual(pr._extract_subject("the wedding is ruined."), "the wedding")
        self.assertEqual(pr._extract_subject("my three kids ran outside."), "my three kids")

    def test_find_rank_for_beat_tracks_claimed_ranks(self):
        ranks = [{"rank": 1, "match_text": "Aguero goal"}, {"rank": 2, "match_text": "Ramos header"}]
        claimed = set()
        self.assertEqual(pr._find_rank_for_beat("The Aguero goal changed it", ranks, claimed)["rank"], 1)
        self.assertEqual(claimed, {1})
        self.assertIsNone(pr._find_rank_for_beat("Aguero goal again", ranks, claimed))
        self.assertIsNone(pr._find_rank_for_beat("no match", ranks, claimed))
        self.assertIsNone(pr._find_rank_for_beat("anything", None))


class BuildUserPromptTest(unittest.TestCase):
    def test_on_screen_prompt_includes_subject_opening_cast_emotion_and_rank_hint_once(self):
        beats = [FakeBeat("My mom found the phone.", 0, 1.5), FakeBeat("Aguero goal happened.", 1.5, 3.0), FakeBeat("Aguero goal again.", 3.0, 4.5)]
        prompt = pr._build_user_prompt(
            narration=" ".join(b.text for b in beats),
            beats=beats,
            source_story="source",
            cast_narrator_desc="brown hair narrator",
            cast_default_emotion="angry",
            style_prefix="pastel style" * 40,
            opening_directives={"required_concrete_tokens": 2, "example_tokens": ["phone", "kitchen"]},
            ranks=[{"rank": 1, "match_text": "Aguero goal", "image_prompt_hint": "blue kit shot"}],
        )
        self.assertIn("PRIMARY SUBJECT IN FRAME: My mom", prompt)
        self.assertIn("blue kit shot", prompt)
        self.assertEqual(prompt.count("CURATOR HINT"), 1)
        self.assertIn("Narrator's default emotional tone", prompt)
        self.assertIn("Beat 0 must contain at least 2 concrete tokens", prompt)
        self.assertIn("pastel style", prompt)

    def test_voice_only_prompt_lists_supporting_people_and_forbids_narrator(self):
        beats = [FakeBeat("Ramos scored.", 0, 1)]
        prompt = pr._build_user_prompt(
            narration="Ramos scored.", beats=beats, source_story="source",
            cast_narrator_desc=None, cast_default_emotion=None, style_prefix="style",
            opening_directives=None, narrator_visual_mode="voice_only",
            supporting=[{"name": "Ramos", "aliases": ["Sergio"], "description": "defender. extra words"}],
        )
        self.assertIn("NARRATOR MODE: voice-over only", prompt)
        self.assertIn("Ramos (aliases: Sergio)", prompt)
        self.assertIn("DO NOT place a narrator", prompt)


class HairAndCastContradictionTest(unittest.TestCase):
    def test_extract_hair_colour_empty_text(self):
        self.assertIsNone(pr._extract_hair_colour(""))

    def test_extract_hair_colour_normalises_variants(self):
        self.assertIsNone(pr._extract_hair_colour("no hair colour"))
        self.assertEqual(pr._extract_hair_colour("grey wavy hair"), "gray")
        self.assertEqual(pr._extract_hair_colour("a blond-haired person"), "blonde")
        self.assertEqual(pr._extract_hair_colour("dusty pink hair"), "dusty-pink")

    def test_cast_contradictions_skip_unpinned_or_other_character_and_flag_narrator(self):
        self.assertEqual(pr._check_cast_contradictions(scene="brown hair narrator", key_visual="", narration_line="", cast_narrator_desc="no colour"), [])
        self.assertEqual(pr._check_cast_contradictions(scene="sister with brown hair, laughing", key_visual="", narration_line="", cast_narrator_desc="pink hair"), [])
        self.assertEqual(pr._check_cast_contradictions(scene="Amelia with brown hair, laughing", key_visual="", narration_line="", cast_narrator_desc="pink hair"), [])
        warnings = pr._check_cast_contradictions(scene="the character with brown hair, arms crossed", key_visual="", narration_line="", cast_narrator_desc="pink hair")
        self.assertEqual(len(warnings), 1)
        self.assertIn("brown", warnings[0])


class ValidateAndAuthorAdditionalTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH_PROMPTS, ignore_errors=True)
        SCRATCH_PROMPTS.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(SCRATCH_PROMPTS, ignore_errors=True)

    def test_validate_strips_text_bait_lints_and_cast_contradictions(self):
        beats = [FakeBeat("beat zero", 0, 1)]
        def strip_text_bait(s):
            if "TEXT" in s:
                return (s.replace("TEXT", "").strip(), ["TEXT"])
            return (s, [])
        with patch.object(pr.images, "strip_text_bait", side_effect=strip_text_bait), \
             patch.object(pr.images, "lint_prompt", return_value=["warn"]):
            cleaned = pr._validate_and_clean(
                [{"key_visual": "TEXT brown hair", "scene": "the character with brown hair TEXT"}],
                beats,
                opening_directives={"required_concrete_tokens": 1, "example_tokens": ["phone"]},
                cast_narrator_desc="pink hair",
            )
        self.assertEqual(cleaned[0]["key_visual"], "brown hair")
        self.assertEqual(cleaned[0]["scene"], "the character with brown hair")
        self.assertEqual(cleaned[0]["narration_line"], "beat zero")

    def test_author_beat_prompts_calls_llm_validates_and_writes(self):
        out = SCRATCH_PROMPTS / "nested" / "prompts.json"
        beats = [FakeBeat("I found the phone.", 0, 1.5)]
        llm_out = [{"narration_line": "I found the phone.", "key_visual": "phone", "scene": "the character holding a phone"}]
        with patch.object(pr.llm, "model_for", return_value="opus"), patch.object(pr.llm, "call_claude_cli", return_value=llm_out) as call:
            cleaned = pr.author_beat_prompts(
                narration="I found the phone.", beats=beats, source_story="source",
                cast_narrator_desc="brown hair", cast_default_emotion="shocked",
                style_prefix="style", opening_directives=None, out_path=out,
            )
        self.assertEqual(cleaned, llm_out)
        self.assertTrue(out.exists())
        self.assertIn("Return ONLY a JSON array", call.call_args.args[0])
        self.assertEqual(call.call_args.kwargs["model"], "opus")
        self.assertEqual(json.loads(out.read_text())[0]["key_visual"], "phone")


if __name__ == "__main__":
    unittest.main()
