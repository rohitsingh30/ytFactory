"""Tests for pipeline.footage.footage_plan_lint — 100% line coverage."""
from __future__ import annotations

import os
import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.footage.footage_plan_lint import (
    AnchorViolation,
    FootagePlanLintError,
    _check_anchor,
    _content_word_count,
    _has_diacritic,
    lint_footage_plan,
    raise_if_any,
)


# ---------------------------------------------------------------------------
# _has_diacritic
# ---------------------------------------------------------------------------

class TestHasDiacritic(unittest.TestCase):
    def test_none_for_plain_ascii(self):
        self.assertIsNone(_has_diacritic("hello world"))

    def test_none_for_empty(self):
        self.assertIsNone(_has_diacritic(""))

    def test_named_diacritic(self):
        ch = _has_diacritic("café")
        self.assertEqual(ch, "é")

    def test_combining_mark(self):
        # U+0300 = combining grave accent — not in _DIACRITIC_NAMES but is combining
        result = _has_diacritic("a\u0300")
        self.assertEqual(result, "\u0300")


# ---------------------------------------------------------------------------
# _content_word_count
# ---------------------------------------------------------------------------

class TestContentWordCount(unittest.TestCase):
    def test_stopwords_not_counted(self):
        # all stopwords
        self.assertEqual(_content_word_count("the and or but"), 0)

    def test_content_words(self):
        # "goal", "scored", "match", "final" are content words
        self.assertEqual(_content_word_count("the goal was scored in the match final"), 4)

    def test_apostrophe_token(self):
        # "John's" = one content word (John's matches the regex)
        cnt = _content_word_count("John's brilliant goal")
        self.assertGreaterEqual(cnt, 2)


# ---------------------------------------------------------------------------
# _check_anchor — individual rules
# ---------------------------------------------------------------------------

class TestCheckAnchor(unittest.TestCase):
    def _vnames(self, text, **kwargs):
        vs = _check_anchor(text, section="match_footage", entry_id="e1")
        return [v.rule for v in vs]

    def test_empty_anchor(self):
        vs = _check_anchor("", section="match_footage", entry_id="x")
        self.assertEqual(len(vs), 1)
        self.assertEqual(vs[0].rule, "empty")

    def test_whitespace_only_anchor(self):
        vs = _check_anchor("   ", section="match_footage", entry_id="x")
        self.assertEqual(vs[0].rule, "empty")

    def test_respelling_violation(self):
        # "zheh-NEH-zee-oh" — lowercase-uppercase hyphenated
        rules = self._vnames("the striker scored a beautiful goal zheh-NEH-zee-oh in stoppage")
        self.assertIn("respelling", rules)

    def test_lowercase_train_3plus(self):
        # "em-bah-pay" — 3 lowercase hyphenated segments
        text = "the striker scored the decisive goal em-bah-pay in the final"
        rules = self._vnames(text)
        self.assertIn("lowercase-train", rules)

    def test_lowercase_train_2_segments_ok(self):
        # "high-value" — only 2 segments, skip
        text = "it was a high-value moment in the final match game"
        rules = self._vnames(text)
        self.assertNotIn("lowercase-train", rules)

    def test_number_word_violation(self):
        text = "they won twenty-five matches in a row during that season"
        rules = self._vnames(text)
        self.assertIn("number-word", rules)

    def test_apostrophe_violation(self):
        text = "it was Newell's club where the player first learned to play football"
        rules = self._vnames(text)
        self.assertIn("apostrophe", rules)

    def test_em_dash_violation(self):
        text = "they scored the goal — it was the greatest moment of all time"
        rules = self._vnames(text)
        self.assertIn("dash", rules)

    def test_en_dash_violation(self):
        text = "the score was two–nil at the end of regular time"
        rules = self._vnames(text)
        self.assertIn("dash", rules)

    def test_digits_violation(self):
        text = "they scored 3 goals against the champions in the match"
        rules = self._vnames(text)
        self.assertIn("digits", rules)

    def test_diacritic_violation(self):
        text = "the Barcelona striker scored a beautiful goal against Real Madrid"
        # Use 'ó' which is in _DIACRITIC_NAMES
        text = "the striker scored góal in the match against the team there"
        rules = self._vnames(text)
        self.assertIn("diacritic", rules)

    def test_too_short_violation(self):
        # fewer than 4 content words
        text = "great goal scored"
        rules = self._vnames(text)
        self.assertIn("too-short", rules)

    def test_valid_anchor_no_violations(self):
        text = "the striker burst through the defence and fired the shot into the net"
        vs = _check_anchor(text, section="match_footage", entry_id="e1")
        self.assertEqual(vs, [])

    def test_anchor_violation_str_method_not_called(self):
        # AnchorViolation.__str__ is pragma: no cover, but test the object is created
        v = AnchorViolation("match_footage", "e1", "text", "digits", "3")
        self.assertEqual(v.rule, "digits")
        self.assertEqual(v.detail, "3")

    def test_check_anchor_none_entry_id(self):
        # entry_id=None is valid
        vs = _check_anchor("they scored", section="b_roll", entry_id=None)
        self.assertEqual(vs[0].entry_id, None)


# ---------------------------------------------------------------------------
# lint_footage_plan
# ---------------------------------------------------------------------------

class TestLintFootagePlan(unittest.TestCase):
    def _plan(self, sections):
        return sections

    def test_empty_plan(self):
        violations = lint_footage_plan({})
        self.assertEqual(violations, [])

    def test_empty_sections(self):
        plan = {"match_footage": [], "b_roll": [], "talking_heads": []}
        self.assertEqual(lint_footage_plan(plan), [])

    def test_good_anchor(self):
        plan = {"match_footage": [
            {"id": "m1", "narration_anchor": "the striker burst through the defence and scored beautifully"}
        ]}
        self.assertEqual(lint_footage_plan(plan), [])

    def test_bad_anchor_in_match_footage(self):
        plan = {"match_footage": [
            {"id": "m1", "narration_anchor": "scored 2 goals in the final"}
        ]}
        vs = lint_footage_plan(plan)
        self.assertTrue(any(v.rule == "digits" for v in vs))

    def test_broll_skip_when_no_anchor(self):
        # b_roll without narration_anchor = background filler, skip
        plan = {"b_roll": [{"id": "b1"}]}
        self.assertEqual(lint_footage_plan(plan), [])

    def test_broll_checked_when_anchor_present(self):
        # b_roll WITH an anchor IS checked
        plan = {"b_roll": [
            {"id": "b1", "narration_anchor": "scored 3 goals match final"}
        ]}
        vs = lint_footage_plan(plan)
        self.assertTrue(any(v.rule == "digits" for v in vs))

    def test_motion_graphics_deferred_skip(self):
        plan = {"motion_graphics": [
            {"id": "mg1", "narration_anchor": "", "deferred": True}
        ]}
        # deferred=True → skip
        self.assertEqual(lint_footage_plan(plan), [])

    def test_motion_graphics_not_deferred_checked(self):
        plan = {"motion_graphics": [
            {"id": "mg1", "narration_anchor": "scored 1 goal in the match final"}
        ]}
        vs = lint_footage_plan(plan)
        self.assertTrue(any(v.rule == "digits" for v in vs))

    def test_all_sections_checked(self):
        anchor = "scored 1 goal"  # digits violation
        plan = {
            "match_footage": [{"id": "m1", "narration_anchor": anchor}],
            "talking_heads": [{"id": "t1", "narration_anchor": anchor}],
            "archival_footage": [{"id": "a1", "narration_anchor": anchor}],
        }
        vs = lint_footage_plan(plan)
        sections = {v.section for v in vs}
        self.assertIn("match_footage", sections)
        self.assertIn("talking_heads", sections)
        self.assertIn("archival_footage", sections)

    def test_missing_id_is_none(self):
        plan = {"match_footage": [
            {"narration_anchor": "scored 1 goal at the match there was big"}
        ]}
        vs = lint_footage_plan(plan)
        self.assertTrue(any(v.entry_id is None for v in vs))


# ---------------------------------------------------------------------------
# raise_if_any
# ---------------------------------------------------------------------------

class TestRaiseIfAny(unittest.TestCase):
    def test_no_violations_no_raise(self):
        plan = {"match_footage": [
            {"id": "m1", "narration_anchor": "the striker burst through defence and scored cleanly"}
        ]}
        raise_if_any(plan, slug="test-slug")  # should not raise

    def test_violations_raise(self):
        plan = {"match_footage": [
            {"id": "m1", "narration_anchor": "scored 2 goals in the match"}
        ]}
        with self.assertRaises(FootagePlanLintError) as ctx:
            raise_if_any(plan, slug="test-slug")
        self.assertIn("test-slug", str(ctx.exception))
        self.assertIn("digit", str(ctx.exception))

    def test_env_var_disable(self):
        plan = {"match_footage": [
            {"id": "m1", "narration_anchor": "scored 2 goals"}
        ]}
        old = os.environ.get("FOOTAGE_PLAN_LINT_DISABLE")
        try:
            os.environ["FOOTAGE_PLAN_LINT_DISABLE"] = "1"
            raise_if_any(plan)  # should NOT raise
        finally:
            if old is None:
                os.environ.pop("FOOTAGE_PLAN_LINT_DISABLE", None)
            else:
                os.environ["FOOTAGE_PLAN_LINT_DISABLE"] = old

    def test_violations_message_contains_rules(self):
        plan = {"match_footage": [
            {"id": "e1", "narration_anchor": "scored 2 goals in match final"}
        ]}
        with self.assertRaises(FootagePlanLintError) as ctx:
            raise_if_any(plan)
        msg = str(ctx.exception)
        self.assertIn("FOOTAGE_PLAN_LINT_DISABLE", msg)
        self.assertIn("whisper", msg)

    def test_raise_if_any_no_slug(self):
        plan = {"match_footage": [
            {"id": "e1", "narration_anchor": "bad 3 here"}
        ]}
        with self.assertRaises(FootagePlanLintError):
            raise_if_any(plan)  # slug=None default


if __name__ == "__main__":
    unittest.main()
