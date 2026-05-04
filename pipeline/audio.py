"""Stage 4 — TTS, provider-switchable.

Backends:

    * ``kokoro`` — Kokoro 82M via ``kokoro-onnx``. ~169 MB model + 26 MB
      voices, fast on CPU, fixed voice library. Channel ``tts_voice``
      is one of the Kokoro voice IDs (``af_bella`` etc.). Default for
      airecap and historyrecapped long-form (Apache 2.0).

    * ``f5_tts`` — F5-TTS-MLX. ~1.35 GB. Zero-shot voice cloning from a
      5-15s reference WAV. Apple-Silicon-native (MLX). Channel
      ``tts_voice`` is the ref-WAV path; ``tts_ref_text`` is required.
      Default for sober English narration (historyrecapped Shorts,
      sportstoriesanimated). MIT.

    * ``chatterbox`` — Resemble AI Chatterbox. ~3 GB. Zero-shot voice
      cloning + emotion-exaggeration control. Channel ``tts_voice`` is
      the ref-WAV path. Default for emotional narration
      (mystoriesanimated AITA). MIT.

    * ``styletts2`` — StyleTTS2. Best open-source long-form prosody.
      Channel ``tts_voice`` is the ref-WAV path. Opt-in upgrade for
      long-form sleep narration. MIT.

    * ``indic_parler`` — AI4Bharat Indic Parler-TTS. Description-
      conditioned (NOT voice-cloned). Covers 20 Indic languages
      including Hindi. Channel ``tts_voice`` is a natural-language
      description of the desired voice. Default for hindutavaanimated.
      Apache 2.0.

    * ``cartesia`` — Cartesia Sonic-2 (paid API). Pre-2026-05-04 default;
      now retained as a per-channel override for ship-quality renders
      where the cost is justified. Requires ``CARTESIA_API_KEY``.

All local providers download their model on first synth into the HF
cache (or ``~/.cache/ytfactory/`` for kokoro). The optional providers
require the matching pip-install; see ``requirements.txt`` for the
opt-in install lines.
"""

from __future__ import annotations

import urllib.error
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


# ---------- cartesia (Sonic-2, paid API) -----------------------------------
#
# Cartesia Sonic-2 is the production narration backbone for AITA / sports /
# History Recapped channels (user decision 2026-05-03 after A/B vs ElevenLabs;
# see `scripts/tts_ab_spike.py` and memory project_cartesia_tts_upgrade.md).
# Pricing: Sonic Starter $9/mo for 100k chars (~100 shorts/mo) — covers
# typical channel cadence with 5× headroom; Pro $39/mo for 1.25M chars
# if/when we scale to compilations.
#
# Voice IDs are channel-configurable. Default below is "Sarah" — US female
# narrator that the user approved on the spike. Channels override via
# `tts_voice: <cartesia voice uuid>` in their YAML (the `tts_voice` field
# is interpreted per-provider — Kokoro voice id for kokoro, ref-WAV path
# for f5_tts, Cartesia voice UUID for cartesia).
#
# API key is read from `CARTESIA_API_KEY` at synth time; fails fast with a
# pointer to https://play.cartesia.ai/keys if unset. Never hardcoded.

_CARTESIA_DEFAULT_VOICE = "694f9389-aac1-45b6-b726-9d9369183238"  # Sarah, US female
_CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"
_CARTESIA_API_VERSION = "2024-11-13"


def _synth_cartesia(
    text: str,
    voice: str,
    out_path: Path,
    speed: float,
    language: str = "en",
) -> Path:
    """Cartesia Sonic-2 synthesis via the REST `/tts/bytes` endpoint.

    Single-shot synthesis (no per-sentence modulation, unlike the Kokoro
    path) — the model emits one continuous WAV for the full narration.
    Per-sentence speed modulation is Kokoro-specific; if a channel needs
    it, the channel should stay on Kokoro.

    `speed` is mapped to Cartesia's `__experimental_controls.speed` knob,
    which accepts `slow|normal|fast` (string) or a float in [-1.0, 1.0].
    Channels declare `tts_speed` at human pace (e.g. 0.95 for war
    history, 0.90 for AITA); we map ≤0.95 → "slow", ≥1.05 → "fast",
    everything else → "normal", which is the closest analogue Cartesia
    supports without needing per-channel calibration.
    """
    import os

    api_key = os.environ.get("CARTESIA_API_KEY")
    if not api_key:
        raise RuntimeError(
            "tts_provider=cartesia requires CARTESIA_API_KEY env var.\n"
            "Get a key: https://play.cartesia.ai/keys\n"
            "Set in shell: export CARTESIA_API_KEY=sk_car_..."
        )

    if speed <= 0.95:
        cartesia_speed = "slow"
    elif speed >= 1.05:
        cartesia_speed = "fast"
    else:
        cartesia_speed = "normal"

    import json as _json

    body = _json.dumps({
        "model_id": "sonic-2",
        "transcript": text,
        "voice": {"mode": "id", "id": voice or _CARTESIA_DEFAULT_VOICE},
        "output_format": {
            "container": "wav",
            "encoding": "pcm_s16le",
            "sample_rate": 44100,
        },
        "language": language,
        "__experimental_controls": {"speed": cartesia_speed},
    }).encode("utf-8")
    headers = {
        "X-API-Key": api_key,
        "Cartesia-Version": _CARTESIA_API_VERSION,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(_CARTESIA_TTS_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            audio_bytes = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Cartesia HTTP {e.code} from {_CARTESIA_TTS_URL}\n{detail}"
        ) from None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(audio_bytes)
    return out_path


# ---------- audio trim helpers (sung-channel duration cap) -----------------


def _detect_leading_silence_s(
    in_path: Path, threshold_db: float = -28.0, min_silence_s: float = 0.3
) -> float:
    """Return the duration of low-RMS leading silence at the start of a WAV.

    Suno songs frequently open with a 1-5s quiet guitar intro that
    Whisper hallucinates as plausible-but-wrong sung words at the wrong
    timestamps. Trimming this before passing to ASR collapses the
    caption-desync class of bug at the source rather than masking it
    after the fact. Threshold tuned to -28 dB based on the
    abhimanyu / hathi-raja sample (vocals at -14 to -20 dB, guitar
    intros around -34 to -38 dB).
    """
    import re as _re
    import subprocess as _sp

    cmd = [
        "ffmpeg", "-i", str(in_path),
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence_s}",
        "-f", "null", "-",
    ]
    res = _sp.run(cmd, capture_output=True, text=True)
    leading = 0.0
    for line in res.stderr.splitlines():
        m = _re.search(r"silence_start:\s*(\d+\.\d+)", line)
        if m and float(m.group(1)) < 0.05:
            # Find the matching silence_end on the next silence_end line
            # in subsequent output.
            continue
        m_end = _re.search(r"silence_end:\s*(\d+\.\d+).*silence_duration:\s*(\d+\.\d+)", line)
        if m_end:
            end_s = float(m_end.group(1))
            dur_s = float(m_end.group(2))
            start_s = end_s - dur_s
            if start_s < 0.05:
                leading = end_s
                break
    return leading


def trim_song_for_short(
    in_path: Path,
    out_path: Path,
    max_duration_s: float,
    *,
    trim_start_s: float = 0.0,
    drop_leading_silence: bool = False,
    leading_silence_threshold_db: float = -28.0,
    drop_vocal_pickup: bool = False,
    vocal_pickup_threshold_db: float = -22.0,
    fade_out_s: float = 0.0,
) -> tuple[float, float]:
    """Trim a song WAV for the Shorts duration cap.

    Pipeline:
      1. Skip the leading instrumental intro. Either via an explicit
         ``trim_start_s`` (recommended for Suno — the intro length is
         consistent at ~2-3s and ffmpeg's silencedetect uses PEAK not
         RMS so quiet-guitar intros are missed by auto-detection), OR
         via ``drop_leading_silence=True`` which probes silencedetect
         (works on truly silent intros only).
      2. (NEW 2026-05-03 v4 critique) If ``drop_vocal_pickup=True``,
         apply ffmpeg ``silenceremove`` on the trimmed audio to drop
         any residual quiet pickup beat between the rough intro cut
         and the actual vocal entry. Suno V4_5 vocals don't always
         land at exactly the requested start offset; this catches
         the 0.3-1.0s "breath / pickup" that left captions ahead of
         the audio.
      3. Cap to ``max_duration_s``.
      4. (NEW 2026-05-03 v4 critique) If ``fade_out_s > 0``, apply
         ``afade=t=out`` over the final N seconds. Eliminates the
         abrupt mid-chorus cut at the duration cap so the closer
         hold transitions gracefully from sung→quiet→still-image.

    Returns ``(trim_start_s, trim_end_s)`` on the SOURCE timeline so the
    caller can log what was cut.

    Caveat: if ``drop_vocal_pickup`` ate ~0.3-0.5s, the actual output
    is slightly less than ``max_duration_s`` and the fadeout's
    effective duration shrinks proportionally. Both effects are still
    net wins versus no-fadeout / no-pickup-drop.
    """
    import subprocess as _sp

    explicit_start = float(trim_start_s or 0.0)
    if drop_leading_silence and explicit_start <= 0.0:
        explicit_start = _detect_leading_silence_s(
            in_path, threshold_db=leading_silence_threshold_db
        )

    trim_end_s = explicit_start + max_duration_s

    af_parts: list[str] = []
    if drop_vocal_pickup:
        # Drop leading audio quieter than the vocal threshold until the
        # first sample louder. start_silence=0.05 tolerates a 50ms
        # low-level lead-in; start_duration=0.1 means after we detect
        # voice we keep 100ms before re-checking. Tuned for Suno's
        # vocal pickup transient.
        af_parts.append(
            f"silenceremove=start_periods=1:"
            f"start_threshold={vocal_pickup_threshold_db}dB:"
            f"start_silence=0.05:start_duration=0.1"
        )
    if fade_out_s > 0:
        # afade.st is in OUTPUT timeline AFTER any silenceremove. We
        # use max_duration_s as the assumed output duration; if
        # silenceremove cut some leading audio, output is shorter and
        # the fade truncates accordingly (still a fade, just shorter).
        af_parts.append(
            f"afade=t=out:st={max(0.0, max_duration_s - fade_out_s):.3f}:"
            f"d={fade_out_s:.3f}"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{explicit_start:.3f}",
        "-i", str(in_path),
        "-t", f"{max_duration_s:.3f}",
    ]
    if af_parts:
        cmd += ["-af", ",".join(af_parts)]
    cmd += [
        "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le",
        str(out_path),
    ]
    _sp.run(cmd, check=True, capture_output=True)
    return (explicit_start, trim_end_s)


# ---------- sunoapi (Suno via sunoapi.org wrapper, sung music) -------------
#
# Suno is the de-facto best AI singing model for bilingual children's
# rhymes (Hinglish, kids' choir, melody adherence). There is no
# official Suno HTTP API for individual devs — the public path is
# the unofficial wrapper at https://sunoapi.org/.
#
# Pricing: pay-per-generation, ~$0.05-0.10/song on the V4_5 model.
# Top up credits at https://sunoapi.org/topup.
#
# API:
#   POST https://api.sunoapi.org/api/v1/generate
#     Authorization: Bearer <SUNOAPI_API_KEY>
#     body: {customMode, instrumental, callBackUrl, model, prompt, style, title, vocalGender}
#     returns: {data: {taskId}}
#   GET https://api.sunoapi.org/api/v1/generate/record-info?taskId=<id>
#     returns: {data: {status, response: {sunoData: [{audioUrl, ...}]}}}
#     poll until status == "SUCCESS" or "FAILED"
#
# Risk: sunoapi.org is an unofficial Suno wrapper; Suno periodically
# DMCAs these. Has weathered ~3 cycles since 2024, currently up. If
# it goes down, fall back to manual Suno (audio_provider:
# external_song with the songs/<slug>.wav drop convention).

_SUNOAPI_BASE = "https://api.sunoapi.org"
_SUNOAPI_GENERATE = f"{_SUNOAPI_BASE}/api/v1/generate"
_SUNOAPI_RECORD = f"{_SUNOAPI_BASE}/api/v1/generate/record-info"


def synth_via_sunoapi(
    lyrics: str,
    style: str,
    out_path: Path,
    *,
    title: str = "",
    model: str = "V4_5",
    vocal_gender: str = "f",
    poll_timeout_s: int = 300,
    poll_interval_s: int = 5,
) -> Path:
    """Generate a sung song via sunoapi.org's Suno wrapper.

    ``lyrics`` is the full lyrics block (one per line, may include
    [Verse 1] / [Chorus] tags — Suno honors those structural markers).
    ``style`` is a one-sentence description of genre + instrumentation
    + tempo (e.g. "cheerful upbeat children's nursery rhyme, female
    lead with kids choir, gentle acoustic guitar + tabla, 120 BPM").

    Returns the path to the downloaded WAV. Raises RuntimeError on
    auth failure, generation failure, or poll timeout.

    Sunoapi.org's required ``callBackUrl`` field is set to a placeholder;
    we poll the record-info endpoint instead of relying on the webhook
    callback (callbacks need a public-internet URL the laptop agent
    doesn't have).
    """
    import json as _json
    import os as _os
    import time as _time

    api_key = _os.environ.get("SUNOAPI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "audio_provider=sunoapi requires SUNOAPI_API_KEY env var.\n"
            "Sign up: https://sunoapi.org/api-key (~$10 top-up = ~100 songs).\n"
            "Set in shell: export SUNOAPI_API_KEY=...\n"
            "Or add to .env: SUNOAPI_API_KEY=..."
        )

    body = _json.dumps({
        "customMode": True,
        "instrumental": False,
        "callBackUrl": "https://example.com/noop",  # required by schema; we poll instead
        "model": model,
        "prompt": lyrics,
        "style": style,
        "title": title or "Untitled",
        "vocalGender": vocal_gender,
    }).encode("utf-8")
    # Cloudflare on api.sunoapi.org rejects the default urllib UA with
    # error 1010 ("access denied"). Adding a real browser-class UA + an
    # Accept header makes the request look like a normal client and gets
    # past the CF bot challenge. Confirmed 2026-05-03 — without this the
    # generate endpoint returns HTTP 403 immediately.
    _UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _UA,
    }

    print(f"[sunoapi] generate model={model} vocal={vocal_gender} "
          f"lyrics={len(lyrics)}c style={len(style)}c…")
    req = urllib.request.Request(_SUNOAPI_GENERATE, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"sunoapi generate HTTP {e.code}\n{detail}") from None

    task_id = (result.get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"sunoapi generate returned no taskId: {result}")
    print(f"[sunoapi] task_id={task_id}; polling for completion…")

    deadline = _time.time() + poll_timeout_s
    while _time.time() < deadline:
        poll_req = urllib.request.Request(
            f"{_SUNOAPI_RECORD}?taskId={task_id}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": _UA,
            },
        )
        try:
            with urllib.request.urlopen(poll_req, timeout=30) as resp:
                data = _json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"[sunoapi] poll HTTP {e.code} — retrying")
            _time.sleep(poll_interval_s)
            continue

        outer = data.get("data") or {}
        status = outer.get("status", "?")
        if status == "SUCCESS":
            songs = (outer.get("response") or {}).get("sunoData") or []
            if not songs:
                raise RuntimeError(f"sunoapi SUCCESS but empty sunoData: {data}")
            audio_url = songs[0].get("audioUrl")
            if not audio_url:
                raise RuntimeError(f"sunoapi SUCCESS but no audioUrl in {songs[0]!r}")
            print(f"[sunoapi] downloading {audio_url}")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            # Cloudflare on the audio CDN (tempfile.aiquickdraw.com)
            # also rejects Python-urllib UA. Use the browser UA we set
            # for the API itself and stream the bytes ourselves rather
            # than urlretrieve which doesn't accept custom headers.
            dl_req = urllib.request.Request(
                audio_url,
                headers={"User-Agent": _UA, "Accept": "*/*"},
            )
            # sunoapi V4_5 returns .mp3, not .wav. Whisper / ffmpeg both
            # handle MP3 transparently downstream, but the pipeline names
            # the cache slot `narration.wav` by convention. We download
            # the MP3 to a sibling .mp3, then ffmpeg-transcode to the
            # caller's out_path so any caller asking for .wav gets a
            # proper WAV (44.1k mono PCM s16le) regardless of what Suno
            # served. Saves callers from having to know the extension.
            mp3_path = out_path.with_suffix(".mp3")
            with urllib.request.urlopen(dl_req, timeout=120) as resp:
                mp3_path.write_bytes(resp.read())
            if out_path.suffix.lower() == ".wav":
                import subprocess as _sp
                _sp.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-i", str(mp3_path),
                     "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le",
                     str(out_path)],
                    check=True,
                )
                # Keep mp3_path for debugging — small file, helpful when
                # listening to the raw Suno output without the WAV transcode.
            else:
                # Caller asked for .mp3 (or other) — just rename.
                mp3_path.rename(out_path)
            return out_path
        if status in ("FAILED", "ERROR", "FAIL", "CREATE_TASK_FAILED"):
            raise RuntimeError(f"sunoapi generation failed: status={status} body={data}")

        # Still pending — log every ~30s so a long render is observable.
        elapsed = int(poll_timeout_s - (deadline - _time.time()))
        if elapsed % 30 == 0:
            print(f"[sunoapi] status={status} (elapsed {elapsed}s)")
        _time.sleep(poll_interval_s)

    raise RuntimeError(
        f"sunoapi timed out after {poll_timeout_s}s for task {task_id}"
    )


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


# ---------- chatterbox (Resemble AI, MIT, voice cloning, opt-in) -----------
#
# Chatterbox is Resemble AI's open-source TTS — MIT-licensed, free for
# commercial use, runs locally on Apple Silicon via PyTorch MPS. Beat
# ElevenLabs in blind A/B tests (63.75% preference, 2026 benchmarks) and
# has built-in emotion-exaggeration control which makes it the right
# fit for AITA-style emotional storytelling on mystoriesanimated.
#
# Channel `tts_voice` is interpreted as the path to a 5-15s reference WAV
# of the target voice (same convention as f5_tts). `ref_audio_text` is
# NOT required by Chatterbox (it does not need the transcript — unlike
# F5-TTS — because its zero-shot path conditions on audio embeddings only).
#
# Output watermark: Chatterbox embeds Resemble's perceptually-inaudible
# Perth watermark for AI-detection traceability. It does not affect
# listening quality or YouTube monetization.

_CHATTERBOX_MODEL = None  # lazy global, lives across synth calls


def _chatterbox_model():
    """Lazy-load + cache the Chatterbox model singleton.

    First call: ~3 GB checkpoint download into the HF cache. Subsequent
    calls reuse the in-memory model — costs ~6 GB of RAM, fine on M2 Max.
    """
    global _CHATTERBOX_MODEL
    if _CHATTERBOX_MODEL is None:
        try:
            from chatterbox.tts import ChatterboxTTS  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "TTS provider 'chatterbox' requires the chatterbox-tts package.\n"
                "  install: .venv/bin/pip install chatterbox-tts\n"
                f"  underlying error: {e}"
            ) from e
        # Apple Silicon → MPS; falls back to CPU on other hardware.
        # The lib's from_pretrained handles device routing internally.
        import torch  # type: ignore

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _CHATTERBOX_MODEL = ChatterboxTTS.from_pretrained(device=device)
    return _CHATTERBOX_MODEL


def _synth_chatterbox(
    text: str,
    ref_audio_path: str,
    out_path: Path,
    speed: float,
    exaggeration: float = 0.5,
    cfg_weight: float = 0.5,
) -> Path:
    """Zero-shot voice cloning via Chatterbox.

    Requires::

        .venv/bin/pip install chatterbox-tts

    `ref_audio_path` should be a 5-15s WAV of the target voice. `speed`
    is mapped to the post-synth atempo factor — Chatterbox's generator
    runs at native rate; we ffmpeg-stretch the output to match the
    channel's `tts_speed`. `exaggeration` (0.0-1.0) controls emotion
    intensity — 0.5 is the library default and the right neutral for
    AITA conversational; bump toward 0.7 for high-drama stories.
    """
    import numpy as _np
    import soundfile as _sf

    model = _chatterbox_model()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wav = model.generate(
        text,
        audio_prompt_path=ref_audio_path,
        exaggeration=exaggeration,
        cfg_weight=cfg_weight,
    )
    # Chatterbox returns a torch.Tensor at model.sr (24kHz typically).
    sr = int(model.sr)
    audio_np = wav.detach().cpu().numpy().squeeze()

    # Apply speed via post-synth atempo if requested. atempo accepts
    # 0.5-2.0 in a single pass; outside that range it'd need chaining,
    # but channel tts_speed is always within 0.85-1.2 in practice.
    if abs(speed - 1.0) > 0.01:
        raw_path = out_path.with_suffix(".raw.wav")
        _sf.write(raw_path, audio_np, sr)
        import subprocess as _sp

        cmd = [
            "ffmpeg", "-y", "-i", str(raw_path),
            "-filter:a", f"atempo={speed:.4f}",
            "-ar", str(sr), "-ac", "1",
            str(out_path),
        ]
        _sp.run(cmd, check=True, capture_output=True)
        raw_path.unlink(missing_ok=True)
    else:
        _sf.write(out_path, audio_np, sr)

    return out_path


# ---------- styletts2 (best long-form prosody, opt-in) ---------------------
#
# StyleTTS2 (Aaron (Yinghao) Li, MIT) is the best open-source model for
# long-form narration prosody — used here as the upgrade path for the
# historyrecapped long-form sleep videos. The default Kokoro+atempo
# pipeline at the top of this file is fully sufficient for most
# narrations; switch to StyleTTS2 only when prosody quality is the
# bottleneck (e.g. a 60-min sleep video where micro-pauses and rhythm
# carry the listener through).
#
# Channel `tts_voice` is the path to a 5-15s reference WAV.
#
# Why not the default for short channels: StyleTTS2's PyTorch path is
# slower than F5-TTS-MLX or Kokoro on M2 Max, and the prosody lift is
# most audible past ~30s of continuous narration. For Shorts the cost
# isn't worth it; F5-TTS-MLX is the pick.

_STYLETTS2_MODEL = None


def _styletts2_model():
    global _STYLETTS2_MODEL
    if _STYLETTS2_MODEL is None:
        try:
            from styletts2 import tts as _styletts2_tts  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "TTS provider 'styletts2' requires the styletts2 package.\n"
                "  install: .venv/bin/pip install styletts2\n"
                f"  underlying error: {e}"
            ) from e
        _STYLETTS2_MODEL = _styletts2_tts.StyleTTS2()
    return _STYLETTS2_MODEL


def _synth_styletts2(
    text: str,
    ref_audio_path: str,
    out_path: Path,
    speed: float,
    alpha: float = 0.3,
    beta: float = 0.7,
    diffusion_steps: int = 7,
    embedding_scale: float = 1.0,
) -> Path:
    """Long-form-tuned synthesis via StyleTTS2.

    Requires::

        .venv/bin/pip install styletts2

    `alpha`/`beta` blend timbre vs prosody from the reference clip; the
    library's recommended defaults (0.3/0.7) preserve speaker identity
    while leaning on the model's own prosody style — exactly the right
    balance for sleep narration where consistency matters more than
    mimicking every micro-inflection of the ref.
    """
    import soundfile as _sf

    model = _styletts2_model()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    audio = model.inference(
        text,
        target_voice_path=ref_audio_path,
        alpha=alpha,
        beta=beta,
        diffusion_steps=diffusion_steps,
        embedding_scale=embedding_scale,
    )
    # StyleTTS2 returns a numpy array at 24kHz.
    sr = 24000
    _sf.write(out_path, audio, sr)

    if abs(speed - 1.0) > 0.01:
        raw_path = out_path.with_suffix(".raw.wav")
        out_path.rename(raw_path)
        import subprocess as _sp

        cmd = [
            "ffmpeg", "-y", "-i", str(raw_path),
            "-filter:a", f"atempo={speed:.4f}",
            "-ar", str(sr), "-ac", "1",
            str(out_path),
        ]
        _sp.run(cmd, check=True, capture_output=True)
        raw_path.unlink(missing_ok=True)

    return out_path


# ---------- indic_parler (AI4Bharat, Apache 2.0, Hindi + 19 Indic langs) ---
#
# Indic Parler-TTS is the AI4Bharat + HuggingFace audio team's
# Apache-2.0 model covering 20 Indic languages including Hindi. Free,
# commercial-safe, and the only credible open-source path for Hindi
# narration on hindutavaanimated.
#
# DIFFERENT INTERFACE: Parler-TTS is description-conditioned, not
# voice-cloned. Channel `tts_voice` is interpreted as a NATURAL-LANGUAGE
# DESCRIPTION of the target voice (e.g. "A female speaker delivers a
# slightly expressive and animated speech with a moderate speed and
# pitch. The recording is of very high quality, with the speaker's
# voice sounding clear and very close up."). The library has named
# voices (Rohit, Divya, Sneha, etc.) — name them in the description
# and the model conditions on that identity.
#
# Reference: https://huggingface.co/ai4bharat/indic-parler-tts

_INDIC_PARLER_MODEL = None
_INDIC_PARLER_TOKENIZER = None
_INDIC_PARLER_DESC_TOKENIZER = None


def _indic_parler_model():
    global _INDIC_PARLER_MODEL, _INDIC_PARLER_TOKENIZER, _INDIC_PARLER_DESC_TOKENIZER
    if _INDIC_PARLER_MODEL is None:
        try:
            from parler_tts import ParlerTTSForConditionalGeneration  # type: ignore
            from transformers import AutoTokenizer  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "TTS provider 'indic_parler' requires the parler-tts package.\n"
                "  install: .venv/bin/pip install git+https://github.com/huggingface/parler-tts.git\n"
                f"  underlying error: {e}"
            ) from e
        import torch  # type: ignore

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        repo = "ai4bharat/indic-parler-tts"
        try:
            _INDIC_PARLER_MODEL = ParlerTTSForConditionalGeneration.from_pretrained(
                repo
            ).to(device)
        except OSError as e:
            if "gated" in str(e).lower() or "403" in str(e):
                raise RuntimeError(
                    "TTS provider 'indic_parler' needs HuggingFace gated-repo access.\n"
                    f"  1. Visit https://huggingface.co/{repo}\n"
                    "  2. Click 'Agree and access repository' (one-time, free)\n"
                    "  3. Ensure your HF token is logged in: huggingface-cli login\n"
                    f"  underlying error: {e}"
                ) from e
            raise
        _INDIC_PARLER_TOKENIZER = AutoTokenizer.from_pretrained(repo)
        # Parler uses a SEPARATE tokenizer for the description prompt
        # (the description encoder is a t5-class model, the prompt
        # encoder is the model's own tokenizer).
        _INDIC_PARLER_DESC_TOKENIZER = AutoTokenizer.from_pretrained(
            _INDIC_PARLER_MODEL.config.text_encoder._name_or_path
        )
    return _INDIC_PARLER_MODEL, _INDIC_PARLER_TOKENIZER, _INDIC_PARLER_DESC_TOKENIZER


_INDIC_PARLER_DEFAULT_DESCRIPTION = (
    "Sneha speaks in a calm, gentle, expressive Hindi storytelling tone "
    "with a moderate speed and warm pitch. The recording is of very high "
    "quality, with the speaker's voice sounding clear and very close up, "
    "no background noise."
)


def _synth_indic_parler(
    text: str,
    description: str,
    out_path: Path,
    speed: float,
) -> Path:
    """Hindi (and other Indic-language) synthesis via Indic Parler-TTS.

    Requires::

        .venv/bin/pip install git+https://github.com/huggingface/parler-tts.git

    `description` is a natural-language description of the desired voice
    (speaker name, emotion, pace, recording quality). The channel YAML
    surfaces this via `tts_voice` — e.g. set `tts_voice: "Sneha speaks
    calmly..."` to name the speaker. Empty/None falls back to the
    default Sneha description above.
    """
    import soundfile as _sf
    import torch  # type: ignore

    model, tok, desc_tok = _indic_parler_model()
    desc_text = description or _INDIC_PARLER_DEFAULT_DESCRIPTION

    device = next(model.parameters()).device
    desc_inputs = desc_tok(desc_text, return_tensors="pt").to(device)
    prompt_inputs = tok(text, return_tensors="pt").to(device)

    with torch.no_grad():
        gen = model.generate(
            input_ids=desc_inputs.input_ids,
            attention_mask=desc_inputs.attention_mask,
            prompt_input_ids=prompt_inputs.input_ids,
            prompt_attention_mask=prompt_inputs.attention_mask,
        )
    audio = gen.cpu().numpy().squeeze()
    sr = model.config.sampling_rate

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _sf.write(out_path, audio, sr)

    if abs(speed - 1.0) > 0.01:
        raw_path = out_path.with_suffix(".raw.wav")
        out_path.rename(raw_path)
        import subprocess as _sp

        cmd = [
            "ffmpeg", "-y", "-i", str(raw_path),
            "-filter:a", f"atempo={speed:.4f}",
            "-ar", str(sr), "-ac", "1",
            str(out_path),
        ]
        _sp.run(cmd, check=True, capture_output=True)
        raw_path.unlink(missing_ok=True)

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
# Cartesia narrated "two-comma-five hundred". `_expand_bare_int` strips
# the commas before int() conversion.
_RE_INTEGER = _re.compile(
    r"(?<![-:\d])\b(\d{1,3}(?:,\d{3})+|\d{1,9})\b(?![-:.\d])"
)

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
    above (.!?।॥ followed by whitespace) so the audio path's stripping
    aligns with how _synth_kokoro will chunk afterwards.
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


def normalize_for_tts(text: str) -> str:
    """Make ``text`` pronounceable by neural TTS.

    Seven normalisations, applied in order:
    1. AITA-class verdict acronyms (AITA / WIBTA / YTA / NTA / NAH / ESH)
       — entire containing sentence STRIPPED. User feedback 2026-05-03:
       the channel must never pronounce these acronyms. Runs FIRST so
       the downstream all-caps + acronym passes never see them. The
       visual closer panel still renders the engagement ask from
       cfg["closer_format"] independently. See _strip_verdict_acronym_sentences.
    2. Currency → English words ($2000 → "two thousand dollars")
    3. Bare integers → English words (60 → "sixty")
    4. Case-insensitive Reddit-class acronyms (MIL/FIL/SIL/BIL/DIL/OOP/
       VLC/TIFU/TIL) → their natural phrases. AITA-class verdict
       acronyms are NOT in this list anymore — they were stripped by
       step 1 above.
    5. Standalone "asshole" / "assholes" → "a hole" / "a holes"
       (YouTube monetisation sanitisation; see _RE_PROFANITY_ASSHOLE).
    6. Remaining all-caps tokens → lowercase (so emphasis-CAPS don't
       read as letter-spelled acronyms).
    7. Hindi tatsama + numeral respellings — only applied when text
       contains Devanagari. Fixes Kokoro's collapse of conjuncts +
       numeral homophones (सात↔साथ, सोलह→soleh).

    Idempotent: re-running produces the same output. Numbers in time
    strings (10:30) and phone numbers (555-1234) are left intact via
    lookarounds.
    """
    if not text:
        return text
    out = _strip_verdict_acronym_sentences(text)
    out = _RE_CURRENCY.sub(_expand_currency, out)
    # Date forms run BEFORE bare integers so the day numbers don't get
    # converted to cardinals first ("15 September" → "fifteen September").
    out = _RE_DAY_MONTH.sub(_expand_day_month, out)
    out = _RE_MONTH_DAY.sub(_expand_month_day, out)
    out = _RE_INTEGER.sub(_expand_bare_int, out)
    out = _RE_ANY_CASE_ACRONYM.sub(_expand_any_case_acronym, out)
    out = _RE_PROFANITY_ASSHOLE.sub(_sub_profanity_asshole, out)
    out = _RE_ALLCAPS_WORD.sub(_lowercase_emphatic_caps, out)
    out = _apply_hindi_respellings(out)
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
    language: str = "en",
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
    if provider == "chatterbox":
        # `voice` = path to a 5-15s reference WAV. ref_audio_text not
        # required — Chatterbox conditions on audio embeddings only.
        return _synth_chatterbox(
            text,
            ref_audio_path=voice,
            out_path=out_path,
            speed=speed,
        )
    if provider == "styletts2":
        # `voice` = path to a 5-15s reference WAV.
        return _synth_styletts2(
            text,
            ref_audio_path=voice,
            out_path=out_path,
            speed=speed,
        )
    if provider == "indic_parler":
        # `voice` = natural-language description of the target speaker
        # (NOT a ref-WAV path). Empty → falls back to the default Sneha
        # description in `_synth_indic_parler`.
        return _synth_indic_parler(
            text,
            description=voice,
            out_path=out_path,
            speed=speed,
        )
    if provider == "cartesia":
        # Channel YAML declares `tts_language: hi` (or es/fr/etc.) for
        # non-English narration; passes through here as the `language`
        # kwarg. Default English.
        return _synth_cartesia(
            text,
            voice=voice,
            out_path=out_path,
            speed=speed,
            language=language,
        )
    raise ValueError(
        f"unknown TTS provider {provider!r} "
        "(choices: kokoro, f5_tts, chatterbox, styletts2, indic_parler, cartesia)"
    )
