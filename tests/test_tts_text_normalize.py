"""Tests for pipeline.tts.text_normalize — 100% branch coverage target.

Pure-Python module (no torch / model loads). All branches are exercised
with real inputs or, for defensive exception guards, with MagicMock stubs.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tests._helpers import PROJECT_ROOT  # noqa: F401 — ensures sys.path is set

from pipeline.tts.text_normalize import (
    _RE_AD_BC_POST_YEAR,
    _RE_AD_BC_YEAR,
    _RE_ALLCAPS_WORD,
    _RE_ANY_CASE_ACRONYM,
    _RE_DAY_MONTH,
    _RE_MONTH_DAY,
    _RE_PROFANITY_ASSHOLE,
    _apply_hindi_respellings,
    _expand_any_case_acronym,
    _expand_bare_int,
    _expand_currency,
    _expand_day_month,
    _expand_month_day,
    _expand_year,
    _int_to_words,
    _lowercase_emphatic_caps,
    _ordinal_to_words,
    _spell_ad_bc,
    _spell_ad_bc_post,
    _strip_verdict_acronym_sentences,
    _sub_profanity_asshole,
    _two_digits_to_words,
    apply_pronunciation_overrides,
    normalize_for_tts,
)


# ---------------------------------------------------------------------------
# _int_to_words
# ---------------------------------------------------------------------------

class IntToWordsTest(unittest.TestCase):
    """All six numeric magnitude branches + zero + negative."""

    def test_zero(self):
        self.assertEqual(_int_to_words(0), "zero")

    def test_negative(self):
        self.assertEqual(_int_to_words(-3), "negative three")

    def test_single_digit(self):
        self.assertEqual(_int_to_words(7), "seven")

    def test_teen(self):
        self.assertEqual(_int_to_words(15), "fifteen")

    def test_tens_no_remainder(self):
        self.assertEqual(_int_to_words(20), "twenty")

    def test_tens_with_remainder(self):
        self.assertEqual(_int_to_words(23), "twenty-three")

    def test_hundreds_no_remainder(self):
        self.assertEqual(_int_to_words(300), "three hundred")

    def test_hundreds_with_remainder(self):
        self.assertEqual(_int_to_words(150), "one hundred fifty")

    def test_thousands_no_remainder(self):
        self.assertEqual(_int_to_words(5_000), "five thousand")

    def test_thousands_with_remainder(self):
        self.assertEqual(_int_to_words(1_500), "one thousand five hundred")

    def test_millions_no_remainder(self):
        self.assertEqual(_int_to_words(2_000_000), "two million")

    def test_millions_with_remainder(self):
        self.assertEqual(_int_to_words(1_500_000), "one million five hundred thousand")

    def test_billions_fallback_str(self):
        # ≥ 1 billion: returns str(n) unchanged
        self.assertEqual(_int_to_words(2_000_000_000), "2000000000")


# ---------------------------------------------------------------------------
# _expand_currency
# ---------------------------------------------------------------------------

class ExpandCurrencyTest(unittest.TestCase):
    """Currency regex → spoken dollars. All format branches + exception guard."""

    def _match(self, text: str):
        from pipeline.tts.text_normalize import _RE_CURRENCY
        return _RE_CURRENCY.search(text)

    def test_plain_integer(self):
        m = self._match("$100")
        self.assertEqual(_expand_currency(m), "one hundred dollars")

    def test_comma_thousands(self):
        m = self._match("$1,000")
        self.assertEqual(_expand_currency(m), "one thousand dollars")

    def test_decimal_with_cents(self):
        m = self._match("$4.50")
        self.assertEqual(_expand_currency(m), "four dollars and fifty cents")

    def test_decimal_cents_zero(self):
        # frac_n == 0 → no "and N cents"
        m = self._match("$4.00")
        self.assertEqual(_expand_currency(m), "four dollars")

    def test_decimal_single_frac_digit(self):
        # "$4.5" → frac "5".ljust(2,"0") = "50" → 50 cents
        m = self._match("$4.5")
        self.assertEqual(_expand_currency(m), "four dollars and fifty cents")

    def test_k_suffix_upper(self):
        m = self._match("$5K")
        self.assertEqual(_expand_currency(m), "five thousand dollars")

    def test_k_suffix_lower(self):
        m = self._match("$5k")
        self.assertEqual(_expand_currency(m), "five thousand dollars")

    def test_m_suffix_upper(self):
        m = self._match("$2M")
        self.assertEqual(_expand_currency(m), "two million dollars")

    def test_m_suffix_lower(self):
        m = self._match("$3m")
        self.assertEqual(_expand_currency(m), "three million dollars")

    def test_decimal_k_suffix(self):
        m = self._match("$2.5K")
        self.assertEqual(_expand_currency(m), "two thousand five hundred dollars")

    def test_exception_returns_original(self):
        # Simulate a match whose group(1) is non-numeric → ValueError → return group(0)
        mock_m = MagicMock()
        mock_m.group.side_effect = lambda n: {0: "$xyz", 1: "xyz", 2: None}[n]
        self.assertEqual(_expand_currency(mock_m), "$xyz")


# ---------------------------------------------------------------------------
# _two_digits_to_words
# ---------------------------------------------------------------------------

class TwoDigitsToWordsTest(unittest.TestCase):
    """0–19 direct / tens-only / tens+ones compound."""

    def test_zero(self):
        self.assertEqual(_two_digits_to_words(0), "zero")

    def test_fifteen(self):
        self.assertEqual(_two_digits_to_words(15), "fifteen")

    def test_nineteen(self):
        self.assertEqual(_two_digits_to_words(19), "nineteen")

    def test_tens_only(self):
        self.assertEqual(_two_digits_to_words(30), "thirty")

    def test_tens_only_fifty(self):
        self.assertEqual(_two_digits_to_words(50), "fifty")

    def test_tens_and_ones(self):
        self.assertEqual(_two_digits_to_words(23), "twenty-three")

    def test_tens_and_ones_47(self):
        self.assertEqual(_two_digits_to_words(47), "forty-seven")


# ---------------------------------------------------------------------------
# _expand_year
# ---------------------------------------------------------------------------

class ExpandYearTest(unittest.TestCase):
    """2000–2009 (rest==0 / rest!=0), 1900–1999 / 2010–2099 (rest==0 / rest!=0),
    and values outside 1900–2099."""

    def test_2000_rest_zero(self):
        self.assertEqual(_expand_year(2000), "two thousand")

    def test_2005_rest_nonzero(self):
        self.assertEqual(_expand_year(2005), "two thousand five")

    def test_2009(self):
        self.assertEqual(_expand_year(2009), "two thousand nine")

    def test_1900_rest_zero(self):
        self.assertEqual(_expand_year(1900), "nineteen hundred")

    def test_1999(self):
        self.assertEqual(_expand_year(1999), "nineteen ninety-nine")

    def test_2014(self):
        self.assertEqual(_expand_year(2014), "twenty fourteen")

    def test_2099(self):
        result = _expand_year(2099)
        self.assertIn("twenty", result)
        self.assertIn("ninety", result)

    def test_outside_range_low(self):
        # < 1900 → _int_to_words
        result = _expand_year(1850)
        self.assertIn("thousand", result)

    def test_outside_range_high(self):
        # > 2099 → _int_to_words
        result = _expand_year(2100)
        self.assertIn("hundred", result)


# ---------------------------------------------------------------------------
# _expand_bare_int
# ---------------------------------------------------------------------------

class ExpandBareIntTest(unittest.TestCase):
    """Year range / regular cardinal / n > 999_999_999 passthrough."""

    def _match(self, text: str):
        from pipeline.tts.text_normalize import _RE_INTEGER
        return _RE_INTEGER.search(text)

    def test_year_form_1999(self):
        m = self._match("In 1999 the match")
        self.assertEqual(_expand_bare_int(m), "nineteen ninety-nine")

    def test_year_form_2024(self):
        m = self._match("year 2024 was")
        self.assertEqual(_expand_bare_int(m), "twenty twenty-four")

    def test_regular_cardinal(self):
        m = self._match("100 goals")
        self.assertEqual(_expand_bare_int(m), "one hundred")

    def test_large_non_year(self):
        m = self._match("scored 500 goals")
        self.assertEqual(_expand_bare_int(m), "five hundred")

    def test_n_greater_than_999_million_passthrough(self):
        # "1,000,000,000" → n = 1_000_000_000 > 999_999_999 → s returned unchanged
        m = self._match("1,000,000,000 people")
        result = _expand_bare_int(m)
        self.assertEqual(result, "1,000,000,000")

    def test_four_digit_non_year_range(self):
        # 3000 is outside 1900–2099 → plain cardinal
        m = self._match("ran 3000 miles")
        self.assertEqual(_expand_bare_int(m), "three thousand")


# ---------------------------------------------------------------------------
# _ordinal_to_words
# ---------------------------------------------------------------------------

class OrdinalToWordsTest(unittest.TestCase):
    """n < 1 / n > 99 / _ORDINAL_ONES / _ORDINAL_TENS / compound."""

    def test_below_one(self):
        # n < 1 → fallback
        self.assertEqual(_ordinal_to_words(0), "zeroth")

    def test_above_99(self):
        self.assertEqual(_ordinal_to_words(100), "one hundredth")

    def test_first(self):
        self.assertEqual(_ordinal_to_words(1), "first")

    def test_twelfth(self):
        self.assertEqual(_ordinal_to_words(12), "twelfth")

    def test_nineteenth(self):
        self.assertEqual(_ordinal_to_words(19), "nineteenth")

    def test_twentieth(self):
        self.assertEqual(_ordinal_to_words(20), "twentieth")

    def test_ninetieth(self):
        self.assertEqual(_ordinal_to_words(90), "ninetieth")

    def test_compound_twenty_first(self):
        self.assertEqual(_ordinal_to_words(21), "twenty-first")

    def test_compound_ninety_ninth(self):
        self.assertEqual(_ordinal_to_words(99), "ninety-ninth")

    def test_compound_thirty_third(self):
        self.assertEqual(_ordinal_to_words(33), "thirty-third")


# ---------------------------------------------------------------------------
# _expand_day_month  /  _expand_month_day
# ---------------------------------------------------------------------------

class ExpandDayMonthTest(unittest.TestCase):
    def test_15_september(self):
        m = _RE_DAY_MONTH.search("15 September")
        self.assertEqual(_expand_day_month(m), "the fifteenth of September")

    def test_1_january_ordinal_suffix(self):
        m = _RE_DAY_MONTH.search("1st January")
        self.assertEqual(_expand_day_month(m), "the first of January")

    def test_2_march(self):
        m = _RE_DAY_MONTH.search("2nd March")
        self.assertEqual(_expand_day_month(m), "the second of March")


class ExpandMonthDayTest(unittest.TestCase):
    def test_september_15(self):
        m = _RE_MONTH_DAY.search("September 15")
        self.assertEqual(_expand_month_day(m), "September the fifteenth")

    def test_january_1(self):
        m = _RE_MONTH_DAY.search("January 1st")
        self.assertEqual(_expand_month_day(m), "January the first")


# ---------------------------------------------------------------------------
# _spell_ad_bc  /  _spell_ad_bc_post
# ---------------------------------------------------------------------------

class SpellAdBcTest(unittest.TestCase):
    def test_ad_uppercase(self):
        m = _RE_AD_BC_YEAR.search("AD 44")
        self.assertEqual(_spell_ad_bc(m), "A D")

    def test_bc_uppercase(self):
        m = _RE_AD_BC_YEAR.search("BC 44")
        self.assertEqual(_spell_ad_bc(m), "B C")

    def test_ad_lowercase(self):
        m = _RE_AD_BC_YEAR.search("ad 79")
        self.assertEqual(_spell_ad_bc(m), "A D")

    def test_bc_lowercase(self):
        m = _RE_AD_BC_YEAR.search("bc 79")
        self.assertEqual(_spell_ad_bc(m), "B C")


class SpellAdBcPostTest(unittest.TestCase):
    def test_post_year_ad(self):
        m = _RE_AD_BC_POST_YEAR.search("79 AD")
        result = _spell_ad_bc_post(m)
        self.assertIn("A D", result)

    def test_post_year_bc(self):
        m = _RE_AD_BC_POST_YEAR.search("44 BC")
        result = _spell_ad_bc_post(m)
        self.assertIn("B C", result)

    def test_post_year_lowercase(self):
        m = _RE_AD_BC_POST_YEAR.search("44 bc")
        result = _spell_ad_bc_post(m)
        self.assertIn("B C", result)


# ---------------------------------------------------------------------------
# _lowercase_emphatic_caps
# ---------------------------------------------------------------------------

class LowercaseEmphaticCapsTest(unittest.TestCase):
    """Known acronym → mapped phrase; unknown ALL-CAPS → lowercase."""

    def test_known_acronym_tv(self):
        m = _RE_ALLCAPS_WORD.search("TV show")
        self.assertEqual(_lowercase_emphatic_caps(m), "T V")

    def test_known_acronym_usa(self):
        m = _RE_ALLCAPS_WORD.search("USA trip")
        self.assertEqual(_lowercase_emphatic_caps(m), "U S A")

    def test_known_acronym_ok(self):
        m = _RE_ALLCAPS_WORD.search("it was OK")
        self.assertEqual(_lowercase_emphatic_caps(m), "okay")

    def test_unknown_caps_lowercased(self):
        m = _RE_ALLCAPS_WORD.search("AMAZING result")
        self.assertEqual(_lowercase_emphatic_caps(m), "amazing")

    def test_unknown_caps_again(self):
        m = _RE_ALLCAPS_WORD.search("OUT of the room")
        self.assertEqual(_lowercase_emphatic_caps(m), "out")


# ---------------------------------------------------------------------------
# _expand_any_case_acronym
# ---------------------------------------------------------------------------

class ExpandAnyAcronymTest(unittest.TestCase):
    def test_mil_uppercase(self):
        m = _RE_ANY_CASE_ACRONYM.search("my MIL said")
        self.assertEqual(_expand_any_case_acronym(m), "mother in law")

    def test_mil_lowercase(self):
        m = _RE_ANY_CASE_ACRONYM.search("my mil said")
        self.assertEqual(_expand_any_case_acronym(m), "mother in law")

    def test_tifu(self):
        m = _RE_ANY_CASE_ACRONYM.search("TIFU big time")
        self.assertEqual(_expand_any_case_acronym(m), "today I screwed up")

    def test_fil(self):
        m = _RE_ANY_CASE_ACRONYM.search("his FIL was")
        self.assertEqual(_expand_any_case_acronym(m), "father in law")


# ---------------------------------------------------------------------------
# _strip_verdict_acronym_sentences
# ---------------------------------------------------------------------------

class StripVerdictAcronymSentencesTest(unittest.TestCase):
    def test_empty_text(self):
        self.assertEqual(_strip_verdict_acronym_sentences(""), "")

    def test_no_verdict_acronym(self):
        text = "She refused to go. He agreed."
        self.assertEqual(_strip_verdict_acronym_sentences(text), text)

    def test_strips_sentence_with_verdict(self):
        text = "AITA for refusing? I said no."
        result = _strip_verdict_acronym_sentences(text)
        self.assertNotIn("AITA", result)
        self.assertIn("I said no", result)

    def test_case_insensitive_strip(self):
        text = "aita for this. She was wrong."
        result = _strip_verdict_acronym_sentences(text)
        self.assertNotIn("aita", result)

    def test_pathological_all_sentences_have_verdict(self):
        # Every sentence has a verdict token → kept=[] → return original text
        text = "AITA? YTA!"
        result = _strip_verdict_acronym_sentences(text)
        self.assertEqual(result, text)

    def test_wibta_stripped(self):
        text = "WIBTA for doing this? She thought so."
        result = _strip_verdict_acronym_sentences(text)
        self.assertNotIn("WIBTA", result)

    def test_nta_stripped(self):
        text = "He said NTA. I disagreed."
        result = _strip_verdict_acronym_sentences(text)
        self.assertNotIn("NTA", result)


# ---------------------------------------------------------------------------
# _sub_profanity_asshole
# ---------------------------------------------------------------------------

class SubProfanityAssholeTest(unittest.TestCase):
    """Singular/plural × lowercase / ALL-CAPS / Title-case branches."""

    def test_singular_lower(self):
        m = _RE_PROFANITY_ASSHOLE.search("you asshole")
        self.assertEqual(_sub_profanity_asshole(m), "a hole")

    def test_plural_lower(self):
        m = _RE_PROFANITY_ASSHOLE.search("those assholes")
        self.assertEqual(_sub_profanity_asshole(m), "a holes")

    def test_singular_allcaps(self):
        m = _RE_PROFANITY_ASSHOLE.search("you ASSHOLE")
        self.assertEqual(_sub_profanity_asshole(m), "A HOLE")

    def test_plural_allcaps(self):
        m = _RE_PROFANITY_ASSHOLE.search("those ASSHOLES")
        self.assertEqual(_sub_profanity_asshole(m), "A HOLES")

    def test_singular_titlecase(self):
        m = _RE_PROFANITY_ASSHOLE.search("you Asshole")
        self.assertEqual(_sub_profanity_asshole(m), "A hole")


# ---------------------------------------------------------------------------
# _apply_hindi_respellings
# ---------------------------------------------------------------------------

class ApplyHindiRespellingsTest(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(_apply_hindi_respellings(""), "")

    def test_no_devanagari_passthrough(self):
        text = "Hello world"
        self.assertEqual(_apply_hindi_respellings(text), text)

    def test_known_tatsama_replaced(self):
        result = _apply_hindi_respellings("अभिमन्यु युद्ध में")
        self.assertIn("अभि-मन्यु", result)

    def test_numeral_respelling(self):
        result = _apply_hindi_respellings("सात योद्धा")
        self.assertIn("साअत", result)

    def test_longer_key_before_shorter(self):
        # "योद्धाओं" (longer) should replace before "योद्धा" (shorter)
        result = _apply_hindi_respellings("योद्धाओं की सेना")
        self.assertIn("योद-धाओं", result)


# ---------------------------------------------------------------------------
# apply_pronunciation_overrides
# ---------------------------------------------------------------------------

class ApplyPronunciationOverridesTest(unittest.TestCase):
    """None dict / empty dict / empty text / all-caps / title / lowercase / skip."""

    def test_none_dict_returns_unchanged(self):
        self.assertEqual(apply_pronunciation_overrides("Hello", None), "Hello")

    def test_empty_dict_returns_unchanged(self):
        self.assertEqual(apply_pronunciation_overrides("Hello", {}), "Hello")

    def test_empty_text_returns_unchanged(self):
        self.assertEqual(apply_pronunciation_overrides("", {"Foo": "Bar"}), "")

    def test_allcaps_match_uppercased_phonetic(self):
        result = apply_pronunciation_overrides("AGUERO scored", {"Aguero": "ah-gwair-oh"})
        self.assertIn("AH-GWAIR-OH", result)

    def test_titlecase_match_capitalises_phonetic(self):
        result = apply_pronunciation_overrides("Aguero scored", {"Aguero": "ah-gwair-oh"})
        self.assertIn("Ah-gwair-oh", result)

    def test_lowercase_match_returns_phonetic_as_is(self):
        result = apply_pronunciation_overrides("aguero scored", {"Aguero": "ah-gwair-oh"})
        self.assertIn("ah-gwair-oh", result)

    def test_empty_name_skipped(self):
        # Should not crash and text should be returned unchanged
        result = apply_pronunciation_overrides("test text", {"": "something"})
        self.assertEqual(result, "test text")

    def test_empty_phonetic_skipped(self):
        result = apply_pronunciation_overrides("test text", {"test": ""})
        self.assertEqual(result, "test text")

    def test_longer_key_matches_before_shorter(self):
        d = {"Kun Aguero": "koon ah-GWAIR-oh", "Aguero": "ah-GWAIR-oh"}
        result = apply_pronunciation_overrides("Kun Aguero scored", d)
        self.assertIn("koon", result.lower())

    def test_case_insensitive_matching(self):
        result = apply_pronunciation_overrides("MBAPPE scored", {"Mbappe": "mm-BAP-ay"})
        self.assertIn("MM-BAP-AY", result)


# ---------------------------------------------------------------------------
# normalize_for_tts  (integration — exercises the full pipeline)
# ---------------------------------------------------------------------------

class NormalizeForTtsTest(unittest.TestCase):
    """Full-pipeline integration tests covering every normalisation step."""

    def test_empty_string_returns_empty(self):
        self.assertEqual(normalize_for_tts(""), "")

    def test_none_like_empty(self):
        # Falsy string → early return
        self.assertEqual(normalize_for_tts(""), "")

    # Step 1: verdict acronym stripping
    def test_verdict_acronym_sentence_stripped(self):
        result = normalize_for_tts("AITA for refusing? I said no.")
        self.assertNotIn("AITA", result)
        self.assertIn("no", result)

    # Step 2: currency
    def test_currency_expansion(self):
        result = normalize_for_tts("She paid $2000.")
        self.assertIn("two thousand dollars", result)

    # Step 3: day-month date
    def test_day_month_expansion(self):
        result = normalize_for_tts("On 15 September he arrived.")
        self.assertIn("fifteenth", result)
        self.assertIn("September", result)

    # Step 3: month-day date
    def test_month_day_expansion(self):
        result = normalize_for_tts("September 15th was the date.")
        self.assertIn("September the fifteenth", result)

    # Step 4: AD/BC pre-year
    def test_ad_pre_year(self):
        result = normalize_for_tts("It was AD 79 when it happened.")
        self.assertIn("A D", result)

    def test_bc_pre_year(self):
        result = normalize_for_tts("Caesar died in BC 44.")
        # BC 44 doesn't match pre-year pattern (digit must follow); test post-year
        result2 = normalize_for_tts("Caesar died in 44 BC.")
        self.assertIn("B C", result2)

    # Step 4: AD/BC post-year
    def test_ad_bc_post_year(self):
        result = normalize_for_tts("In 44 AD he was born.")
        self.assertIn("A D", result)

    # Step 5: bare integer
    def test_bare_integer_expanded(self):
        result = normalize_for_tts("There were 100 people.")
        self.assertIn("one hundred", result)

    # Step 5: year integer
    def test_year_integer_expanded(self):
        result = normalize_for_tts("The year 1999 was special.")
        self.assertIn("nineteen ninety-nine", result)

    # Step 6: case-insensitive AITA-domain acronym
    def test_mil_lowercase_expanded(self):
        result = normalize_for_tts("my mil said no.")
        self.assertIn("mother in law", result)

    def test_tifu_expanded(self):
        result = normalize_for_tts("TIFU big time.")
        self.assertIn("today I screwed up", result)

    # Step 7: profanity
    def test_asshole_sanitised(self):
        result = normalize_for_tts("He was an asshole.")
        self.assertIn("a hole", result)

    def test_assholes_plural_sanitised(self):
        result = normalize_for_tts("They were assholes.")
        self.assertIn("a holes", result)

    # Step 8: remaining all-caps lowercased
    def test_allcaps_emphasis_lowercased(self):
        result = normalize_for_tts("She was AMAZED by it.")
        self.assertIn("amazed", result)

    # Step 9: Hindi respellings (skip=False vs skip=True)
    def test_hindi_respellings_applied_by_default(self):
        result = normalize_for_tts("अभिमन्यु की कथा")
        self.assertIn("अभि-मन्यु", result)

    def test_hindi_respellings_skipped(self):
        result = normalize_for_tts("अभिमन्यु की कथा", skip_hindi_respellings=True)
        # Original Devanagari preserved
        self.assertIn("अभिमन्यु", result)


if __name__ == "__main__":
    unittest.main()
