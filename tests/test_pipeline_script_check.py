"""Tests for pipeline.script_check — narration-shape validators.

Covers the closer CTA, hook timing, wedge presence, AITA-class
LIKE/COMMENT split detection, and the slow-hook beat check.
"""

from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT, fake_beat_list  # noqa: F401

from pipeline.llm.script_check import check_beats, check_script_text


GOOD_AITA = (
    "AITA for refusing a water birth in my living room? My DIL wants "
    "to give birth here. My place is two bedrooms. I told her no. She's "
    "furious. Like if YTA, comment if NTA."
)


def _codes(issues):
    return [i.code for i in issues]


class CheckScriptTextCtaTest(unittest.TestCase):
    def test_question_mark_satisfies_cta(self):
        text = "I refused. I had ten dollars. What would you do?"
        self.assertNotIn("missing_cta", _codes(check_script_text(text)))

    def test_no_cta_flagged(self):
        text = "I refused. I had ten dollars. We never spoke again."
        self.assertIn("missing_cta", _codes(check_script_text(text)))


class CheckScriptTextHookTest(unittest.TestCase):
    def test_aita_frame_in_first_8_words_passes(self):
        text = "AITA for refusing my sister's wedding cake? I have ten reasons. AITA?"
        self.assertNotIn("weak_hook", _codes(check_script_text(text)))

    def test_strong_claim_verb_passes(self):
        text = "I refused to bake my sister three cakes. AITA?"
        self.assertNotIn("weak_hook", _codes(check_script_text(text)))

    def test_question_in_hook_passes(self):
        # `?` must land within the first ~8 words to count as a hook.
        text = "Should I get a divorce? My husband threw away ten dollars. AITA?"
        self.assertNotIn("weak_hook", _codes(check_script_text(text)))

    def test_bland_opening_flagged(self):
        text = "Today I want to share a story about my family. We had ten cookies. What do you think?"
        self.assertIn("weak_hook", _codes(check_script_text(text)))


class CheckScriptTextWedgeTest(unittest.TestCase):
    def test_digit_number_satisfies_wedge(self):
        text = "I refused to share my $400 dinner. AITA?"
        self.assertNotIn("missing_wedge", _codes(check_script_text(text)))

    def test_word_number_satisfies_wedge(self):
        text = "I refused. There were three bottles of wine and a steak. AITA?"
        self.assertNotIn("missing_wedge", _codes(check_script_text(text)))

class CheckScriptTextEmptyTest(unittest.TestCase):
    def test_empty_returns_error(self):
        issues = check_script_text("")
        self.assertEqual([i.code for i in issues], ["empty"])
        self.assertEqual(issues[0].severity, "error")

    def test_whitespace_only(self):
        issues = check_script_text("   \n  ")
        self.assertEqual([i.code for i in issues], ["empty"])


class CheckScriptTextAitaClassTest(unittest.TestCase):
    """When channel_cfg has closer_format set, the like/comment split is
    required, and soft principles escalate to errors."""

    def test_full_aita_closer_passes(self):
        cfg = {"closer_format": "aita"}
        issues = check_script_text(GOOD_AITA, channel_cfg=cfg)
        self.assertNotIn("weak_closer", _codes(issues))

    def test_weak_hook_escalates_to_error_for_aita(self):
        cfg = {"closer_format": "aita"}
        # bland hook, has wedge + cta, has like/comment split
        text = (
            "So basically my husband and I had three kids. They wanted dinner. "
            "Like if YTA comment if NTA."
        )
        issues = check_script_text(text, channel_cfg=cfg)
        weak = next((i for i in issues if i.code == "weak_hook"), None)
        if weak:
            self.assertEqual(weak.severity, "error")

    def test_weak_hook_only_warning_without_closer_format(self):
        text = "Today I want to share a story about my three kids' dinner. AITA?"
        issues = check_script_text(text)  # no channel_cfg
        weak = next((i for i in issues if i.code == "weak_hook"), None)
        if weak:
            self.assertEqual(weak.severity, "warning")


class CheckBeatsTest(unittest.TestCase):
    def _beats(self, *, b0_dur=2.0):
        # Beat 0 is the hook — duration tested specifically.
        return [
            type("B", (), {
                "text": "AITA for refusing my sister's wedding cake?",
                "start": 0.0, "end": b0_dur,
                "duration": b0_dur,
            })(),
            type("B", (), {
                "text": "I had ten dollars and three cakes.",
                "start": b0_dur, "end": b0_dur + 2.0, "duration": 2.0,
            })(),
            type("B", (), {
                "text": "Like if YTA comment if NTA. AITA?",
                "start": b0_dur + 2.0, "end": b0_dur + 4.0, "duration": 2.0,
            })(),
        ]

    def test_no_beats_error(self):
        self.assertEqual(_codes(check_beats([])), ["no_beats"])

    def test_slow_hook_beat_flagged(self):
        beats = self._beats(b0_dur=3.0)
        self.assertIn("slow_hook", _codes(check_beats(beats)))

    def test_fast_hook_passes(self):
        beats = self._beats(b0_dur=1.5)
        self.assertNotIn("slow_hook", _codes(check_beats(beats)))


if __name__ == "__main__":
    unittest.main()
