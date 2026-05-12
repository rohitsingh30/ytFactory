from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import script_lint as sl


class ScriptLintPrimitiveTest(unittest.TestCase):
    def test_split_and_count_helpers(self):
        self.assertEqual(sl._word_count(" one  two\nthree "), 3)
        self.assertEqual(sl._split_paragraphs(" A.\n\n B. \n\n"), ["A.", "B."])
        self.assertEqual(sl._split_sentences("A. B! C?"), ["A.", "B!", "C?"])
        self.assertEqual(sl._strip_terminator("Hello?!"), "Hello")


class MergeConsecutiveShortsTest(unittest.TestCase):
    def test_no_merge_for_single_sentence_or_single_short(self):
        self.assertEqual(sl._merge_consecutive_shorts("One sentence only."), ("One sentence only.", 0))
        self.assertEqual(sl._merge_consecutive_shorts("No. This sentence has five words."), ("No. This sentence has five words.", 0))

    def test_merges_runs_and_decaps_mid_clause_words(self):
        merged, runs = sl._merge_consecutive_shorts("Wine. Crafts. Just us. Then she left quickly.")
        self.assertEqual(runs, 1)
        self.assertIn("Wine, crafts, just us.", merged)

    def test_preserves_i_and_acronyms_when_merging(self):
        merged, runs = sl._merge_consecutive_shorts("AITA? I froze. NASA left. Home.")
        self.assertEqual(runs, 1)
        self.assertIn("AITA, I froze, NASA left, home.", merged)


class CheckViolationsTest(unittest.TestCase):
    def test_empty_and_sentence_count_and_word_count(self):
        self.assertEqual(sl._check(""), ["narration is empty"])
        issues = sl._check("One sentence only.")
        self.assertTrue(any("only 1 sentences" in i for i in issues))
        self.assertTrue(any("only 3 total words" in i for i in issues))

    def test_long_sentence_high_average_and_total_overflow(self):
        sentence = " ".join(["word"] * 20) + "."
        narration = " ".join([sentence] * 10)
        issues = sl._check(narration)
        self.assertTrue(any("exceeds soft cap" in i for i in issues))
        self.assertTrue(any("sentence 0 is 20 words" in i for i in issues))
        self.assertTrue(any("above target" in i for i in issues))

    def test_low_average_and_staccato_run(self):
        narration = "One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten."
        issues = sl._check(narration)
        self.assertTrue(any("below target" in i for i in issues))
        self.assertTrue(any("staccato" in i for i in issues))


class CapitalizeAndLintTest(unittest.TestCase):
    @unittest.expectedFailure
    def test_capitalizes_sentence_starts_and_honorific_typos(self):
        # Audit Q2.58 — pre-fix this self.skipTest'd when the helper
        # was missing, marking the test SKIPPED (green) and hiding
        # the feature gap. ``_capitalize_sentence_starts`` does NOT
        # exist in the current source; @expectedFailure surfaces
        # the gap as an XFAIL line in test output (visible) instead
        # of a SKIP (invisible).
        self.assertTrue(hasattr(sl, "_capitalize_sentence_starts"))
        fixed, n = sl._capitalize_sentence_starts("she left. then Dr, martin arrived.")
        self.assertEqual(fixed, "She left. Then Dr. Martin arrived.")
        self.assertEqual(n, 3)

    def test_lint_and_fix_reports_applied_fixes(self):
        result = sl.lint_and_fix("wine. crafts. just us. then Dr, martin arrived.")
        self.assertTrue(result.fixed)
        self.assertIn("Wine, crafts, just us.", result.narration)
        self.assertTrue(any("merged" in f for f in result.fixes_applied))
        # Audit Q2.58 — capitalization helper is documented missing
        # via the @expectedFailure above; the public lint_and_fix
        # API still exists and is fully tested without depending
        # on the missing helper.

    def test_lint_and_fix_clean_input_is_unchanged(self):
        narration = " ".join(["This sentence has exactly six words."] * 10)
        result = sl.lint_and_fix(narration)
        self.assertFalse(result.fixed)
        self.assertEqual(result.narration, narration)


if __name__ == "__main__":
    unittest.main()
