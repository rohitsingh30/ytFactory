"""Tests for pipeline.sources.drama — score_drama and pick_best."""

from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.sources.drama import pick_best, score_drama


class ScoreDramaTest(unittest.TestCase):
    def test_empty_text_returns_zero(self):
        score, reasons = score_drama("", "")
        self.assertEqual(score, 0.0)
        self.assertEqual(reasons, ["empty"])

    def test_whitespace_only_returns_zero(self):
        score, reasons = score_drama("   ", "   ")
        self.assertEqual(score, 0.0)
        self.assertEqual(reasons, ["empty"])

    def test_plain_text_with_no_signals(self):
        score, reasons = score_drama("Hello world", "Nothing interesting here.")
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)
        self.assertIn("no_drama_signals", reasons)

    def test_named_antagonist_lever(self):
        score, reasons = score_drama(
            "AITA for excluding my MIL Carol?",
            "My MIL Carol showed up uninvited to our wedding.",
        )
        labels = [r.split("=")[0] for r in reasons]
        self.assertIn("named_antagonist", labels)

    def test_conflict_verbs_lever(self):
        score, reasons = score_drama(
            "She screamed at me",
            "She screamed, yelled, and threatened to sue me. I shoved her back.",
        )
        labels = [r.split("=")[0] for r in reasons]
        self.assertIn("conflict", labels)

    def test_escalation_lever(self):
        score, reasons = score_drama(
            "Things escalated",
            "Then the next day it gets worse. After that, finally we settled.",
        )
        labels = [r.split("=")[0] for r in reasons]
        self.assertIn("escalation", labels)

    def test_pivot_lever(self):
        score, reasons = score_drama(
            "Plot twist",
            "Turns out she had been lying. The truth finally came out. I discovered it all.",
        )
        labels = [r.split("=")[0] for r in reasons]
        self.assertIn("pivot", labels)

    def test_dollar_stakes_lever(self):
        score, reasons = score_drama(
            "Money dispute",
            "She stole $5000 from me. That's 10k I can never get back.",
        )
        labels = [r.split("=")[0] for r in reasons]
        self.assertIn("dollar_stakes", labels)

    def test_named_event_lever(self):
        score, reasons = score_drama(
            "Wedding drama",
            "She crashed our wedding and ruined the ceremony. The graduation was next.",
        )
        labels = [r.split("=")[0] for r in reasons]
        self.assertIn("named_event", labels)

    def test_named_stakes_lever(self):
        score, reasons = score_drama(
            "Family breakdown",
            "She moved out and we went no contact. I lost my job because of this.",
        )
        labels = [r.split("=")[0] for r in reasons]
        self.assertIn("named_stakes", labels)

    def test_reddit_score_lever(self):
        score, reasons = score_drama(
            "Popular post",
            "This got lots of votes.",
            reddit_score=50000,
        )
        labels = [r.split("=")[0] for r in reasons]
        self.assertIn("upvotes", labels)

    def test_reddit_score_zero_not_included(self):
        score, reasons = score_drama(
            "Low score",
            "Nobody upvoted this.",
            reddit_score=0,
        )
        labels = [r.split("=")[0] for r in reasons]
        self.assertNotIn("upvotes", labels)

    def test_num_comments_lever(self):
        score, reasons = score_drama(
            "Controversial post",
            "People argued a lot.",
            num_comments=10000,
        )
        labels = [r.split("=")[0] for r in reasons]
        self.assertIn("comments", labels)

    def test_num_comments_zero_not_included(self):
        score, reasons = score_drama("", "x", num_comments=0)
        # num_comments=0 → not added, but we don't have non-zero
        labels = [r.split("=")[0] for r in reasons]
        self.assertNotIn("comments", labels)

    def test_score_clamped_to_zero_one(self):
        # Max-drama story — score should not exceed 1.0
        body = (
            "My MIL Carol screamed at me and threatened to sue me for $50,000. "
            "Then the next day she lied and turned out to have been cheating. "
            "We went no contact after the wedding disaster. Finally the truth came out."
        )
        score, _ = score_drama("Wedding disaster", body,
                                reddit_score=50000, num_comments=10000)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_cap_on_lever_hits(self):
        # Repeating the same conflict verb many times shouldn't push past the cap.
        # cap=6 for conflict, so >=6 hits give the same score as exactly 6 hits.
        body_over_cap = " ".join(["screamed"] * 50)
        score_over_cap, _ = score_drama("", body_over_cap)
        body_at_cap = " ".join(["screamed"] * 6)
        score_at_cap, _ = score_drama("", body_at_cap)
        # Both should give the same contribution since cap=6
        self.assertAlmostEqual(score_over_cap, score_at_cap, places=5)

    def test_named_antagonist_requires_name_after_relationship(self):
        # The named_antagonist pattern: "my <rel> <Name>" where Name is capitalized.
        # With re.IGNORECASE, any 2+ letter word qualifies as the "name" token —
        # which is the actual regex behavior. Verify a match IS found when a word follows.
        _, reasons = score_drama("My sister Carol is mean", "Nothing else here.")
        labels = [r.split("=")[0] for r in reasons]
        self.assertIn("named_antagonist", labels)

    def test_named_antagonist_hits_various_relationships(self):
        text = (
            "My husband Dave called me. Her boyfriend Jake yelled. "
            "His boss Karen threatened. Their landlord Mike sued."
        )
        _, reasons = score_drama("", text)
        labels = [r.split("=")[0] for r in reasons]
        self.assertIn("named_antagonist", labels)


class PickBestTest(unittest.TestCase):
    def test_empty_list_returns_none(self):
        best, ranked = pick_best([])
        self.assertIsNone(best)
        self.assertEqual(ranked, [])

    def test_single_story(self):
        stories = [{"title": "Hello", "body": "World", "metadata": {}}]
        best, ranked = pick_best(stories)
        self.assertEqual(best, stories[0])
        self.assertEqual(len(ranked), 1)

    def test_picks_highest_drama_score(self):
        boring = {"title": "Nothing", "body": "x", "metadata": {}}
        spicy = {
            "title": "Wedding drama",
            "body": (
                "My MIL Carol screamed at me and threatened to call police. "
                "Turns out she had been lying the whole time. "
                "We went no contact after the wedding ceremony."
            ),
            "metadata": {"score": 10000, "num_comments": 500},
        }
        best, ranked = pick_best([boring, spicy])
        self.assertEqual(best["title"], "Wedding drama")
        # Ranked descending
        self.assertGreaterEqual(ranked[0][1], ranked[1][1])

    def test_ranked_sorted_descending(self):
        stories = [
            {"title": "Boring", "body": "nothing", "metadata": {}},
            {"title": "Drama", "body": "my MIL Carol screamed and threatened to sue me",
             "metadata": {"score": 5000}},
            {"title": "Medium", "body": "then she yelled and slapped me", "metadata": {}},
        ]
        _, ranked = pick_best(stories)
        scores = [r[1] for r in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_metadata_none_handled(self):
        stories = [{"title": "T", "body": "B"}]  # no metadata key
        best, ranked = pick_best(stories)
        self.assertIsNotNone(best)

    def test_returns_reason_strings(self):
        stories = [{"title": "Hello", "body": "my MIL Carol screamed", "metadata": {}}]
        _, ranked = pick_best(stories)
        _, score, reasons = ranked[0]
        self.assertIsInstance(reasons, list)
        self.assertTrue(len(reasons) > 0)


if __name__ == "__main__":
    unittest.main()
