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


# ---- Additional coverage for newer validators ----

import contextlib
import io

from pipeline.llm import script_check as sc


class LastSentenceHelperTest(unittest.TestCase):
    def test_last_sentence_handles_empty_and_split_text(self):
        self.assertEqual(sc._last_sentence("First. Last question?"), "Last question?")
        self.assertEqual(sc._last_sentence(""), "")


class CloserTokenAndLeakPatternTest(unittest.TestCase):
    def test_required_closer_tokens_dedupes_and_drops_glue(self):
        tokens = sc._required_closer_tokens("LIKE if YTA, COMMENT your worst COMMENT")
        self.assertEqual(tokens, ["like", "yta", "comment", "worst"])

    def test_leak_patterns_bigram_common_cta_words_and_standalone_acronyms(self):
        pats = sc._leak_patterns("LIKE if YTA COMMENT your NTA")
        self.assertTrue(any(p.search("please LIKE if you agree") for p in pats))
        self.assertTrue(any(p.search("This says YTA early") for p in pats))
        self.assertFalse(any(p.pattern == r"\bLIKE\b" and p.search("I like this") for p in pats))

    def test_check_beats_flags_cta_leak_before_final_beat(self):
        beats = [
            type("B", (), {"text": "I refused with ten dollars. LIKE if YTA", "duration": 1.0})(),
            type("B", (), {"text": "What would you have done?", "duration": 1.0})(),
        ]
        issues = sc.check_beats(beats, channel_cfg={"closer_format": "LIKE if YTA COMMENT if NTA"})
        leak = next(i for i in issues if i.code == "cta_leak")
        self.assertEqual(leak.severity, "error")


class CheckScriptTextAdditionalBranchesTest(unittest.TestCase):
    def test_long_narration_warning(self):
        text = "I refused " + "word " * 170 + "What would you have done?"
        issues = sc.check_script_text(text)
        self.assertIn("long_narration", _codes(issues))

    def test_strict_false_keeps_soft_issues_as_warnings(self):
        text = "Today this bland opening has three cookies. It simply ends."
        issues = sc.check_script_text(text, channel_cfg={"closer_format": "LIKE if YTA", "script_check_strict": False})
        self.assertTrue(all(i.severity == "warning" for i in issues if i.code in {"missing_cta", "weak_hook"}))

    def test_cliffhanger_cta_examples_and_acceptance(self):
        bad = sc.check_script_text("I found three boxes. The door opened.", channel_cfg={"cliffhanger": True})
        self.assertIn("missing_cta", _codes(bad))
        good = sc.check_script_text("I found three boxes. Part 2 drops next. Subscribe so you do not miss it.", channel_cfg={"cliffhanger": True})
        self.assertNotIn("missing_cta", _codes(good))


class HookAnaphoraTest(unittest.TestCase):
    def test_too_short_and_no_anaphora_fail(self):
        if not hasattr(sc, "check_hook_anaphora"):
            self.skipTest("hook anaphora gate not present in this source version")
        self.assertEqual(sc.check_hook_anaphora("One. Two." )[0].code, "hook_too_short")
        issues = sc.check_hook_anaphora({"clauses": ["Imagine the goal.", "Forty years waiting.", "One night changed it."]})
        self.assertEqual(issues[0].code, "hook_no_anaphora")

    def test_parallel_clauses_pass_from_text_field(self):
        if not hasattr(sc, "check_hook_anaphora"):
            self.skipTest("hook anaphora gate not present in this source version")
        issues = sc.check_hook_anaphora({"text": "Forty years waiting. Forty years hoping. Forty years hurting."})
        self.assertEqual(issues, [])


class SubscribeCtaAndFootagePlanTest(unittest.TestCase):
    def test_single_subscribe_cta_all_failures_and_success(self):
        if not hasattr(sc, "check_single_subscribe_cta"):
            self.skipTest("single subscribe CTA gate not present in this source version")
        self.assertEqual(sc.check_single_subscribe_cta({}, channel_display_name="Sports")[0].code, "cta_missing")
        bad = sc.check_single_subscribe_cta({"cta_text": "Smash this button now without naming anything at all and keep talking for far too many extra words please"}, channel_display_name="Sports", max_words=5)
        self.assertIn("cta_too_long", _codes(bad))
        self.assertIn("cta_no_subscribe", _codes(bad))
        self.assertIn("cta_no_bell", _codes(bad))
        self.assertIn("cta_no_channel_name", _codes(bad))
        self.assertIn("cta_smash_banned", _codes(bad))
        good = sc.check_single_subscribe_cta({"cta_text": "Subscribe to Sports and hit the bell."}, channel_display_name="Sports")
        self.assertEqual(good, [])

    def test_no_talking_heads_gate(self):
        if not hasattr(sc, "check_no_talking_heads"):
            self.skipTest("talking-head gate not present in this source version")
        self.assertEqual(sc.check_no_talking_heads({}), [])
        issues = sc.check_no_talking_heads({"commentary_takes": [1], "talking_heads": [1, 2]})
        self.assertEqual([i.code for i in issues], ["talking_heads_forbidden", "talking_heads_forbidden"])


class ReportTest(unittest.TestCase):
    def test_report_prints_success_warnings_errors_and_raises(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sc.report([])
        self.assertIn("no issues", buf.getvalue())
        issues = [sc.ScriptIssue("warning", "warn", "careful"), sc.ScriptIssue("error", "err", "bad")]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sc.report(issues, fail_on_error=False)
        out = buf.getvalue()
        self.assertIn("⚠ [warn]", out)
        self.assertIn("✗ [err]", out)
        with self.assertRaises(ValueError):
            sc.report(issues, fail_on_error=True)


class NonEnglishLanguageGateTest(unittest.TestCase):
    """Pin the 2026-05-15 Hindi-validator regression.

    Backstory: every CTA / hook / wedge regex in
    ``pipeline.llm.script_check`` was English-only. Job cdd90432
    (HindutavaAnimated Krishna leela Short) was authored fluently by
    the LLM in Devanagari but failed validation 3 times in a row::

        stage 'rewrite' failed validation after 3 attempts:
        long_narration(warning), missing_cta(error), missing_wedge(warning)

    There's no way for any Devanagari ending to match
    ``\\bam i wrong\\b`` etc., so the gate would reject every Hindi
    rewrite forever.

    The fix routes every non-English channel (``tts_language``
    anything other than ``en`` / ``en-us`` / ``en-gb`` / unset)
    through a single ``non_english_cta_skipped`` info entry instead
    of running the four English-only checks. Per-language regex
    sets are the next pass; for now we trust the LLM to land its
    own close in the requested locale.
    """

    def _check(self, text: str, channel_cfg: dict | None) -> list:
        from pipeline.llm.script_check import check_script_text
        return check_script_text(text, channel_cfg=channel_cfg)

    def test_hindi_text_skips_english_only_checks(self):
        # Devanagari narration with NO English CTA / hook / wedge.
        text = (
            "बहुत समय पहले, वृंदावन में एक छोटा बालक रहता था। "
            "उसका नाम कृष्ण था। एक दिन, उसने गोवर्धन पर्वत उठा लिया।"
        )
        cfg = {"tts_language": "hi", "closer_format": "subscribe"}
        issues = self._check(text, cfg)
        # Should NOT contain missing_cta / missing_wedge / weak_hook
        names = {i.code for i in issues}
        self.assertNotIn("missing_cta", names)
        self.assertNotIn("missing_wedge", names)
        self.assertNotIn("weak_hook", names)
        # SHOULD contain the gate marker
        self.assertIn("non_english_cta_skipped", names)

    def test_english_text_still_runs_all_checks(self):
        # Plain English narration without a CTA — regression sentinel
        # that the gate doesn't accidentally also disable for English.
        text = "This is a story about Krishna. He lifted a mountain. The end."
        cfg = {"tts_language": "en", "closer_format": "subscribe"}
        issues = self._check(text, cfg)
        names = {i.code for i in issues}
        # missing_cta SHOULD still fire on English without CTA.
        self.assertIn("missing_cta", names)
        # gate marker should NOT appear for English channels
        self.assertNotIn("non_english_cta_skipped", names)

    def test_unset_tts_language_defaults_to_english(self):
        # No tts_language → assume English → all checks run.
        text = "A short narration with no closing question."
        cfg = {"closer_format": "subscribe"}
        issues = self._check(text, cfg)
        names = {i.code for i in issues}
        self.assertIn("missing_cta", names)
        self.assertNotIn("non_english_cta_skipped", names)

    def test_en_us_and_en_gb_are_treated_as_english(self):
        for lang in ("en", "en-US", "EN-GB", " en ", "en-us"):
            text = "A short narration with no closing question."
            cfg = {"tts_language": lang, "closer_format": "subscribe"}
            issues = self._check(text, cfg)
            names = {i.code for i in issues}
            self.assertIn(
                "missing_cta", names,
                f"tts_language={lang!r} should be treated as English",
            )
            self.assertNotIn(
                "non_english_cta_skipped", names,
                f"tts_language={lang!r} should NOT trigger language gate",
            )

    def test_other_locales_skip_checks(self):
        for lang in ("hi", "es", "ta", "ar", "ja", "zh-CN"):
            text = "Some non-english text — checker shouldn't pile on."
            cfg = {"tts_language": lang, "closer_format": "subscribe"}
            issues = self._check(text, cfg)
            names = {i.code for i in issues}
            self.assertNotIn(
                "missing_cta", names,
                f"tts_language={lang!r} should skip English-only checks",
            )


if __name__ == "__main__":
    unittest.main()
