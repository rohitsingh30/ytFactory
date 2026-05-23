"""Pre-TTS text normalisation.

Kokoro (and most neural TTS) reads "$2000" as "two zero zero zero" because
it has no g2p rule for the dollar sign and falls back to digit-by-digit on
unrecognised tokens. We pre-expand currency, K/M suffixes, and bare integers
to spelled-out words so the audio sounds natural — and so Whisper hears
the same words the source narration aligns against (no caption/audio drift).
Applies to every TTS provider — all of which suffer the same numeral
pronunciation problem to varying degrees.

This module is **import-safe** — no torch / kokoro / soundfile dependencies.
``pipeline/audio.py`` re-exports ``normalize_for_tts`` and
``apply_pronunciation_overrides`` from here.
"""
from __future__ import annotations

import re as _re


_ONES = ("", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_TEENS = ("ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
          "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty",
         "sixty", "seventy", "eighty", "ninety")


def _int_to_words(n: int) -> str:
    """Spell out a non-negative integer up to 999,999,999 as English words."""
    if n == 0:
        return "zero"
    if n < 0:
        return "negative " + _int_to_words(-n)
    if n < 10:
        return _ONES[n]
    if n < 20:
        return _TEENS[n - 10]
    if n < 100:
        rest = n % 10
        return _TENS[n // 10] + ("" if rest == 0 else "-" + _ONES[rest])
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + (" " + _int_to_words(rest) if rest else "")
    if n < 1_000_000:
        rest = n % 1000
        return _int_to_words(n // 1000) + " thousand" + (" " + _int_to_words(rest) if rest else "")
    if n < 1_000_000_000:
        rest = n % 1_000_000
        return _int_to_words(n // 1_000_000) + " million" + (" " + _int_to_words(rest) if rest else "")
    return str(n)


def _expand_currency(match: _re.Match) -> str:
    """Expand $XXX / $X,XXX[.YY] / $XK / $XM to spoken words."""
    raw = match.group(1).replace(",", "")
    suffix = (match.group(2) or "").lower()
    try:
        if suffix == "k":
            n = int(float(raw) * 1000)
            return f"{_int_to_words(n)} dollars"
        if suffix == "m":
            n = int(float(raw) * 1_000_000)
            return f"{_int_to_words(n)} dollars"
        if "." in raw:
            whole, frac = raw.split(".", 1)
            whole_n = int(whole) if whole else 0
            frac_n = int(frac.ljust(2, "0")[:2])  # treat as cents
            words = _int_to_words(whole_n) + " dollars"
            if frac_n:
                words += " and " + _int_to_words(frac_n) + " cents"
            return words
        return _int_to_words(int(raw)) + " dollars"
    except (ValueError, OverflowError):
        return match.group(0)


# NOTE: _ONES and _TENS are intentionally re-bound below to a 0-indexed
# variant covering 0..19 / 0..9. _two_digits_to_words and _expand_year
# (added later) rely on these wider tables. _int_to_words above runs
# at module import time first so its capture of the original tuples
# happens before the rebind — its semantics are unaffected because the
# new _ONES still has "one"=index 1, "two"=index 2, etc. The only
# observable difference would be a hypothetical caller passing 0 to
# _int_to_words AFTER the rebind expecting "" — but it's guarded by the
# `if n == 0: return "zero"` early-return.
_ONES = (
    "zero", "one", "two", "three", "four",
    "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen",
    "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS = (
    "", "", "twenty", "thirty", "forty",
    "fifty", "sixty", "seventy", "eighty", "ninety",
)


def _two_digits_to_words(n: int) -> str:
    """0..99 → spoken English. Used by _expand_year for the second half."""
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    if ones == 0:
        return _TENS[tens]
    return f"{_TENS[tens]}-{_ONES[ones]}"


def _expand_year(n: int) -> str:
    """Year-style expansion: 1999 → "nineteen ninety-nine", 2014 → "twenty
    fourteen", 2009 → "two thousand nine". Critic 2026-05-03 (top3-stoppage-
    goals v1): every year was mangled by the generic _int_to_words path —
    "1999" came out as "one thousand nine hundred ninety-nine".
    """
    century, rest = divmod(n, 100)
    if 1900 <= n <= 2099:
        if 2000 <= n <= 2009:
            # 2000 → "two thousand"; 2001-2009 → "two thousand X"
            return "two thousand" if rest == 0 else f"two thousand {_ONES[rest]}"
        # 1900-1999 + 2010-2099: <century> <rest two-digit>
        # 1999 → "nineteen ninety-nine", 2014 → "twenty fourteen"
        century_words = _two_digits_to_words(century)
        if rest == 0:
            # 1900 → "nineteen hundred", 2100 → "twenty-one hundred"
            return f"{century_words} hundred"
        return f"{century_words} {_two_digits_to_words(rest)}"
    return _int_to_words(n)


def _expand_bare_int(match: _re.Match) -> str:
    """Expand a standalone integer (1–999,999,999) to words. Kept conservative
    via word boundaries so phone numbers / hyphenated digit runs aren't
    mangled (e.g. ``555-1234`` stays as-is).

    Years 1900-2099 take a special spoken form via _expand_year (so
    "1999" reads as "nineteen ninety-nine" rather than the cardinal
    "one thousand nine hundred ninety-nine"). The detection is purely
    by numeric range — football narration cites years that fall in
    this band; a literal cardinal "1999 viewers" in another niche
    would also expand to year-style, but that's a rare edge case
    versus the constant-cost win on sports / TIH / wiki narration.
    """
    s = match.group(0)
    # Strip comma-grouped thousands ("2,500" → "2500") before int().
    # _RE_INTEGER captures comma-separated forms as a single token
    # (class-of-bug fix 2026-05-03 — see regex docstring).
    digits = s.replace(",", "")
    try:
        n = int(digits)
    except ValueError:
        return s
    if n > 999_999_999:
        return s
    # Year rule applies only to bare 4-digit forms (no commas), so
    # "1944" becomes "nineteen forty-four" but "1,944" (rare) becomes
    # the cardinal "one thousand nine hundred forty-four".
    if 1900 <= n <= 2099 and len(s) == 4:
        return _expand_year(n)
    return _int_to_words(n)


# AITA-class acronyms expanded to their natural English phrases for TTS.
# Earlier approach (letter-spacing → "A I T A") technically worked but
# produced unnatural narration; the correct human-narrator behaviour is
# to SAY THE WORDS the acronym stands for. Audio critic finding 2026-05
# confirmed: real listeners hear "Aira"/"Zira"/"Antigua" when Kokoro
# phoneticises the bare acronyms, and even letter-spelled they sound
# robotic. Expansion is the fix.
#
# Captions (derived downstream from Whisper) will pick up the spoken
# phrase, which is correct — that IS what was said. The visual closer
# panel renders the original acronyms separately via render_closer_panel
# from cfg["closer_format"], so the panel stays as "LIKE if YTA /
# COMMENT if NTA / AITA?" regardless of what the audio path does.
_ACRONYM_PHRASES: dict[str, str] = {
    # AITA-class verdict acronyms (AITA / WIBTA / YTA / NTA / NAH / ESH)
    # are DELIBERATELY ABSENT from this map. User feedback 2026-05-03:
    # the channel should never PRONOUNCE these acronyms — neither
    # letter-spelled ("A I T A") nor expanded to natural English
    # ("am I the a hole"). Both readings sound off-tone for the
    # channels going forward.
    #
    # Two layers enforce this:
    #   (1) LLM-level (pipeline/rewrite.py + pipeline/prompts.py) —
    #       the rewrite prompt forbids the LLM from authoring narration
    #       that contains these tokens; the hook uses "Am I wrong for…"
    #       framing instead; the spoken closer drops the acronym line
    #       entirely. All NEW renders are clean by construction.
    #   (2) Audio-level (this file) — _strip_verdict_acronym_sentences
    #       below removes any sentence still containing these tokens
    #       before TTS, as a defensive class-of-bug safety net for
    #       re-renders of pre-2026-05-03 scripts.
    #
    # The visual closer panel STILL renders "LIKE if YTA / COMMENT if NTA"
    # from cfg["closer_format"] via compose.render_closer_panel — that's
    # the engagement ask, and it lives entirely in pixels, never audio.
    # Reddit family-relationship abbreviations.
    "MIL":   "mother in law",
    "FIL":   "father in law",
    "SIL":   "sister in law",
    "BIL":   "brother in law",
    "DIL":   "daughter in law",
    "SO":    "significant other",
    # Reddit storytelling-meta abbreviations.
    "OOP":   "the original poster",
    "OP":    "OP",          # commonly read as letters; leave as-is
    "NC":    "no contact",
    "VLC":   "very low contact",
    "INFO":  "info",         # short enough; word form
    "TIL":   "today I learned",
    "TIFU":  "today I screwed up",
    # General initialisms that read fine as letters in normal speech.
    # Mapping to themselves keeps them in the recognised set so the
    # generic ALL-CAPS lowercase rule doesn't mangle them.
    "TV":    "T V",
    "DJ":    "D J",
    "ID":    "I D",
    "PR":    "P R",
    "HR":    "H R",
    "IT":    "I T",
    "AI":    "A I",
    "PC":    "P C",
    "DVD":   "D V D",
    "CEO":   "C E O",
    "CFO":   "C F O",
    "CTO":   "C T O",
    "VP":    "V P",
    "ER":    "E R",
    "ICU":   "I C U",
    "NICU":  "N I C U",
    "OBGYN": "O B G Y N",
    "USA":   "U S A",
    "UK":    "U K",
    "EU":    "E U",
    "FBI":   "F B I",
    "CIA":   "C I A",
    "IRS":   "I R S",
    "DMV":   "D M V",
    "ATM":   "A T M",
    "PIN":   "P I N",
    "GPS":   "G P S",
    "USB":   "U S B",
    "WIFI":  "WiFi",         # word, not letters
    "OK":    "okay",
}

# Match all-caps word-tokens of length >= 2 (single letters like "I"
# don't count). Apostrophes and hyphens kept inside the token.
_RE_ALLCAPS_WORD = _re.compile(r"\b[A-Z][A-Z'\-]{1,}\b")

# AITA-domain acronyms that are SAFE to expand case-insensitively.
# These are unambiguous in narration context — there is no English word
# "aita" or "mil" or "yta" that we'd be clobbering. General initialisms
# from _ACRONYM_PHRASES (SO, IT, ER, TV, ID, …) are explicitly excluded
# because they collide with common English words: "So I refused" should
# stay "so", not "significant other"; "It was bad" should stay "it",
# not "I T". Those still expand on the all-caps path (uppercase-only).
#
# AITA-class VERDICT acronyms (AITA, WIBTA, YTA, NTA, NAH, ESH) are
# deliberately NOT in this list — they're stripped (whole containing
# sentence removed) by _strip_verdict_acronym_sentences before this
# expansion path ever runs. See the _ACRONYM_PHRASES comment block.
_AITA_ACRONYMS_CI: tuple[str, ...] = (
    "MIL", "FIL", "SIL", "BIL", "DIL",
    "OOP", "VLC", "TIFU", "TIL",
    # Note deliberately omitted: SO, OP, NC, INFO, ID, IT, AI, ER, etc.
    # — they all collide with common English words at lowercase.
)
_RE_ANY_CASE_ACRONYM = _re.compile(
    r"\b(" + "|".join(sorted(_AITA_ACRONYMS_CI, key=len, reverse=True)) + r")\b",
    flags=_re.IGNORECASE,
)


def _expand_any_case_acronym(match: _re.Match) -> str:
    """Look up the matched AITA-domain token (case-insensitively) in
    ``_ACRONYM_PHRASES`` and substitute its natural phrase.

    Only invoked for tokens in ``_AITA_ACRONYMS_CI``. Other initialisms
    (TV, ER, USA, …) take the uppercase-only path via the all-caps
    rule so we don't accidentally turn "so" into "significant other"
    or "it" into "I T".
    """
    tok_upper = match.group(1).upper()
    return _ACRONYM_PHRASES.get(tok_upper, match.group(0))

# Currency: optional $, optional comma-separated thousands, optional decimal,
# optional K/M suffix. Matches "$2000", "$2,000", "$4.50", "$4K", "$2.5M".
# `(?:\s*([KkMm]))?` keeps the optional whitespace + K/M tied together
# atomically — if K/M is absent, the whole group bails and the trailing
# space is preserved (without this, `\s*([KkMm])?` greedily ate the space
# after "$4,200" and produced "four thousand two hundred dollarsfrom").
_RE_CURRENCY = _re.compile(r"\$([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)(?:\s*([KkMm]))?\b")
# Bare integer with word-boundary on each side, supporting comma-separated
# thousands (e.g. "2,500", "2,937", "1,000,000"). The negative-lookbehind
# for `-` and `:` sidesteps phone numbers (555-1234) and timestamps (10:30);
# the negative-lookahead for `:` and digit-hyphen prevents matching the
# left half of those patterns. Class-of-bug fix 2026-05-03 — without the
# comma-grouping branch, "2,500" matched twice (as "2" and "500") and
# the TTS narrated "two-comma-five hundred". `_expand_bare_int` strips
# the commas before int() conversion.
_RE_INTEGER = _re.compile(
    r"(?<![-:\d])\b(\d{1,3}(?:,\d{3})+|\d{1,9})\b(?![-:.\d])"
)

# AD / BC year markers. TTS otherwise reads "AD" as the word "ad"
# (advertisement) and "BC" as "bee-cee", both broken. We spell them as
# letter pairs so neural TTS produces "A D" / "B C". Both pre-year
# ("AD 44") and post-year ("79 AD") forms are supported. Case-insensitive.
_RE_AD_BC_YEAR = _re.compile(r"\b([Aa][Dd]|[Bb][Cc])\s+(\d{1,4})\b")
_RE_AD_BC_POST_YEAR = _re.compile(r"\b(\d{1,4})\s+([Aa][Dd]|[Bb][Cc])\b")


def _spell_ad_bc(m: "_re.Match[str]") -> str:
    """Spell ``AD``/``BC`` as letter pairs; year is preserved by the caller
    via group(2). Returns just the letter-pair so callers (or tests)
    can compose with the year as needed.
    """
    token = m.group(1).upper()
    return " ".join(token)


def _spell_ad_bc_post(m: "_re.Match[str]") -> str:
    """Same idea, post-year form ("79 AD" → "79 A D")."""
    year = m.group(1)
    token = m.group(2).upper()
    return f"{year} {' '.join(token)}"


# Day-month natural-speech rewrite. Without this, "15 September" comes out
# of TTS as "fifteen September" — robotic. Real human narrators say
# "the fifteenth of September" or "September fifteenth". We rewrite to
# the British "the Nth of Month" form (better fit for the documentary /
# historyrecapped tone). Applied BEFORE _RE_INTEGER so the day number isn't
# stripped to a bare cardinal first. Class-of-bug fix 2026-05-03.
_MONTH_NAMES = (
    "January|February|March|April|May|June|July|"
    "August|September|October|November|December"
)
_RE_DAY_MONTH = _re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES})\b",
    flags=_re.IGNORECASE,
)
# Reverse form too: "September 15" / "September 15th" → "September the fifteenth".
_RE_MONTH_DAY = _re.compile(
    rf"\b({_MONTH_NAMES})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b",
    flags=_re.IGNORECASE,
)

_ORDINAL_TENS = {
    20: "twentieth", 30: "thirtieth", 40: "fortieth", 50: "fiftieth",
    60: "sixtieth", 70: "seventieth", 80: "eightieth", 90: "ninetieth",
}
_ORDINAL_ONES = {
    0: "",       1: "first",    2: "second",   3: "third",   4: "fourth",
    5: "fifth",  6: "sixth",    7: "seventh",  8: "eighth",  9: "ninth",
    10: "tenth", 11: "eleventh", 12: "twelfth", 13: "thirteenth",
    14: "fourteenth", 15: "fifteenth", 16: "sixteenth", 17: "seventeenth",
    18: "eighteenth", 19: "nineteenth",
}


def _ordinal_to_words(n: int) -> str:
    """Return ``n`` as an English ordinal phrase. Used for date formatting."""
    if n < 1 or n > 99:
        return _int_to_words(n) + "th"
    if n in _ORDINAL_ONES:
        return _ORDINAL_ONES[n]
    if n in _ORDINAL_TENS:
        return _ORDINAL_TENS[n]
    tens, ones = divmod(n, 10)
    cardinal_tens = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty",
                     6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}[tens]
    return f"{cardinal_tens}-{_ORDINAL_ONES[ones]}"


def _expand_day_month(match: _re.Match) -> str:
    day = int(match.group(1))
    month = match.group(2)
    return f"the {_ordinal_to_words(day)} of {month}"


def _expand_month_day(match: _re.Match) -> str:
    month = match.group(1)
    day = int(match.group(2))
    return f"{month} the {_ordinal_to_words(day)}"


def _lowercase_emphatic_caps(match: _re.Match) -> str:
    """Normalize an all-caps token for TTS.

    Three outcomes:
    - Known acronym in ``_ACRONYM_PHRASES`` → replace with its mapped
      phrase. AITA-class acronyms expand to natural English ("AITA" →
      "am I the asshole"); general initialisms that read as letters
      (TV, USA, ER) map to letter-spaced form ("T V"). This is what
      a human narrator does — neither letter-spelling nor phonetic
      guessing.
    - Anything else (emphasis caps from the rewrite prompt, e.g.
      "OUT", "AGAIN") → lowercase, so it reads as the normal word
      instead of getting letter-spelled. Captions are derived
      downstream from Whisper, so the visual emphasis would have
      been lost anyway.
    - Single-letter tokens (I, A) never match the regex and pass
      through untouched.
    """
    tok = match.group(0)
    phrase = _ACRONYM_PHRASES.get(tok)
    if phrase is not None:
        return phrase
    return tok.lower()


def apply_pronunciation_overrides(
    text: str,
    pronunciation_dict: dict | None,
) -> str:
    """Replace proper nouns with phonetic respellings before TTS.

    ``pronunciation_dict`` is ``{name_or_alias: phonetic_respelling}``,
    typically supplied by ``pipeline/wiki_research.py``'s dossier
    (``dossier.pronunciation_dict``). Keys are matched case-insensitively
    on word boundaries and the **original token's leading capitalisation
    pattern** is preserved on the substituted respelling — so:

      "Aguero scores!"  → "Ah-GWAIR-oh scores!"
      "AGUEROOOOO!"     → "AH-GWAIR-OH!"   (still in caps, emphatic)

    Order: longer keys first, so multi-word names ("Kun Aguero") match
    before the bare surname does and we don't double-replace.

    Returns ``text`` unchanged if the dict is empty / None. Idempotent
    on its OWN output is not guaranteed — re-applying on already-
    respelled text can re-match the respelling. Callers should apply
    once, between ``normalize_for_tts`` and ``synthesize``.
    """
    if not pronunciation_dict or not text:
        return text

    # Sort by descending key length so multi-word forms match before
    # their substring single-word forms.
    import re  # local import — module already aliases re elsewhere
    items = sorted(pronunciation_dict.items(), key=lambda kv: -len(kv[0]))
    out = text
    for name, phonetic in items:
        if not name or not phonetic:
            continue
        # Word-boundary regex, case-insensitive. Don't escape the
        # phonetic respelling — it already contains hyphens / capitals.
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)

        def _sub(m: re.Match, _phon=phonetic) -> str:
            original = m.group(0)
            if original.isupper():
                return _phon.upper()
            if original[:1].isupper():
                # Preserve sentence-case (capitalise first letter, leave
                # the embedded SMALL-CAPS stress markers in the respelling
                # alone — they're meaningful for emphasis).
                return _phon[:1].upper() + _phon[1:] if _phon else _phon
            return _phon

        out = pattern.sub(_sub, out)
    return out


# Profanity sanitisation for YouTube monetisation. The narrated word
# "asshole" gets the slur-flagging by YouTube's auto-classifier, which
# can yellow-icon AITA shorts. Replacing with "a hole" pre-TTS makes
# Kokoro speak it as two phonemes ("ay-hole"), Whisper transcribes the
# two words back, and captions match audio. User feedback 2026-05-03.
# Word-bounded so "asshole" inside larger compound tokens (rare) won't
# get split mid-word; capture preserves the trailing punctuation.
_RE_PROFANITY_ASSHOLE = _re.compile(
    r"\b(asshole|assholes)\b",
    flags=_re.IGNORECASE,
)


# AITA-class verdict acronym stripper. Defensive class-of-bug safety
# net for re-renders of older scripts (pre-2026-05-03) that still
# contain AITA/WIBTA/YTA/NTA/NAH/ESH in their narration. New renders
# are kept clean at the LLM authoring layer (pipeline/rewrite.py),
# but if any of these tokens slip through we strip the WHOLE sentence
# containing them before TTS so the audio never says them. The
# visual closer panel still renders the engagement ask from
# cfg["closer_format"] independently in compose.py, so the panel
# survives stripping. See user feedback 2026-05-03 — the channel
# decision is "never pronounce these acronyms".
_RE_VERDICT_ACRONYM = _re.compile(
    r"\b(?:AITA|WIBTA|YTA|NTA|NAH|ESH)\b",
    flags=_re.IGNORECASE,
)


def _strip_verdict_acronym_sentences(text: str) -> str:
    """Remove sentences containing AITA-class verdict acronyms.

    Whole-sentence removal (not just the token) because excising the
    bare acronym leaves grammatically-broken fragments — "AITA for
    refusing to host?" with the acronym deleted becomes " for
    refusing to host?", which Kokoro then reads as a half-question.
    Dropping the entire sentence produces clean output: the next
    sentence becomes the spoken hook, and downstream Whisper alignment
    sees what was actually said.

    Sentence boundary regex matches the same set as ``_split_into_sentences``
    in ``pipeline.tts.kokoro`` (.!?।॥ followed by whitespace) so the
    audio path's stripping aligns with how _synth_kokoro will chunk
    afterwards.
    """
    if not text or not _RE_VERDICT_ACRONYM.search(text):
        return text
    parts = _re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [p for p in parts if not _RE_VERDICT_ACRONYM.search(p)]
    if not kept:
        # Pathological case: every sentence had a verdict acronym.
        # Returning empty would crash downstream; return original so
        # the caller at least gets the legacy "letter-spelled" reading
        # rather than a hard failure. Should never trigger on
        # well-formed AITA narrations (only the closer line has them).
        return text
    return " ".join(kept)


def _sub_profanity_asshole(m: "_re.Match[str]") -> str:
    raw = m.group(1)
    is_plural = raw.lower().endswith("s")
    repl = "a holes" if is_plural else "a hole"
    # Preserve case bucket: ALL-CAPS → "A HOLE", Title → "A hole",
    # otherwise lower → "a hole".
    if raw.isupper():
        return repl.upper()
    if raw[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


# ---- Hindi tatsama + numeral respelling -----------------------------------
#
# Class-of-bug fix per critique 2026-05-03 (data/critiques/abhimanyu-chakravyuh/).
# Kokoro's Hindi voices (hf_alpha, hf_beta, hm_omega, hm_psi) systematically
# break Devanagari conjuncts (द्ध, क्ष, ज्ञ, र्भ) and re-segment proper
# nouns at wrong syllable boundaries. The TTS path replaces these with
# phonetic respellings; captions / source text continue to see canonical
# Devanagari (the substitution happens inside normalize_for_tts only).
#
# Add new entries here as the audio critic flags new mispronunciations
# (memory: feedback_pronunciation_pretts.md is the parent principle).

_HINDI_TATSAMA_RESPELLINGS: dict[str, str] = {
    # Mahabharat dramatis personae — hyphen forces syllable break
    # (per critique 2026-05-03 v2). Bare अभीमन्यू still re-segmented
    # at the wrong boundary; the hyphen survives Kokoro's phoneme
    # pass and prevents म्न्यु cluster collapse.
    "अभिमन्यु":   "अभि-मन्यु",
    "अर्जुन":     "अरजुन",
    "द्रोणाचार्य": "द्रोणाचारीय",
    # Place names
    "कुरुक्षेत्र": "कुरुक्शेत्र",
    "हस्तिनापुर": "हस्तीनापुर",
    # Tatsama nouns + conjunct workarounds
    "ज्ञान":       "ग्यान",     # accept colloquial gyaan; document
    "योद्धा":      "योद-धा",    # द्ध gemination preserved via hyphen
    "योद्धाओं":    "योद-धाओं",
    "महारथियों":  "महा-रथियों", # र्थ → र्त collapse fixed
    "गर्भ":        "गर्-भ",     # र्भ → र्ब collapse fixed
    "बाण":         "बाण्",      # final ण halant preserves the nasal stop
    "धनुष":        "धनुष्",     # final ष survives
    # Nasalisation losses (anusvara ं + chandrabindu ँ silently dropped)
    "साँस":        "सान्स",
    "तेरहवाँ":     "तेरहवान",
    # YouTube CTA tokens — Hindi-transliterated English breaks on hm_psi
    "सब्सक्राइब":  "सब-स्क्राइब",
    "कमेंट":       "कमेन्ट",
    "पसंद":        "पसन्द",
}

# Hindi numerals collide acoustically with common postpositions/verbs:
#   सात (seven) ↔ साथ (with)
#   सोलह (sixteen) — Kokoro inserts a schwa, lands as "soleh" (fuzzy).
# For collision-prone tokens, respell with explicit vowels so Kokoro
# emits the numeral, not the homophone.
_HINDI_NUMERAL_RESPELLINGS: dict[str, str] = {
    "सात":   "साअत",
    "सोलह":  "सोलाह",
}

_DEVANAGARI_PROBE = _re.compile(r"[ऀ-ॿ]")


def _apply_hindi_respellings(text: str) -> str:
    """Apply Hindi tatsama + numeral respellings.

    Cheap Devanagari-character probe so non-Hindi text is a no-op.
    Word boundaries (``\\b``) don't behave on Indic scripts, so we use
    plain substring replacement after sorting keys by descending
    length to avoid prefix collisions (e.g. "महारथियों" before
    "महारथी" if the latter is ever added).
    """
    if not text or not _DEVANAGARI_PROBE.search(text):
        return text
    out = text
    items = sorted(
        list(_HINDI_TATSAMA_RESPELLINGS.items())
        + list(_HINDI_NUMERAL_RESPELLINGS.items()),
        key=lambda kv: -len(kv[0]),
    )
    for src, dst in items:
        out = out.replace(src, dst)
    return out


def normalize_for_tts(text: str, *, skip_hindi_respellings: bool = False) -> str:
    """Make ``text`` pronounceable by neural TTS.

    Seven normalisations, applied in order:
    1. AITA-class verdict acronyms (AITA / WIBTA / YTA / NTA / NAH / ESH)
       — entire containing sentence STRIPPED. User feedback 2026-05-03:
       the channel must never pronounce these acronyms. Runs FIRST so
       the downstream all-caps + acronym passes never see them. The
       visual closer panel still renders the engagement ask from
       cfg["closer_format"] independently. See _strip_verdict_acronym_sentences.
    2. Currency → English words ($2000 → "two thousand dollars")
    3. AD/BC year markers → letter-spelled tokens ("AD 44" → "A D 44")
       so neural TTS doesn't read "AD" as "ad" or "BC" as "bee-cee".
    4. Bare integers → English words (60 → "sixty")
    5. Case-insensitive Reddit-class acronyms (MIL/FIL/SIL/BIL/DIL/OOP/
       VLC/TIFU/TIL) → their natural phrases. AITA-class verdict
       acronyms are NOT in this list anymore — they were stripped by
       step 1 above.
    6. Standalone "asshole" / "assholes" → "a hole" / "a holes"
       (YouTube monetisation sanitisation; see _RE_PROFANITY_ASSHOLE).
    7. Remaining all-caps tokens → lowercase (so emphasis-CAPS don't
       read as letter-spelled acronyms).
    8. Hindi tatsama + numeral respellings — only applied when text
       contains Devanagari **and** ``skip_hindi_respellings`` is False
       (default). Caller passes ``skip_hindi_respellings=True`` when
       the downstream TTS already handles native Devanagari (e.g.
       IndicF5 which was trained on raw Hindi text).

    Idempotent: re-running produces the same output. Numbers in time
    strings (10:30) and phone numbers (555-1234) are left intact via
    lookarounds.
    """
    if not text:
        return text
    out = _strip_verdict_acronym_sentences(text)
    out = _RE_CURRENCY.sub(_expand_currency, out)
    # AD/BC year markers run BEFORE date forms + bare integers so the
    # year digit is preserved as a year (not a cardinal) and the
    # AD/BC token is spelled as a letter pair.
    out = _RE_AD_BC_YEAR.sub(lambda m: f"{_spell_ad_bc(m)} {m.group(2)}", out)
    out = _RE_AD_BC_POST_YEAR.sub(_spell_ad_bc_post, out)
    # Date forms run BEFORE bare integers so the day numbers don't get
    # converted to cardinals first ("15 September" → "fifteen September").
    out = _RE_DAY_MONTH.sub(_expand_day_month, out)
    out = _RE_MONTH_DAY.sub(_expand_month_day, out)
    out = _RE_INTEGER.sub(_expand_bare_int, out)
    out = _RE_ANY_CASE_ACRONYM.sub(_expand_any_case_acronym, out)
    out = _RE_PROFANITY_ASSHOLE.sub(_sub_profanity_asshole, out)
    out = _RE_ALLCAPS_WORD.sub(_lowercase_emphatic_caps, out)
    if not skip_hindi_respellings:
        out = _apply_hindi_respellings(out)
    return out
