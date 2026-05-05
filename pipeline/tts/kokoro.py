"""Kokoro 82M provider — fast English/Hindi/multilingual TTS.

Apache 2.0, ~169 MB model + 26 MB voices. Fixed voice library; channel
``tts_voice`` is one of the Kokoro voice IDs (``af_bella``, ``hf_alpha``,
etc.). Default for airecap, hindutavaanimated, and historyrecapped Shorts.

Provider-specific behaviour:

* Per-sentence synthesis (not per-paragraph) so the modulation knobs
  (hook / closer / punch / hanging / excited / thoughtful) actually fire.
* Trailing-silence trim per chunk to keep inter-paragraph gaps under
  the "did the video end?" threshold.
* Voice-prefix → language inference (``af_*`` → en-us, ``hf_*`` → hi, etc.).

Public surface re-exported by :mod:`pipeline.audio` (preserve verbatim
so monkeypatch tests + existing imports keep working):

* ``_kokoro``                    — model singleton accessor
* ``_synth_kokoro``              — per-sentence synth → WAV
* ``lang_for_voice``             — voice-id → lang code lookup
* ``_split_into_sentences``      — paragraph-aware sentence splitter
* ``_modulate_sentence_speed``   — per-sentence speed multiplier rules
* ``_trim_trailing_silence``     — also re-used by other providers
"""
from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import soundfile as sf
from kokoro_onnx import Kokoro


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
