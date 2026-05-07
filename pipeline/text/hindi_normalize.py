"""Hindi text normalizer for TTS pipelines.

TTS models (IndicF5, IndicParler, Higgs, Kokoro hf_alpha) consistently
trip on un-normalized Hindi text. This normalizer applies the
preprocessing that gives the biggest free quality win:

  1. **Devanagari digits** — Western digits (123) → Devanagari words
     (एक सौ तेईस). TTS otherwise reads them as English digit names.
  2. **Punctuation** — Western period (.) → Hindi purna viram (।).
     TTS prosody is tuned for purna viram pauses, not Western period.
     Comma, question, exclamation kept as-is (Hindi accepts both).
  3. **English loanwords** — common ASCII words inside Devanagari text
     get transliterated to Devanagari so phoneme mapping works:
       OpenAI → ओपन एआई
       AITA   → एआईटीए
     Falls back to leaving the original word if no mapping (avoids
     making things worse than they are).
  4. **Acronym expansion** — common acronyms expanded to readable form.
  5. **Whitespace** — collapse multiple spaces, normalize line breaks.

Designed to be a pure-Python, dependency-free preprocessing step run
BEFORE any /synth call.

Usage:
    from pipeline.text.hindi_normalize import normalize_hindi_for_tts
    text = normalize_hindi_for_tts(raw_text)
    # then pass `text` to the TTS service
"""
from __future__ import annotations

import re

# Devanagari digit → word mapping (0-9)
_DIGIT_WORDS = {
    "0": "शून्य", "1": "एक", "2": "दो", "3": "तीन", "4": "चार",
    "5": "पाँच", "6": "छह", "7": "सात", "8": "आठ", "9": "नौ",
}

# Two-digit and select multi-digit common words
_NUMBER_WORDS = {
    10: "दस", 11: "ग्यारह", 12: "बारह", 13: "तेरह", 14: "चौदह",
    15: "पंद्रह", 16: "सोलह", 17: "सत्रह", 18: "अठारह", 19: "उन्नीस",
    20: "बीस", 21: "इक्कीस", 22: "बाईस", 23: "तेईस", 24: "चौबीस",
    25: "पच्चीस", 26: "छब्बीस", 27: "सत्ताईस", 28: "अट्ठाईस", 29: "उनतीस",
    30: "तीस", 31: "इकतीस", 32: "बत्तीस", 33: "तैंतीस", 34: "चौंतीस",
    35: "पैंतीस", 36: "छत्तीस", 37: "सैंतीस", 38: "अड़तीस", 39: "उनतालीस",
    40: "चालीस", 41: "इकतालीस", 42: "बयालीस", 43: "तैंतालीस", 44: "चौवालीस",
    45: "पैंतालीस", 46: "छियालीस", 47: "सैंतालीस", 48: "अड़तालीस", 49: "उनचास",
    50: "पचास", 60: "साठ", 70: "सत्तर", 80: "अस्सी", 90: "नब्बे",
    100: "सौ", 1000: "हज़ार", 100000: "लाख", 10000000: "करोड़",
}

# Common English loanwords that appear in Hindi tech / news content
_LOANWORD_TRANSLITERATION = {
    # Tech brands
    "OpenAI": "ओपन एआई",
    "ChatGPT": "चैट जीपीटी",
    "Google": "गूगल",
    "Microsoft": "माइक्रोसॉफ्ट",
    "Apple": "एप्पल",
    "Meta": "मेटा",
    "Facebook": "फेसबुक",
    "YouTube": "यूट्यूब",
    "Twitter": "ट्विटर",
    "Anthropic": "एंथ्रोपिक",
    "Claude": "क्लॉड",
    # Common acronyms
    "AI": "ए आई",
    "GPU": "जी पी यू",
    "API": "ए पी आई",
    "URL": "यू आर एल",
    "GPT": "जी पी टी",
    "GDP": "जी डी पी",
    "CEO": "सी ई ओ",
    "USA": "यू एस ए",
    "UK": "यू के",
    "EU": "ई यू",
    "AITA": "एआईटीए",
    "WIBTA": "विबीटीए",
    # Common loanwords
    "internet": "इंटरनेट",
    "phone": "फोन",
    "email": "ईमेल",
    "video": "वीडियो",
    "online": "ऑनलाइन",
    "computer": "कंप्यूटर",
}


def _two_digit_to_words(n: int) -> str:
    """Convert 0-99 to Hindi words."""
    if n in _NUMBER_WORDS:
        return _NUMBER_WORDS[n]
    if n < 10:
        return _DIGIT_WORDS[str(n)]
    tens = (n // 10) * 10
    ones = n % 10
    if tens in _NUMBER_WORDS and 1 <= ones <= 9:
        return f"{_NUMBER_WORDS[tens]} {_DIGIT_WORDS[str(ones)]}"
    return " ".join(_DIGIT_WORDS[d] for d in str(n))


def _year_to_words(n: int) -> str:
    """Convert a 4-digit year (1900-2099) to natural Hindi.

    1944 → "उन्नीस सौ चौवालीस"
    2024 → "दो हज़ार चौबीस"
    """
    if 1900 <= n <= 1999:
        century = (n // 100)  # 19
        rest = n % 100  # 44
        return f"{_two_digit_to_words(century)} सौ {_two_digit_to_words(rest)}"
    if 2000 <= n <= 2099:
        rest = n % 100
        if rest == 0:
            return "दो हज़ार"
        return f"दो हज़ार {_two_digit_to_words(rest)}"
    return _number_to_words(n)


def _number_to_words(n: int) -> str:
    """Convert any non-negative integer to Hindi words. Keeps it simple
    for numbers >= 100; uses digit-by-digit for unmapped large numbers."""
    if n in _NUMBER_WORDS:
        return _NUMBER_WORDS[n]
    if n < 100:
        return _two_digit_to_words(n)
    if n < 1000:
        h = n // 100
        rest = n % 100
        out = f"{_DIGIT_WORDS[str(h)] if h > 1 else 'एक'} सौ"
        if rest:
            out += " " + _two_digit_to_words(rest)
        return out
    if n < 100000:
        thou = n // 1000
        rest = n % 1000
        out = f"{_number_to_words(thou)} हज़ार"
        if rest:
            out += " " + _number_to_words(rest)
        return out
    return " ".join(_DIGIT_WORDS[d] for d in str(n))


_NUMBER_RE = re.compile(r"\b(\d{1,7})\b")


def _replace_numbers(text: str) -> str:
    def repl(m: re.Match) -> str:
        n = int(m.group(1))
        if 1900 <= n <= 2099 and len(m.group(1)) == 4:
            return _year_to_words(n)
        return _number_to_words(n)
    return _NUMBER_RE.sub(repl, text)


def _replace_loanwords(text: str) -> str:
    # Sort by length descending so "OpenAI" matches before "AI"
    for src in sorted(_LOANWORD_TRANSLITERATION, key=len, reverse=True):
        pattern = r"\b" + re.escape(src) + r"\b"
        text = re.sub(pattern, _LOANWORD_TRANSLITERATION[src], text)
    return text


_DEVANAGARI_BLOCK_RE = re.compile(r"[\u0900-\u097F]")


def _replace_punctuation(text: str) -> str:
    """Convert Western sentence terminators to Hindi purna viram (।)
    when the surrounding context is Devanagari. Leave commas / question
    marks / exclamations alone — those are universally accepted."""
    def repl(m: re.Match) -> str:
        start = max(0, m.start() - 30)
        window = text[start:m.start()]
        if _DEVANAGARI_BLOCK_RE.search(window):
            return "।"
        return m.group(0)
    text = re.sub(r"\.(?=\s+[A-Z\u0900-\u097F])|\.(?=\s*$)", repl, text)
    return text


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_hindi_for_tts(text: str) -> str:
    """Apply the full Hindi-friendly TTS normalisation pipeline.

    Pure function; safe to call on already-normalised text (idempotent
    for the punctuation + whitespace passes; loanword + number passes
    are nearly idempotent since their replacements don't re-trigger the
    patterns).
    """
    text = _replace_loanwords(text)
    text = _replace_numbers(text)
    text = _replace_punctuation(text)
    text = _collapse_whitespace(text)
    return text


__all__ = ["normalize_hindi_for_tts"]
