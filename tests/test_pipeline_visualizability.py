"""Tests for pipeline.visualizability — pre-TTS heuristic gate."""

from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm.visualizability import score_visualizability


class EmptyTextTest(unittest.TestCase):
    def test_empty(self):
        s, reasons = score_visualizability("")
        self.assertEqual(s, 0.0)
        self.assertIn("empty", reasons)

    def test_whitespace_only(self):
        s, reasons = score_visualizability("   \n  ")
        self.assertEqual(s, 0.0)


class GoodStoryTest(unittest.TestCase):
    """A typical AITA-shaped story should score above 0.6."""

    def test_typical_aita_scores_high(self):
        story = (
            "My DIL wants a water birth in my living room. She and my "
            "son are staying with me — their house got flooded by a "
            "burst fire hydrant. Now she's announcing she's having her "
            "baby here, in a blow-up pool, with a midwife. My place is "
            "two bedrooms. I told her no. She yelled and called me selfish. "
            "My son grabbed his coat and walked out. AITA for refusing to host "
            "this in my house?"
        )
        score, reasons = score_visualizability(story)
        self.assertGreater(score, 0.6, f"score={score}, reasons={reasons}")


class TooShortTest(unittest.TestCase):
    def test_under_200_chars_low_score(self):
        story = "I said no. AITA?"
        score, reasons = score_visualizability(story)
        # Lowest length bucket → length sub-score is 0
        self.assertLess(score, 0.5)
        self.assertTrue(any("short" in r for r in reasons))


class FewConcreteNounsTest(unittest.TestCase):
    def test_abstract_story_flagged(self):
        # 250+ chars, but mostly feelings, no concrete nouns/verbs/numbers
        story = (
            "I felt fundamentally unseen. The persistence of incremental "
            "alienation persisted unmistakably. Everything felt opaque and "
            "abstract. Boundaries dissolved. Hopes evaporated. Identity "
            "fragmented. The arc of recognition curved inward. AITA?"
        )
        score, reasons = score_visualizability(story)
        self.assertLess(score, 0.5, f"score={score}, reasons={reasons}")
        self.assertTrue(any("concrete nouns" in r for r in reasons))


class DialogueHeavyTest(unittest.TestCase):
    def test_mostly_quotes_flagged(self):
        # >50% of chars inside quote marks
        story = (
            'My MIL said "I cannot believe you would do that to my son." '
            'I said "I think you are not being fair." She said "You always '
            'twist my words around." I said "We are not communicating." '
            'She said "You started it three times this week."'
        )
        score, reasons = score_visualizability(story)
        # At least the dialogue heuristic should fire
        self.assertTrue(any("dialogue" in r for r in reasons))


class NoNumbersTest(unittest.TestCase):
    def test_missing_numbers_flagged(self):
        # Concrete nouns + actions but no numbers
        story = (
            "My husband threw away the leftovers in the kitchen. I told him "
            "I needed them for lunch. He said he didn't know. My daughter "
            "started crying. The dog barked at the back door. I grabbed my "
            "coat and walked out to the car. AITA?"
        )
        _, reasons = score_visualizability(story)
        self.assertTrue(any("concrete numbers" in r for r in reasons))


class LengthyButOkTest(unittest.TestCase):
    def test_long_story_partial_credit(self):
        # 5000+ chars — length bucket fires "long ... risk of losing specifics"
        story = (
            "The kitchen was small. " * 250 +
            "I said no three times. AITA for thinking my husband was wrong?"
        )
        score, reasons = score_visualizability(story)
        # We don't bottom out — long is "0.3" on the length sub-score
        self.assertGreater(score, 0.0)
        self.assertTrue(any("losing specifics" in r or "long" in r for r in reasons),
                        f"reasons did not flag length: {reasons}")


class ScoreInRangeTest(unittest.TestCase):
    def test_score_always_0_to_1(self):
        for txt in [
            "x" * 250,
            "I refused to bake the cake. AITA? " * 30,
            "" ,
        ]:
            s, _ = score_visualizability(txt)
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, 1.0)


if __name__ == "__main__":
    unittest.main()
