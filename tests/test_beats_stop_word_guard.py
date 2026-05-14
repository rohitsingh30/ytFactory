"""Tests for the beat segmenter — specifically the stop-word boundary
guard added 2026-05-14 to fix the 'I refused to cut my' / 'sister's
wedding cake' mid-clause split bug.

The 27-render audit (docs/pipeline_bug_catalogue_v2_2026-05-14.html)
found tier-3 middle-word splits in pipeline/beats.py:_split_long_group
were ending the left side of the split on stop-words like 'my', 'to',
'of' — viewers read this as the video freezing mid-sentence and flick.

The fix: forbid splits that end the left side on a stop-word; walk
±3 words to find an acceptable boundary; fall back to the original
index if no acceptable position exists (the max_s contract still
takes precedence).
"""

from __future__ import annotations

import unittest

from pipeline.beats import (
    Word,
    _FORBIDDEN_END_TOKENS,
    _is_acceptable_split_idx,
    _shift_to_acceptable,
    _split_long_group,
)


def _w(text: str, start: float, end: float) -> Word:
    """Convenience word builder for test fixtures."""
    return Word(text=text, start=start, end=end)


def _wphrase(phrase: str, start: float = 0.0, per_word_s: float = 0.3) -> list[Word]:
    """Build a word list from a space-separated phrase. Each word
    occupies ``per_word_s`` seconds."""
    words = phrase.split()
    out: list[Word] = []
    t = start
    for w in words:
        out.append(_w(w, t, t + per_word_s))
        t += per_word_s
    return out


class ForbiddenEndTokensTest(unittest.TestCase):

    def test_articles_are_forbidden(self):
        for tok in ["a", "an", "the"]:
            self.assertIn(tok, _FORBIDDEN_END_TOKENS,
                          f"article {tok!r} must be forbidden as a split-end")

    def test_possessives_are_forbidden(self):
        for tok in ["my", "your", "his", "her", "our", "their"]:
            self.assertIn(tok, _FORBIDDEN_END_TOKENS)

    def test_common_prepositions_are_forbidden(self):
        for tok in ["of", "for", "to", "with", "in", "on", "at", "by", "from"]:
            self.assertIn(tok, _FORBIDDEN_END_TOKENS)

    def test_lowercase_normalised(self):
        # All entries lowercase (matched against .lower() at lookup).
        for tok in _FORBIDDEN_END_TOKENS:
            self.assertEqual(tok, tok.lower(),
                             f"{tok!r} must be lowercase")


class IsAcceptableSplitIdxTest(unittest.TestCase):

    def test_split_after_my_rejected(self):
        # "I refused to cut my | sister's wedding cake" — best=5
        # means left=[I, refused, to, cut, my] which ends on "my".
        words = _wphrase("I refused to cut my sister's wedding cake")
        self.assertFalse(_is_acceptable_split_idx(words, best=5))

    def test_split_after_real_word_accepted(self):
        # "I refused to cut my sister's | wedding cake" — best=6
        # means left ends on "sister's" which is acceptable.
        words = _wphrase("I refused to cut my sister's wedding cake")
        self.assertTrue(_is_acceptable_split_idx(words, best=6))

    def test_split_handles_trailing_punctuation(self):
        # "I went to," with trailing comma should still be rejected
        # for ending on "to" (strip punct before lookup).
        words = [
            _w("I", 0, 0.3), _w("went", 0.3, 0.6), _w("to,", 0.6, 0.9),
            _w("the", 0.9, 1.2), _w("park.", 1.2, 1.5),
        ]
        # best=3 → left ends on "to," → punct stripped → "to" → forbidden.
        self.assertFalse(_is_acceptable_split_idx(words, best=3))

    def test_boundary_indices_pass(self):
        # best=0 or best=len means no real split — return True trivially.
        words = _wphrase("a b c d")
        self.assertTrue(_is_acceptable_split_idx(words, best=0))
        self.assertTrue(_is_acceptable_split_idx(words, best=len(words)))

    def test_case_insensitive_lookup(self):
        # "MY" vs "my" — same forbidden status.
        words = [_w("cut", 0, 0.3), _w("MY", 0.3, 0.6),
                 _w("cake", 0.6, 0.9)]
        self.assertFalse(_is_acceptable_split_idx(words, best=2))


class ShiftToAcceptableTest(unittest.TestCase):

    def test_already_acceptable_returns_unchanged(self):
        words = _wphrase("I cut the cake yesterday afternoon")
        # best=3 → left ends on "the" → forbidden, but words[4]="cake"
        # is acceptable → shift right to 4.
        result = _shift_to_acceptable(words, candidate=3)
        self.assertEqual(result, 4)

    def test_candidate_already_acceptable_returns_self(self):
        # No shift needed — line 577 early return.
        words = _wphrase("alice met bob yesterday afternoon")
        # best=3 → left ends on "bob" (not a stop word) → accept.
        result = _shift_to_acceptable(words, candidate=3)
        self.assertEqual(result, 3)

    def test_walks_right_first(self):
        # "I refused to cut my sister's wedding cake at her reception"
        # candidate=5 → left ends on "my" → walk to 6 ("sister's") OK.
        words = _wphrase("I refused to cut my sister's wedding cake at her reception")
        result = _shift_to_acceptable(words, candidate=5)
        self.assertEqual(result, 6)

    def test_walks_left_when_right_fails(self):
        # "I went to the park" with candidate=3 → left=[I, went, to]
        # ends on "to". Right (4) ends on "the" (also forbidden). Walk
        # left to 2 (left=[I, went], ends on "went" → OK).
        words = _wphrase("I went to the park")
        result = _shift_to_acceptable(words, candidate=3)
        self.assertEqual(result, 2)

    def test_returns_none_when_no_acceptable_within_walk_range(self):
        # Pathological case: 5 stop-words in a row.
        words = _wphrase("the my her of for")
        # candidate=2 → left=[the, my] ends on "my". Walk right finds
        # "her", "of", "for" all forbidden. Walk left finds "the"
        # forbidden. Return None.
        result = _shift_to_acceptable(words, candidate=2, max_walk=3)
        self.assertIsNone(result)

    def test_walks_only_within_max_walk(self):
        # max_walk=1: only check ±1.
        # candidate=1, left ends on "the" (forbidden).
        # Right=2 ("their") forbidden, left=0 is the boundary case
        # (loop guards `left > 0` so 0 is never returned — that would
        # mean "no left side at all", not a valid split). Result: None.
        words = _wphrase("the my their our friend nearby today")
        result = _shift_to_acceptable(words, candidate=1, max_walk=1)
        self.assertIsNone(result,
                          "shift can't reach 0 (would be empty left); "
                          "max_walk=1 with both ±1 forbidden returns None")


class SplitLongGroupStopWordIntegrationTest(unittest.TestCase):

    def test_cake_aita_hook_no_longer_splits_after_my(self):
        """The 27-render audit's smoking gun. Pre-fix this split as
        ['I refused to cut my', "sister's wedding cake at her reception"].
        Post-fix the splitter walks past 'my' to a real word boundary."""
        words = _wphrase(
            "I refused to cut my sister's wedding cake at her reception",
            start=0.0, per_word_s=0.3,
        )
        # Total duration = 11 * 0.3 = 3.3s. max_s=2.0 forces split.
        result = _split_long_group(words, max_s=2.0)
        # Verify NO sub-group ends on a stop-word.
        for i, sub in enumerate(result):
            if sub:
                last_word = sub[-1].text.strip(",.;:!?\"'-").lower()
                self.assertNotIn(
                    last_word, _FORBIDDEN_END_TOKENS,
                    f"sub-group {i} ends on stop-word {last_word!r}: "
                    f"{[w.text for w in sub]}"
                )

    def test_short_group_unchanged(self):
        # Below max_s → no split.
        words = _wphrase("short phrase here", per_word_s=0.3)
        result = _split_long_group(words, max_s=10.0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], words)

    def test_clause_break_takes_precedence_over_stop_word_guard(self):
        # When a comma exists, tier 1 (clause break) is used regardless
        # of whether the post-comma words look stop-y.
        words = [
            _w("First", 0, 0.3), _w("part,", 0.3, 0.6),
            _w("with", 0.6, 0.9), _w("the", 0.9, 1.2),
            _w("rest.", 1.2, 1.5),
        ]
        # Total = 1.5s, but force split via max_s=0.8.
        result = _split_long_group(words, max_s=0.8)
        # First sub-group ends with "part," (a CLAUSE break, valid).
        self.assertEqual(result[0][-1].text, "part,")

    def test_max_s_contract_holds_when_no_acceptable_split(self):
        # All-stop-word phrase: splitter must still produce sub-groups
        # ≤ max_s, even if every boundary lands on a stop word.
        words = _wphrase("the my her of for with by to", per_word_s=0.5)
        # 8 * 0.5 = 4.0s. max_s=1.5 → must split.
        result = _split_long_group(words, max_s=1.5)
        for sub in result:
            if len(sub) > 1:
                dur = sub[-1].end - sub[0].start
                self.assertLessEqual(
                    dur, 1.5 + 0.01,  # tolerance for float math
                    f"max_s contract violated for sub-group "
                    f"{[w.text for w in sub]} (dur={dur:.2f}s)"
                )

    def test_tier_2_conjunction_split_walks_past_stop_word(self):
        # Realistic sentence with no clause break + a conjunction
        # in the middle. Force tier 2 + verify the shift logic fires.
        # "She saw the dog and ran toward the park" — 9 words, "and"
        # at index 4. Default split: left=[She, saw, the, dog] (ends
        # on "dog" — fine), right=[and, ran, toward, the, park].
        # Shift here is unnecessary; just verifies tier 2 fires
        # without breaking and produces non-stop-word endings.
        words = _wphrase("She saw the dog and ran toward the park",
                         per_word_s=0.5)
        # 9 * 0.5 = 4.5s. max_s=2.5 → must split. No commas → tier 2.
        result = _split_long_group(words, max_s=2.5)
        # Verify NO sub-group ends on a forbidden stop word.
        # Note: "park" / "dog" are content words, so this naturally
        # passes — the test mainly proves tier 2 + shift didn't
        # CREATE a worse boundary than the original.
        self.assertGreater(len(result), 1, "should have split")
        for sub in result:
            if sub:
                last_bare = sub[-1].text.strip(",.;:!?\"'-").lower()
                self.assertNotIn(
                    last_bare, _FORBIDDEN_END_TOKENS,
                    f"tier-2 conjunction split left a stop-word ending: "
                    f"{[w.text for w in sub]}"
                )


if __name__ == "__main__":
    unittest.main()
