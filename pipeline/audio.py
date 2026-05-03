"""Stage 4 — TTS, provider-switchable.

Backends:

    * ``kokoro`` (default) — Kokoro 82M via ``kokoro-onnx``.
      ~169 MB model + 26 MB voices, fast on CPU, fixed voice library.
      Channel ``tts_voice`` is one of the Kokoro voice IDs (``af_bella``
      etc.).

    * ``f5_tts`` (opt-in) — F5-TTS-MLX. ~1.35 GB. Zero-shot voice cloning
      from a 5–15s reference clip. Channel ``tts_voice`` is interpreted
      as the path to that reference WAV; ``tts_ref_text`` is required.

The ``kokoro`` model is downloaded eagerly on first synth. ``f5_tts``
requires the optional ``f5-tts-mlx`` PyPI package and downloads its
model from HF on first use.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import soundfile as sf
from kokoro_onnx import Kokoro


# ---------- kokoro ---------------------------------------------------------

_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
_MODEL_FILE = "kokoro-v1.0.fp16.onnx"  # 169 MB — fp16, fast, good quality
_VOICES_FILE = "voices-v1.0.bin"        # 26 MB — all voices

_CACHE_DIR = Path.home() / ".cache" / "ytfactory" / "kokoro"
_KOKORO: Kokoro | None = None


def _download_if_missing(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {dest.name}…")
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.rename(dest)
    return dest


def _kokoro() -> Kokoro:
    global _KOKORO
    if _KOKORO is None:
        model_path = _download_if_missing(f"{_RELEASE}/{_MODEL_FILE}", _CACHE_DIR / _MODEL_FILE)
        voices_path = _download_if_missing(f"{_RELEASE}/{_VOICES_FILE}", _CACHE_DIR / _VOICES_FILE)
        _KOKORO = Kokoro(str(model_path), str(voices_path))
    return _KOKORO


# Map Kokoro voice-id prefix to its expected lang code.
# Prefix scheme: <lang>{f|m}_<name>. f=female, m=male.
_VOICE_PREFIX_LANG = {
    "a": "en-us",  # American
    "b": "en-gb",  # British
    "e": "es",     # Spanish
    "f": "fr-fr",  # French
    "h": "hi",     # Hindi
    "i": "it",     # Italian
    "j": "ja",     # Japanese
    "p": "pt-br",  # Portuguese (Brazilian)
    "z": "cmn",    # Mandarin Chinese
}


def lang_for_voice(voice_id: str) -> str:
    """Infer Kokoro lang code from voice id (e.g. 'af_bella' → 'en-us').

    Falls back to 'en-us' so unknown prefixes still produce audible
    output (Kokoro will read with an English accent rather than crash).
    """
    if not voice_id or len(voice_id) < 3:
        return "en-us"
    return _VOICE_PREFIX_LANG.get(voice_id[0], "en-us")


# Silence durations (seconds) between TTS chunks.
#
# Why two values: viewers reported that mid-narration pauses sometimes
# feel like the video has ENDED and trigger a swipe-away. Investigation
# (2026-05) found that Kokoro emits ~200–400ms of trailing silence in
# every chunk, which we used to concatenate WITHOUT trimming and then
# add another 0.55s explicit gap on top — total dead-air ≈ 0.75–1.0s
# at every paragraph break, well into "video ended?" territory. The fix
# is a tighter explicit gap PLUS per-chunk trailing-silence trim so
# 0.30s paragraph / 0.12s sentence is what actually plays out.
#
# Numbers chosen so a paragraph break is clearly longer than a sentence
# break (matching natural reading rhythm — a sentence period is ~0.20s,
# a paragraph reset wants to feel deliberate but never terminal).
_PARAGRAPH_PAUSE_S = 0.30
_SENTENCE_PAUSE_S = 0.12

# Hard cap on any silence run (Kokoro tail + explicit gap combined). If
# anything exceeds this we'd be back in "did the video end?" territory.
_MAX_SILENCE_S = 0.40


def _trim_trailing_silence(
    samples,                # numpy array of audio samples (float or int)
    sample_rate: int,
    threshold_db: float = -45.0,
    min_keep_s: float = 0.05,
):
    """Trim trailing silence from a 1-D audio buffer.

    Whisper hallucinates same-token runs ("that that that…") on a
    trailing silence tail; if there's no silence, there's nothing to
    hallucinate over. We trim everything below ``threshold_db`` after
    the last sample of speech, leaving a tiny ``min_keep_s`` cushion
    so the audio doesn't end on a hard click. Class-of-bug fix from
    audio-critic finding 2026-05.
    """
    import numpy as _np
    if samples.size == 0:
        return samples
    # Convert to float32 abs amplitude (works for int16 / float32 inputs).
    arr = _np.asarray(samples, dtype=_np.float32)
    if arr.dtype.kind == "i":
        arr = arr / float(_np.iinfo(samples.dtype).max)
    threshold = 10.0 ** (threshold_db / 20.0)
    above = _np.where(_np.abs(arr) > threshold)[0]
    if above.size == 0:
        return samples  # all silence — leave alone
    last = int(above[-1])
    keep_until = min(samples.shape[0], last + int(sample_rate * min_keep_s))
    if keep_until >= samples.shape[0]:
        return samples
    return samples[:keep_until]


_DEFAULT_MODULATION = {
    # Hook (first sentence) — slow for clarity. The first 1.5s of every
    # Short is the highest-leverage retention beat; a too-fast hook is
    # the single biggest swipe-away cause.
    "hook_speed_factor": 0.92,
    # Closer (last sentence of last paragraph) — slow for impact. The
    # listener should feel the question land, not feel rushed past it.
    "closer_speed_factor": 0.88,
    # Sentence containing "!" → energy bump. Exclamation correlates with
    # energetic delivery in real narration; small tempo bump approximates
    # that without needing a prosody-aware model.
    "excited_speed_factor": 1.10,
    # Sentence containing "..." → hanging-beat slowdown. The rewrite
    # prompt uses exactly one ellipsis per Short on the pivot reveal —
    # this is where the listener leans in, so we slow more aggressively
    # than other content rules.
    "hanging_speed_factor": 0.82,
    # Sentence with mid-text "?" (not the closer's terminal "?") reads
    # thoughtful — moderate slowdown.
    "thoughtful_speed_factor": 0.92,
    # Punch fragment: a ≤punch_max_words sentence following a
    # ≥punch_min_setup_words sentence. The rewrite prompt promises this
    # rhythm pattern ("She lied. Every time.") but flat per-paragraph
    # synthesis flattened it. Slowing the punch sentence at the model
    # level is what makes it actually LAND as a punch instead of being
    # read at the same speed as the build-up.
    "punch_speed_factor": 0.82,
    "punch_max_words": 3,
    "punch_min_setup_words": 6,
}


def _word_count(s: str) -> int:
    """Token count using whitespace split — close enough for our heuristics."""
    return len([w for w in s.split() if w.strip()])


def _modulate_sentence_speed(
    sentence_text: str,
    *,
    is_hook: bool,
    is_closer: bool,
    prev_sentence_words: int,
    base_speed: float,
    modulation: dict | None = None,
) -> tuple[float, str]:
    """Per-sentence speed multiplier.

    Returns ``(speed, tag)`` where ``tag`` is a one-word label naming
    the dominant rule applied (for log readability when scanning what
    the modulator actually did).

    Replaces the older per-paragraph version which left the entire
    middle of a Short at flat base speed because the content-driven
    knobs (excited / thoughtful / hanging) almost never fired at
    paragraph granularity. Per-sentence catches the build/punch rhythm
    the rewrite prompt promises but Kokoro otherwise flattens.

    Rules (applied multiplicatively, clamped to ±25% of base):
    - Hook sentence → slow for clarity.
    - Closer sentence → slow for impact.
    - Sentence with "..." → hanging-beat slowdown.
    - Sentence with "!" → energy bump.
    - Sentence with mid-text "?" → thoughtful slowdown.
    - Punch fragment (≤3 words after ≥6-word setup) → dramatic slowdown.

    Hook/closer always win when they apply; content rules only fire on
    non-hook, non-closer sentences so the structural anchors aren't
    overridden by punctuation that happens to land in the hook line.
    """
    m = {**_DEFAULT_MODULATION, **(modulation or {})}
    if not (modulation is None or modulation.get("enabled", True)):
        return base_speed, "off"

    factor = 1.0
    tag = "base"
    text = sentence_text.strip()
    words = _word_count(text)

    if is_hook:
        factor *= float(m["hook_speed_factor"])
        tag = "hook"
    elif is_closer:
        factor *= float(m["closer_speed_factor"])
        tag = "closer"
    else:
        # Punch wins over generic content rules — it's the most
        # dramatic moment and we don't want a "!" inside a 2-word
        # punch to flip it into excited.
        if (
            words <= int(m["punch_max_words"])
            and prev_sentence_words >= int(m["punch_min_setup_words"])
        ):
            factor *= float(m["punch_speed_factor"])
            tag = "punch"
        elif "..." in text:
            factor *= float(m["hanging_speed_factor"])
            tag = "hanging"
        elif "!" in text:
            factor *= float(m["excited_speed_factor"])
            tag = "excited"
        elif "?" in text and not text.rstrip().endswith("?"):
            factor *= float(m["thoughtful_speed_factor"])
            tag = "thoughtful"

    factor = max(0.75, min(1.25, factor))
    return base_speed * factor, tag


# ---- Sentence + paragraph splitting -----------------------------------------

import re as _re_split  # noqa: E402

_SENTENCE_SPLIT_RE = _re_split.compile(r"(?<=[.!?।॥])\s+")
_PARAGRAPH_SPLIT_RE = _re_split.compile(r"\n\s*\n")


def _split_into_sentences(narration: str) -> list[tuple[int, str]]:
    """Split narration into ``[(paragraph_idx, sentence_text), ...]``.

    Paragraph boundary = blank line (``\\n\\s*\\n``); sentence boundary
    = period / exclamation / question mark followed by whitespace.

    Empty sentences and empty paragraphs are filtered. Paragraph indices
    are dense (re-numbered after empty-skip) so callers can rely on
    ``p_idx == 0`` meaning hook and ``p_idx == max(p_idx)`` meaning
    closer-paragraph.
    """
    paras = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(narration) if p.strip()]
    out: list[tuple[int, str]] = []
    for p_idx, para in enumerate(paras):
        sents = [s.strip() for s in _SENTENCE_SPLIT_RE.split(para) if s.strip()]
        for s in sents:
            out.append((p_idx, s))
    return out


def _synth_kokoro(
    text: str,
    voice: str,
    out_path: Path,
    speed: float,
    lang: str | None = None,
    modulation: dict | None = None,
) -> Path:
    """Per-sentence synthesis with controlled inter-chunk silence.

    Was per-paragraph; now per-sentence so the punch / hanging /
    excited modulators can actually fire (at paragraph granularity
    they almost never did because exclamation/ellipsis usually live
    inside ONE sentence of the paragraph). Per-sentence costs ~5×
    more Kokoro invocations but it's the only way to make the
    rewrite prompt's promised rhythm audible.

    Trailing silence is trimmed off EVERY chunk before stitching so
    the pause that actually plays is the explicit gap, not Kokoro's
    tail (which used to add 200–400ms per paragraph break, pushing
    real silence above the "did the video end?" threshold).
    """
    kokoro = _kokoro()
    if lang is None:
        lang = lang_for_voice(voice)

    sentences = _split_into_sentences(text)
    if not sentences:
        # Empty/whitespace-only text — fall through so the caller
        # still gets a valid (probably empty) WAV instead of a crash.
        sentences = [(0, text)]

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if len(sentences) == 1:
        # Fast path: a one-sentence narration is too short for
        # modulation to mean anything. Single Kokoro call at base
        # speed matches the legacy behaviour exactly.
        samples, sample_rate = kokoro.create(
            sentences[0][1], voice=voice, speed=speed, lang=lang
        )
        samples = _trim_trailing_silence(samples, sample_rate)
        sf.write(out_path, samples, sample_rate)
        return out_path

    import numpy as _np

    n = len(sentences)
    last_p_idx = sentences[-1][0]
    chunks: list = []
    sample_rate: int | None = None
    prev_words = 0  # for the punch-fragment detector

    print(f"[tts] {n} sentences across {last_p_idx + 1} paragraph(s), "
          f"base speed {speed:.2f}, modulation:")
    for i, (p_idx, sentence) in enumerate(sentences):
        is_hook = i == 0
        is_closer = i == n - 1
        sent_speed, mod_tag = _modulate_sentence_speed(
            sentence_text=sentence,
            is_hook=is_hook,
            is_closer=is_closer,
            prev_sentence_words=prev_words,
            base_speed=speed,
            modulation=modulation,
        )
        delta = (sent_speed / speed - 1.0) * 100.0
        tag_str = f"{mod_tag}" if mod_tag != "base" else "base"
        delta_str = "" if abs(delta) < 0.5 else f" ({delta:+.0f}%)"
        print(
            f"[tts]   s{i:02d} p{p_idx}: speed={sent_speed:.2f} "
            f"[{tag_str}]{delta_str}  {sentence[:50]!r}"
        )
        s, sr = kokoro.create(sentence, voice=voice, speed=sent_speed, lang=lang)
        if sample_rate is None:
            sample_rate = sr
        # Trim Kokoro's tail off EVERY chunk. Without this, the per-chunk
        # tails (~200–400ms each) accumulate in front of the explicit
        # inter-chunk silence and produce >1s dead-air between
        # paragraphs — the "video ended?" failure mode.
        s_trimmed = _trim_trailing_silence(_np.asarray(s), sr)
        chunks.append(s_trimmed)

        if i < n - 1:
            next_p_idx = sentences[i + 1][0]
            same_paragraph = next_p_idx == p_idx
            gap_s = _SENTENCE_PAUSE_S if same_paragraph else _PARAGRAPH_PAUSE_S
            # Hard ceiling — defensive: if any caller bumps the gap
            # constants we never want to exceed _MAX_SILENCE_S of pure
            # silence (post-trim) at one location.
            gap_s = min(gap_s, _MAX_SILENCE_S)
            silence = _np.zeros(int(sr * gap_s), dtype=chunks[-1].dtype)
            chunks.append(silence)

        prev_words = _word_count(sentence)

    audio = _np.concatenate(chunks)
    # Final-tail trim only — the inter-chunk silences were already
    # built precisely above and must NOT be touched here.
    audio = _trim_trailing_silence(audio, sample_rate)
    sf.write(out_path, audio, sample_rate)
    return out_path


# ---------- f5_tts (zero-shot voice cloning, opt-in) -----------------------


def _synth_f5_tts(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str,
    out_path: Path,
    speed: float,
) -> Path:
    """Zero-shot voice cloning via F5-TTS-MLX.

    Requires::

        .venv/bin/pip install f5-tts-mlx

    On first call, fetches the F5-TTS-MLX checkpoint (~1.35 GB) into the
    HuggingFace cache. ``ref_audio_path`` should be a 5–15s WAV of the
    target voice; ``ref_audio_text`` is the transcript of that clip.
    """
    try:
        from f5_tts_mlx.generate import generate as f5_generate  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "TTS provider 'f5_tts' requires the f5-tts-mlx package.\n"
            "  install: .venv/bin/pip install f5-tts-mlx\n"
            f"  underlying error: {e}"
        ) from e

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # estimate_duration=True: the library's default is False, which makes
    # generate() emit a fixed ~1.4s clip regardless of input text length —
    # produced a 1.41s output for a 145-word narration on first contact.
    # With estimate_duration=True the library extrapolates target duration
    # from text length using the ref clip's tempo, which is the correct
    # behaviour for narration synthesis.
    # steps=8: the library default. Lower (4-6) is faster but visibly
    # noisier. Worth tuning per-host if generation latency is the bottleneck.
    f5_generate(
        generation_text=text,
        ref_audio_path=ref_audio_path,
        ref_audio_text=ref_audio_text,
        output_path=str(out_path),
        speed=speed,
        estimate_duration=True,
    )
    return out_path


# ---------- TTS-input normalisation ---------------------------------------
#
# Kokoro (and most neural TTS) reads "$2000" as "two zero zero zero" because
# it has no g2p rule for the dollar sign and falls back to digit-by-digit on
# unrecognised tokens. We pre-expand currency, K/M suffixes, and bare integers
# to spelled-out words so the audio sounds natural — and so Whisper hears
# the same words the source narration aligns against (no caption/audio drift).
# Applies to both Kokoro and f5_tts, both of which suffer the same numeral
# pronunciation problem to varying degrees.

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
    try:
        n = int(s)
    except ValueError:
        return s
    if n > 999_999_999:
        return s
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
    # Story closer / verdict acronyms. The slur "asshole" is sanitised
    # to "a hole" pre-TTS for YouTube monetisation — Kokoro speaks it
    # as two phonemes ("ay-hole"), Whisper transcribes those two words
    # back into the captions, and the on-screen caption matches the
    # spoken word. User feedback 2026-05-03.
    "AITA":  "am I the a hole",
    "WIBTA": "would I be the a hole",
    "YTA":   "you're the a hole",
    "NTA":   "not the a hole",
    "NAH":   "no a holes here",
    "ESH":   "everyone sucks here",
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
_AITA_ACRONYMS_CI: tuple[str, ...] = (
    "AITA", "WIBTA", "YTA", "NTA", "NAH", "ESH",
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
# Bare integer with word-boundary on each side. The negative-lookbehind for
# `-` and `:` sidesteps phone numbers (555-1234) and timestamps (10:30).
# The negative-lookahead for `:` and digit-hyphen prevents matching the left
# half of those patterns. Decimals like "1.5" — we expand the integer part
# only (the trailing ".5" pronounces correctly on Kokoro).
_RE_INTEGER = _re.compile(r"(?<![-:\d])\b(\d{1,9})\b(?![-:.\d])")


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


def normalize_for_tts(text: str) -> str:
    """Make ``text`` pronounceable by neural TTS.

    Five normalisations, applied in order:
    1. Currency → English words ($2000 → "two thousand dollars")
    2. Bare integers → English words (60 → "sixty")
    3. Case-insensitive AITA-class acronyms → their natural phrases
       (AITA / Aita / aita all → "am I the a hole"). Runs BEFORE the
       all-caps rule because lowercase "aita?" would otherwise slip
       past, leaving the literal letters for the TTS to phoneticise.
    4. Standalone "asshole" / "assholes" → "a hole" / "a holes"
       (YouTube monetisation sanitisation; see _RE_PROFANITY_ASSHOLE).
       Runs AFTER the acronym pass so any "asshole" the acronym pass
       just emitted (it doesn't anymore — the dict was updated — but
       belt-and-braces for any future regression) gets caught.
    5. Remaining all-caps tokens (any not handled above) → lowercase
       (so emphasis-CAPS don't read as letter-spelled acronyms).

    Idempotent: re-running produces the same output. Numbers in time
    strings (10:30) and phone numbers (555-1234) are left intact via
    lookarounds.
    """
    if not text:
        return text
    out = _RE_CURRENCY.sub(_expand_currency, text)
    out = _RE_INTEGER.sub(_expand_bare_int, out)
    out = _RE_ANY_CASE_ACRONYM.sub(_expand_any_case_acronym, out)
    out = _RE_PROFANITY_ASSHOLE.sub(_sub_profanity_asshole, out)
    out = _RE_ALLCAPS_WORD.sub(_lowercase_emphatic_caps, out)
    return out


# ---------- public API -----------------------------------------------------


def synthesize(
    text: str,
    voice: str,
    out_path: Path,
    speed: float = 1.0,
    provider: str = "kokoro",
    ref_audio_text: str | None = None,
    modulation: dict | None = None,
    pronunciation_dict: dict | None = None,
) -> Path:
    """Generate speech audio. Returns path to the .wav file.

    For ``provider='kokoro'``, ``voice`` is a Kokoro voice id.
    For ``provider='f5_tts'``, ``voice`` is the path to a 5–15s
    reference clip, and ``ref_audio_text`` is its transcript.

    Numbers and currency in ``text`` are expanded to spoken words before
    synthesis (Kokoro reads "$2000" as digit-by-digit otherwise). The
    caller should pass the SAME normalised string to source-text alignment
    downstream so beat captions match what was actually spoken — see
    ``normalize_for_tts``.

    ``modulation`` (Kokoro path only): per-paragraph speed-factor knobs.
    See ``_DEFAULT_MODULATION`` for the keys; channel YAML's
    ``tts_modulation`` block overrides individual factors. Pass
    ``{"enabled": False}`` to disable modulation entirely. Without
    modulation each paragraph runs at the base ``speed``; with it, the
    hook reads slightly slower for clarity, the closer reads slower
    for impact, exclamation-heavy paragraphs read faster, and ellipsis-
    trailing paragraphs read slower for the hanging-beat effect.
    """
    text = normalize_for_tts(text)
    # Pronunciation overrides apply ONLY to the TTS-input string.
    # Captions and ASR-source-text alignment must continue to see the
    # original (unrespelled) text upstream — synthesize is the only
    # path that should ever consume the respelled form.
    text = apply_pronunciation_overrides(text, pronunciation_dict)
    if provider == "kokoro":
        return _synth_kokoro(
            text, voice=voice, out_path=out_path, speed=speed,
            modulation=modulation,
        )
    if provider == "f5_tts":
        if not ref_audio_text:
            raise ValueError(
                "f5_tts provider requires `ref_audio_text` (the transcript of "
                "the reference audio at `voice`)"
            )
        return _synth_f5_tts(
            text,
            ref_audio_path=voice,
            ref_audio_text=ref_audio_text,
            out_path=out_path,
            speed=speed,
        )
    raise ValueError(f"unknown TTS provider {provider!r} (choices: kokoro, f5_tts)")
