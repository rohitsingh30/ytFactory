"""Tests for pipeline.text.hindi_normalize — 100% branch coverage target.

Pure-Python module: no network, no model loads, no file I/O needed.
"""
from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401 — ensures sys.path is set

from pipeline.text.hindi_normalize import (
    _collapse_whitespace,
    _number_to_words,
    _replace_loanwords,
    _replace_numbers,
    _replace_punctuation,
    _two_digit_to_words,
    _year_to_words,
    normalize_hindi_for_tts,
)


# ---------------------------------------------------------------------------
# _two_digit_to_words
# ---------------------------------------------------------------------------

class TwoDigitToWordsTest(unittest.TestCase):
    """Covers three branches: direct _NUMBER_WORDS hit / n < 10 / compound."""

    # branch 1: n in _NUMBER_WORDS (10–50 individually, 60/70/80/90)
    def test_ten(self):
        self.assertEqual(_two_digit_to_words(10), "दस")

    def test_eleven(self):
        self.assertEqual(_two_digit_to_words(11), "ग्यारह")

    def test_nineteen(self):
        self.assertEqual(_two_digit_to_words(19), "उन्नीस")

    def test_twenty(self):
        self.assertEqual(_two_digit_to_words(20), "बीस")

    def test_forty_five(self):
        self.assertEqual(_two_digit_to_words(45), "पैंतालीस")

    def test_fifty(self):
        self.assertEqual(_two_digit_to_words(50), "पचास")

    def test_sixty(self):
        self.assertEqual(_two_digit_to_words(60), "साठ")

    def test_ninety(self):
        self.assertEqual(_two_digit_to_words(90), "नब्बे")

    # branch 2: n < 10 → _DIGIT_WORDS
    def test_zero(self):
        self.assertEqual(_two_digit_to_words(0), "शून्य")

    def test_one(self):
        self.assertEqual(_two_digit_to_words(1), "एक")

    def test_nine(self):
        self.assertEqual(_two_digit_to_words(9), "नौ")

    # branch 3: compound tens + ones (e.g. 51 = 50 + 1)
    def test_fifty_one(self):
        self.assertEqual(_two_digit_to_words(51), "पचास एक")

    def test_sixty_five(self):
        self.assertEqual(_two_digit_to_words(65), "साठ पाँच")

    def test_ninety_nine(self):
        self.assertEqual(_two_digit_to_words(99), "नब्बे नौ")

    def test_seventy_three(self):
        self.assertEqual(_two_digit_to_words(73), "सत्तर तीन")


# ---------------------------------------------------------------------------
# _year_to_words
# ---------------------------------------------------------------------------

class YearToWordsTest(unittest.TestCase):
    """Covers: 1900–1999 (rest==0 / rest!=0), 2000–2099 (rest==0 / rest!=0),
    and the else fall-through to _number_to_words."""

    # 1900–1999, rest == 0
    def test_1900_rest_zero(self):
        result = _year_to_words(1900)
        self.assertIn("उन्नीस", result)
        self.assertIn("सौ", result)

    # 1900–1999, rest != 0
    def test_1947(self):
        result = _year_to_words(1947)
        self.assertIn("उन्नीस", result)
        self.assertIn("सौ", result)

    def test_1944(self):
        result = _year_to_words(1944)
        self.assertIn("चौवालीस", result)

    # 2000–2099, rest == 0
    def test_2000_rest_zero(self):
        self.assertEqual(_year_to_words(2000), "दो हज़ार")

    # 2000–2099, rest != 0
    def test_2024(self):
        result = _year_to_words(2024)
        self.assertTrue(result.startswith("दो हज़ार"))
        self.assertIn("चौबीस", result)

    def test_2001(self):
        result = _year_to_words(2001)
        self.assertTrue(result.startswith("दो हज़ार"))

    # else: outside 1900–2099 → falls through to _number_to_words
    def test_outside_low(self):
        result = _year_to_words(1800)
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_outside_high(self):
        result = _year_to_words(2200)
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)


# ---------------------------------------------------------------------------
# _number_to_words
# ---------------------------------------------------------------------------

class NumberToWordsTest(unittest.TestCase):
    """Covers all five branches: direct map / <100 / <1000 / <100000 / fallback."""

    # branch 1: n in _NUMBER_WORDS directly
    def test_direct_100(self):
        self.assertEqual(_number_to_words(100), "सौ")

    def test_direct_1000(self):
        self.assertEqual(_number_to_words(1000), "हज़ार")

    def test_direct_100000(self):
        self.assertEqual(_number_to_words(100000), "लाख")

    def test_direct_10000000(self):
        self.assertEqual(_number_to_words(10000000), "करोड़")

    def test_direct_10(self):
        self.assertEqual(_number_to_words(10), "दस")

    # branch 2: n < 100 (via _two_digit_to_words)
    def test_lt100_single_digit(self):
        self.assertEqual(_number_to_words(5), "पाँच")

    def test_lt100_ten(self):
        self.assertEqual(_number_to_words(50), "पचास")

    # branch 3: 100 ≤ n < 1000, h == 1 (uses literal "एक")
    def test_hundred_one(self):
        result = _number_to_words(101)
        self.assertIn("एक सौ", result)

    def test_hundred_fifty(self):
        result = _number_to_words(150)
        self.assertIn("एक सौ", result)
        self.assertIn("पचास", result)

    # branch 3: h > 1, rest == 0
    def test_two_hundred(self):
        self.assertEqual(_number_to_words(200), "दो सौ")

    def test_nine_hundred(self):
        self.assertEqual(_number_to_words(900), "नौ सौ")

    # branch 3: h > 1, rest != 0
    def test_two_fifty(self):
        result = _number_to_words(250)
        self.assertIn("दो सौ", result)
        self.assertIn("पचास", result)

    # branch 4: 1000 ≤ n < 100000, rest == 0
    def test_two_thousand(self):
        result = _number_to_words(2000)
        self.assertIn("हज़ार", result)

    # branch 4: rest != 0
    def test_1500(self):
        result = _number_to_words(1500)
        self.assertIn("हज़ार", result)
        self.assertIn("पाँच सौ", result)

    def test_5500(self):
        result = _number_to_words(5500)
        self.assertIn("हज़ार", result)

    # branch 5: n ≥ 100000 and not in _NUMBER_WORDS → digit-by-digit
    def test_200000_fallback(self):
        result = _number_to_words(200000)
        # 2,0,0,0,0,0 in Devanagari digit words
        self.assertIn("दो", result)
        self.assertIn("शून्य", result)


# ---------------------------------------------------------------------------
# _replace_numbers
# ---------------------------------------------------------------------------

class ReplaceNumbersTest(unittest.TestCase):
    """Year-form (4-digit 1900–2099) and cardinal branches."""

    def test_year_1947(self):
        result = _replace_numbers("सन् 1947 में")
        self.assertIn("उन्नीस", result)

    def test_year_2024(self):
        result = _replace_numbers("2024 में")
        self.assertIn("दो हज़ार", result)

    def test_cardinal_small(self):
        result = _replace_numbers("5 बच्चे")
        self.assertIn("पाँच", result)

    def test_cardinal_multi_digit(self):
        result = _replace_numbers("150 रुपये")
        self.assertIn("एक सौ", result)

    def test_four_digit_non_year(self):
        # 3000 is outside 1900–2099 → plain cardinal
        result = _replace_numbers("3000 किलोमीटर")
        self.assertIn("हज़ार", result)
        self.assertNotIn("दो हज़ार", result)

    def test_no_digits(self):
        text = "कोई संख्या नहीं"
        self.assertEqual(_replace_numbers(text), text)


# ---------------------------------------------------------------------------
# _replace_loanwords
# ---------------------------------------------------------------------------

class ReplaceLoanwordsTest(unittest.TestCase):
    """Mapped words replaced; unmapped text unchanged."""

    def test_google(self):
        self.assertIn("गूगल", _replace_loanwords("Google ने कहा"))

    def test_openai_before_ai(self):
        # "OpenAI" (longer key) must match before "AI" (shorter key)
        result = _replace_loanwords("OpenAI का AI उत्पाद")
        self.assertIn("ओपन एआई", result)
        self.assertIn("ए आई", result)

    def test_chatgpt(self):
        self.assertIn("चैट जीपीटी", _replace_loanwords("ChatGPT"))

    def test_youtube(self):
        self.assertIn("यूट्यूब", _replace_loanwords("YouTube"))

    def test_phone_loanword(self):
        self.assertIn("फोन", _replace_loanwords("phone"))

    def test_no_match_unchanged(self):
        original = "सामान्य हिंदी पाठ"
        self.assertEqual(_replace_loanwords(original), original)

    def test_acronym_gpu(self):
        self.assertIn("जी पी यू", _replace_loanwords("GPU"))


# ---------------------------------------------------------------------------
# _replace_punctuation
# ---------------------------------------------------------------------------

class ReplacePunctuationTest(unittest.TestCase):
    """Period → purna viram when Devanagari precedes; else unchanged."""

    def test_period_before_capital_devanagari_context(self):
        # `. ` before uppercase, Devanagari in window → purna viram
        text = "यह वाक्य है. अगला वाक्य"
        result = _replace_punctuation(text)
        self.assertIn("।", result)

    def test_period_before_devanagari_char(self):
        text = "यह पहला वाक्य. यह दूसरा"
        result = _replace_punctuation(text)
        self.assertIn("।", result)

    def test_period_at_end_devanagari_context(self):
        text = "यह अंतिम वाक्य है."
        result = _replace_punctuation(text)
        self.assertIn("।", result)

    def test_period_no_devanagari_unchanged(self):
        text = "Hello. World"
        result = _replace_punctuation(text)
        self.assertNotIn("।", result)

    def test_english_trailing_period_unchanged(self):
        text = "Hello."
        result = _replace_punctuation(text)
        self.assertNotIn("।", result)

    def test_no_period(self):
        text = "यह वाक्य है"
        result = _replace_punctuation(text)
        self.assertEqual(result, text)


# ---------------------------------------------------------------------------
# _collapse_whitespace
# ---------------------------------------------------------------------------

class CollapseWhitespaceTest(unittest.TestCase):
    def test_multiple_spaces(self):
        self.assertEqual(_collapse_whitespace("a  b   c"), "a b c")

    def test_newlines(self):
        self.assertEqual(_collapse_whitespace("a\nb\nc"), "a b c")

    def test_tabs(self):
        self.assertEqual(_collapse_whitespace("a\tb"), "a b")

    def test_leading_trailing(self):
        self.assertEqual(_collapse_whitespace("  hello  "), "hello")

    def test_already_clean(self):
        self.assertEqual(_collapse_whitespace("hello world"), "hello world")

    def test_empty(self):
        self.assertEqual(_collapse_whitespace(""), "")


# ---------------------------------------------------------------------------
# normalize_hindi_for_tts  (integration — exercises the full pipeline)
# ---------------------------------------------------------------------------

class NormalizeHindiForTtsTest(unittest.TestCase):

    def test_full_pipeline(self):
        text = "OpenAI ने 2023 में ChatGPT लॉन्च किया."
        result = normalize_hindi_for_tts(text)
        self.assertIn("ओपन एआई", result)
        self.assertIn("चैट जीपीटी", result)
        self.assertIn("दो हज़ार", result)  # 2023 as year form

    def test_empty_string(self):
        # Every sub-function must handle "" without crashing
        self.assertEqual(normalize_hindi_for_tts(""), "")

    def test_whitespace_collapsed(self):
        result = normalize_hindi_for_tts("यह   एक  वाक्य")
        self.assertNotIn("  ", result)

    def test_pure_hindi_unchanged(self):
        text = "यह शुद्ध हिंदी है"
        self.assertEqual(normalize_hindi_for_tts(text), text)

    def test_number_converted(self):
        result = normalize_hindi_for_tts("वहाँ 5 लोग थे")
        self.assertIn("पाँच", result)

    def test_purna_viram_applied(self):
        text = "यह वाक्य है. अगला वाक्य"
        result = normalize_hindi_for_tts(text)
        self.assertIn("।", result)


if __name__ == "__main__":
    unittest.main()
