"""Tests for hindi_normalize.py."""
from pipeline.text.hindi_normalize import normalize_hindi_for_tts


def test_devanagari_passthrough():
    text = "नमस्ते दुनिया"
    assert normalize_hindi_for_tts(text) == "नमस्ते दुनिया"


def test_year_2024_to_words():
    text = "वर्ष 2024 में कई घटनाएँ हुईं।"
    out = normalize_hindi_for_tts(text)
    assert "दो हज़ार" in out
    assert "2024" not in out


def test_year_1944_to_words():
    text = "नौ जून 1944 को मित्र राष्ट्रों ने हमला किया।"
    out = normalize_hindi_for_tts(text)
    assert "उन्नीस सौ" in out
    assert "1944" not in out


def test_simple_number_to_words():
    text = "आज मेरे पास 5 किताबें हैं।"
    out = normalize_hindi_for_tts(text)
    assert "पाँच" in out
    assert "5" not in out


def test_two_digit_number():
    text = "उसकी उम्र 25 साल है।"
    out = normalize_hindi_for_tts(text)
    assert "पच्चीस" in out


def test_loanword_openai():
    text = "OpenAI ने एक नया मॉडल जारी किया।"
    out = normalize_hindi_for_tts(text)
    assert "ओपन एआई" in out
    assert "OpenAI" not in out


def test_loanword_ai_word_boundary():
    """AI must match as a whole word, not inside other words."""
    text = "यह AI तकनीक बहुत अच्छी है।"
    out = normalize_hindi_for_tts(text)
    assert "ए आई" in out


def test_acronym_aita():
    text = "AITA पूछा गया कि क्या मैं गलत था।"
    out = normalize_hindi_for_tts(text)
    assert "एआईटीए" in out


def test_punctuation_period_becomes_purna_viram():
    text = "नमस्ते। यह अच्छा है. आप कैसे हैं?"
    out = normalize_hindi_for_tts(text)
    # Period after Devanagari context → purna viram
    # Question mark and existing purna viram preserved
    assert "अच्छा है।" in out or "अच्छा है ।" in out


def test_punctuation_does_not_touch_english_only():
    """If sentence is all-English, leave the period alone."""
    text = "Hello. This is fine."
    out = normalize_hindi_for_tts(text)
    # No Devanagari context → period preserved
    assert "." in out


def test_idempotent_double_apply():
    """Applying normalisation twice should give same result as once."""
    text = "वर्ष 2024 में OpenAI ने AI मॉडल जारी किया."
    once = normalize_hindi_for_tts(text)
    twice = normalize_hindi_for_tts(once)
    assert once == twice


def test_complex_combined():
    text = "OpenAI ने 2024 में 50 नए AI मॉडल जारी किए।"
    out = normalize_hindi_for_tts(text)
    assert "ओपन एआई" in out
    assert "दो हज़ार चौबीस" in out
    assert "पचास" in out
    assert "ए आई" in out
    assert "OpenAI" not in out
    assert "2024" not in out
    assert "50" not in out
    assert "AI " not in out and " AI " not in out


def test_collapses_whitespace():
    text = "नमस्ते   दुनिया\n\nकैसे हो"
    out = normalize_hindi_for_tts(text)
    assert "  " not in out
    assert "\n" not in out


def test_handles_empty():
    assert normalize_hindi_for_tts("") == ""
    assert normalize_hindi_for_tts("   ") == ""
