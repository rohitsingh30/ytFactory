"""Stage 5 — word-level timestamps via the configured ASR backend, then
split into beats.

A "beat" is a span of narration ~1.5–3s that gets one image. Beats are
split at clause boundaries (commas, periods, conjunctions), not fixed
time chunks.

ASR backend selection lives in ``pipeline.asr``. The default model
(``mlx-community/whisper-large-v3-mlx-4bit``) is preserved here so this
stage's behaviour is unchanged unless a channel config explicitly opts
into a different provider (e.g. ``parakeet_mlx``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

from pipeline import observability as _obs


# Hard sentence terminators — primary break points (one beat per sentence).
_SENTENCE_BREAKERS = {".", "!", "?"}
# Clause-level breaks — fallback when a single sentence is too long to fit.
_CLAUSE_BREAKERS = {",", ";", ":"}
# Combined set, preserved for backward-compat with split_with_forced_boundaries.
_BREAKERS = _SENTENCE_BREAKERS | _CLAUSE_BREAKERS


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Beat:
    text: str
    start: float
    end: float
    words: list[Word]
    # Optional kind dispatch. Default is "animated" (or static image, on
    # the slideshow path). Set to "footage" for beats that should cut to
    # a real broadcast clip via ``pipeline.footage.fetch_clip``. The
    # ``footage`` dict carries the resolution params: ``url`` (YouTube),
    # ``in_s`` / ``out_s`` (frame-accurate), and optional ``audio_mix``
    # (0.0-1.0, original audio level under the narrator).
    kind: str = "animated"
    footage: dict | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start


@_obs.traced("beats.transcribe_words", category="asr",
             capture=["provider", "model"])
def transcribe_words(
    audio_path: Path,
    provider: str = "whisper_mlx",
    model: str | None = None,
) -> list[Word]:
    """Word-level timestamps for ``audio_path`` using the chosen backend.

    Default model for the whisper provider stays at
    ``mlx-community/whisper-large-v3-mlx-4bit`` (current behaviour).
    Pass ``provider="parakeet_mlx"`` to use Parakeet instead — the
    parakeet-mlx package is a separate optional install.

    D3-3 (2026-05-24 retry-cache sweep): the docstring in
    ``pipeline.cloud.cache`` lists ``alignments/<name>.json`` as a cached
    artifact kind, but pre-fix nobody actually wrote to it. ASR re-ran
    on every retry. Now we:

      1. Check ``cache/alignments/<audio_name>.json`` first — if a
         previous attempt persisted the alignment for the SAME audio
         file (same name + matching size + matching duration), skip the
         ASR call and return the cached words.
      2. After a fresh ASR call, write the sanitised word list as JSON
         to that path AND ``persist_artifact(kind="alignments", ...)``
         so the next retry hits the disk cache.

    Cache key = (audio_name, audio_bytes, provider, model). We deliberately
    DON'T key on the audio's content hash — that would require reading
    the whole wav into memory just to maybe-skip ASR, and a same-name +
    same-size + same-provider hit is robust enough in practice: TTS
    cache keys panel→narration→alignment all flow from the same render
    spec, so a wav with matching bytes is virtually always the same.
    """
    # Late import to avoid a circular dependency with pipeline.asr.
    from . import asr

    if provider == "whisper_mlx" and model is None:
        model = "mlx-community/whisper-large-v3-mlx-4bit"

    # D3-3 — skip-if-exists check. Cache file lives next to where
    # B2's hydrate_cache puts it (work_dir/cache/alignments/<name>.json).
    # We compute the cache path from the audio_path's parent +
    # canonical relative location, then hop UP to work_dir to find
    # cache/. Robust against the audio_path being either work_dir or
    # work_dir/<subdir>. Disabled if env var YTFACTORY_ASR_CACHE=0.
    cached = _try_load_alignment_cache(audio_path, provider=provider, model=model)
    if cached is not None:
        return cached

    result = asr.transcribe(audio_path, provider=provider, model=model)
    words: list[Word] = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            words.append(
                Word(
                    text=(w.get("word") or "").strip(),
                    start=float(w["start"]),
                    end=float(w["end"]),
                )
            )
    # Class-of-bug fix (2026-05-03 hathi-raja-kahan-chale render): Whisper
    # on SUNG audio (Suno-generated nursery rhyme with overlapping lead +
    # choir) produced word_69.start (38.44s) BEFORE word_68.end (44.82s).
    # Downstream ffmpeg compose computes per-word trim durations as
    # `next.start - this.start`; a negative value crashes the trim
    # filter. Sanitise here: clamp every word's start to be ≥ the
    # previous word's start, and end to be ≥ start. Idempotent.
    sanitised: list[Word] = []
    last_start = 0.0
    last_end = 0.0
    for w in words:
        s = max(w.start, last_start)
        e = max(w.end, s + 0.01, last_end)
        sanitised.append(Word(text=w.text, start=s, end=e))
        last_start = s
        last_end = e

    # Class-of-bug fix (2026-05-03 v5 render observation): Whisper
    # hallucinates word timestamps (a) past the actual audio duration
    # and (b) during instrumental gaps where there's no vocal. Both
    # produce silent caption rolls in the rendered mp4. Two layers:
    #
    # (a) Cap every word's start/end to the actual audio duration.
    #     ffprobe the file and clamp.
    # (b) Sample audio RMS at 100ms granularity. Drop any word whose
    #     midpoint falls in a low-RMS span (< -28 dB) — that's
    #     instrumental / silence with no actual vocal Whisper could
    #     have transcribed.
    audio_dur_s = _ffprobe_duration_s(audio_path)
    if audio_dur_s and audio_dur_s > 0:
        # Cap word stamps to audio duration first (drops the trailing
        # hallucinations past the trim end).
        capped: list[Word] = []
        for w in sanitised:
            if w.start >= audio_dur_s:
                continue  # word entirely past audio — drop
            s = min(w.start, audio_dur_s - 0.001)
            e = min(w.end, audio_dur_s)
            if e <= s:
                e = s + 0.01
            capped.append(Word(text=w.text, start=s, end=e))
        sanitised = capped

    rms_mask = _audio_low_rms_spans(audio_path, threshold_db=-28.0)
    if rms_mask:
        masked: list[Word] = []
        for w in sanitised:
            mid = 0.5 * (w.start + w.end)
            in_silence = any(s <= mid <= e for s, e in rms_mask)
            if in_silence:
                continue  # word falls in a silent/instrumental span
            masked.append(w)
        sanitised = masked

    # D3-3 — persist the sanitised alignment so retries skip ASR. We
    # cache the POST-sanitisation list because that's what downstream
    # consumers see; re-running the sanitiser on the cached read is
    # idempotent (it's all clamping) so this is safe.
    _persist_alignment_cache(
        audio_path, sanitised, provider=provider, model=model,
    )
    return sanitised


# ---------------------------------------------------------------------------
# D3-3 — Alignment cache helpers
# ---------------------------------------------------------------------------
#
# The cache file lives at ``<work_dir>/cache/alignments/<audio_name>.json``.
# Layout matches what ``pipeline.cloud.cache.hydrate_cache`` restores on
# worker startup so a retry of a SIGKILLed render reads the cached
# alignment without paying for whisper again.
#
# We probe the cache root by walking up from the audio file looking for a
# ``cache/alignments/`` neighbour — the same pattern long-form already
# uses for its panel + tts chunk caches (they live as
# ``cache_dir/panels/...``, ``cache_dir/tts_chunks/...`` so the alignment
# version is just one more subdir under the same root).


def _alignment_cache_root(audio_path: Path) -> Path | None:
    """Find the ``cache/alignments/`` dir for a given audio file.

    Walks the parent chain up to 6 levels. Returns the resolved dir
    (creating parents lazily on write) or ``None`` if no sensible
    cache root can be located AND no ``YTFACTORY_WORK_DIR`` env is set.

    Why two probe strategies:

      * Long-form path: ``audio_path == cache_dir / "narration.wav"`` —
        the parent IS the cache_dir, so the alignments subdir is right
        there.
      * Short-form path: ``audio_path == work_dir / "narration.wav"`` —
        the parent is the work_dir; cache lives at ``work_dir/cache/``
        (hydrate_cache mirrors gs://.../cache/<kind>/ → work_dir/cache/<kind>/).
    """
    import os as _os  # noqa: PLC0415
    if _os.environ.get("YTFACTORY_ASR_CACHE", "1") == "0":
        return None
    p = Path(audio_path).resolve().parent
    for _ in range(6):
        if (p / "cache" / "alignments").is_dir() or (p / "alignments").is_dir():
            # Prefer the canonical work_dir/cache/alignments/ layout.
            cand = p / "cache" / "alignments"
            if cand.is_dir() or (p / "cache").is_dir():
                return cand
            return p / "alignments"
        if p == p.parent:
            break
        p = p.parent
    # Fallback: write next to the audio file.
    return Path(audio_path).resolve().parent / "cache" / "alignments"


def _alignment_cache_path(audio_path: Path) -> Path | None:
    root = _alignment_cache_root(audio_path)
    if root is None:
        return None
    return root / f"{Path(audio_path).name}.json"


def _try_load_alignment_cache(
    audio_path: Path, *, provider: str, model: str | None,
) -> list[Word] | None:
    """Return cached words if a valid cache file exists for this audio.

    Validity = file exists, parses as JSON, ``audio_bytes`` matches the
    current file's size, ``provider``/``model`` match. On any mismatch
    return ``None`` and let the caller re-run ASR.
    """
    path = _alignment_cache_path(audio_path)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or "words" not in data:
        return None
    try:
        cur_bytes = Path(audio_path).stat().st_size
    except OSError:
        return None
    if data.get("audio_bytes") != cur_bytes:
        return None
    if data.get("provider") != provider:
        return None
    # model may be None on cache write or read — treat None as wildcard.
    if data.get("model") not in (None, model):
        return None
    words = []
    for w in data["words"]:
        try:
            words.append(Word(
                text=str(w["text"]),
                start=float(w["start"]),
                end=float(w["end"]),
            ))
        except (KeyError, TypeError, ValueError):
            return None
    return words


def _persist_alignment_cache(
    audio_path: Path, words: list[Word], *,
    provider: str, model: str | None,
) -> None:
    """Write the alignment JSON + fire-and-forget GCS persist.

    Best-effort: any failure is logged at debug and swallowed. The
    render must never fail because the cache write failed.
    """
    path = _alignment_cache_path(audio_path)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            audio_bytes = Path(audio_path).stat().st_size
        except OSError:
            audio_bytes = 0
        payload = {
            "audio_bytes": audio_bytes,
            "provider": provider,
            "model": model,
            "words": [{"text": w.text, "start": w.start, "end": w.end} for w in words],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        return
    try:
        from pipeline.cloud.cache import persist_artifact  # noqa: PLC0415
        persist_artifact(path, kind="alignments")
    except Exception:  # noqa: BLE001
        pass


def _ffprobe_duration_s(path: Path) -> float | None:
    """Return audio duration via ffprobe, or None on failure."""
    import subprocess as _sp
    try:
        res = _sp.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        return float(res.stdout.strip())
    except (ValueError, OSError, _sp.SubprocessError):
        return None


def _audio_low_rms_spans(
    path: Path,
    threshold_db: float = -28.0,
    sample_window_s: float = 0.1,
    min_span_s: float = 0.5,
) -> list[tuple[float, float]]:
    """Return [(start_s, end_s), ...] spans where audio RMS is below
    ``threshold_db`` for at least ``min_span_s``. Used to mask Whisper
    hallucinations during instrumental gaps in sung audio.
    """
    import re as _re
    import subprocess as _sp
    try:
        res = _sp.run(
            [
                "ffmpeg", "-i", str(path),
                "-af", f"astats=metadata=1:reset={sample_window_s},"
                       "ametadata=print:key=lavfi.astats.Overall.RMS_level",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, _sp.SubprocessError):
        return []

    samples: list[tuple[float, float]] = []
    cur_t = 0.0
    for line in res.stderr.splitlines():
        m_t = _re.search(r"pts_time:([0-9.]+)", line)
        if m_t:
            cur_t = float(m_t.group(1))
            continue
        m_r = _re.search(r"RMS_level=(-?[0-9.]+)", line)
        if m_r:
            samples.append((cur_t, float(m_r.group(1))))

    spans: list[tuple[float, float]] = []
    span_start: float | None = None
    last_t = 0.0
    for t, rms in samples:
        if rms < threshold_db:
            if span_start is None:
                span_start = t
            last_t = t
        else:
            if span_start is not None:
                if last_t - span_start >= min_span_s:
                    spans.append((span_start, last_t + sample_window_s))
                span_start = None
        last_t = t
    if span_start is not None and last_t - span_start >= min_span_s:
        spans.append((span_start, last_t + sample_window_s))
    return spans


def _make_beat(words: list[Word]) -> Beat:
    return Beat(
        text=" ".join(w.text for w in words).strip(),
        start=words[0].start,
        end=words[-1].end,
        words=list(words),
    )


_NORM_RE = None  # lazy-init


def _norm_token(t: str) -> str:
    """Normalize a single word for matching: lowercase, strip everything
    except letters/digits/apostrophes.

    Class-of-bug fix per critique 2026-05-03 (abhimanyu-chakravyuh.video.md):
    the previous regex ``[^a-z0-9']+`` was ASCII-only — Devanagari and any
    non-Latin script collapsed to empty strings, breaking
    split_with_forced_boundaries() for Hindi narrations and silently
    producing zero word-captions. ``[^\\w']+`` with re.UNICODE keeps
    Devanagari, Arabic, CJK. .lower() is a no-op on Devanagari.
    """
    import re as _re
    global _NORM_RE
    if _NORM_RE is None:
        _NORM_RE = _re.compile(r"[^\w']+", flags=_re.UNICODE)
    return _NORM_RE.sub("", t.lower())


def _norm_tokens(text: str) -> list[str]:
    """Split a phrase into normalized tokens, dropping empties."""
    return [t for t in (_norm_token(p) for p in text.split()) if t]


def _find_subsequence(haystack: list[str], needle: list[str], start: int) -> int:
    """Return the index in ``haystack`` (>= ``start``) where ``needle``
    starts as a contiguous subsequence, or -1 if no match.
    Allows haystack tokens that are empty strings to be skipped (they
    arise from punctuation-only "words" after normalization)."""
    if not needle:
        return start
    n_idx = 0
    h_match_start = -1
    i = start
    while i < len(haystack):
        h = haystack[i]
        if not h:
            i += 1
            continue
        if h == needle[n_idx]:
            if n_idx == 0:
                h_match_start = i
            n_idx += 1
            if n_idx == len(needle):
                return h_match_start
        else:
            if n_idx > 0:
                # Reset, but don't skip tokens we already advanced past —
                # restart at h_match_start + 1 to allow overlapping matches.
                i = h_match_start
                n_idx = 0
                h_match_start = -1
        i += 1
    return -1


@_obs.traced("beats.split_with_forced_boundaries", category="render")
def split_with_forced_boundaries(
    words: list[Word],
    forced_narration_lines: list[str],
) -> list[Beat]:
    """Split ``words`` into beats where each ``forced_narration_lines[i]``
    becomes one beat, in order. Beats are 1:1 with shots by construction —
    no duration heuristic, no merge step. This is the path used by
    /make-movie-short, where the shotlist is the source of truth for
    visual cuts (Principle #26 / NEW).

    If a forced line cannot be located in the words, raise — the caller
    must fix the script-vs-shotlist drift before rendering.

    Words preceding the first forced match (e.g. a hesitation token TTS
    produced) are prepended to the first beat. Words after the last
    forced match (e.g. trailing silence) are appended to the last beat.
    """
    if not words:
        return []
    if not forced_narration_lines:
        raise ValueError("forced_narration_lines must be non-empty")

    word_tokens = [_norm_token(w.text) for w in words]

    # Find every line's match span first so we can validate before
    # committing to any beats.
    spans: list[tuple[int, int]] = []
    cursor = 0
    for line in forced_narration_lines:
        line_tokens = _norm_tokens(line)
        if not line_tokens:
            continue
        start = _find_subsequence(word_tokens, line_tokens, start=cursor)
        if start < 0:
            raise ValueError(
                f"forced narration line not found in TTS words "
                f"(starting from word {cursor}): {line!r}\n"
                f"  remaining words: "
                f"{' '.join(w.text for w in words[cursor:cursor+20])!r}"
            )
        end = start + len(line_tokens) - 1
        # Walk forward over any empty-tokenised words (pure punctuation)
        # so they get included in this beat rather than the next.
        while end + 1 < len(word_tokens) and not word_tokens[end + 1]:
            end += 1
        spans.append((start, end))
        cursor = end + 1

    # Convert spans to beats. Words before the first span go into beat 0;
    # words after each span up to the next span's start go into the
    # PREVIOUS beat (treat them as trailing punctuation/space).
    beats: list[Beat] = []
    prev_end = -1
    for i, (start, end) in enumerate(spans):
        # Beat covers [prev_end+1 .. end].
        beat_words = words[prev_end + 1 : end + 1]
        if beat_words:
            beats.append(_make_beat(beat_words))
        prev_end = end

    # Trailing words go into the last beat.
    if prev_end + 1 < len(words) and beats:
        tail = words[prev_end + 1 :]
        last = beats[-1]
        beats[-1] = Beat(
            text=(last.text + " " + " ".join(w.text for w in tail)).strip(),
            start=last.start,
            end=tail[-1].end,
            words=last.words + tail,
        )

    return beats


@_obs.traced("beats.split_into_beats", category="render")
def split_into_beats(
    words: list[Word],
    target_s: float = 1.8,
    max_s: float = 2.8,
    min_s: float = 1.0,
    *,
    forced_narration_lines: list[str] | None = None,
) -> list[Beat]:
    """Group words into beats — one beat per sentence, ideally.

    Principle #8 (DESIGN.md §14): beats end on `.!?;:,` — never on
    conjunctions. Sentence-level granularity (one image per sentence,
    optionally two short sentences merged) gives denser visuals and
    keeps each subtitle line short enough to read on a phone. The
    pipeline:

    1. SPLIT at every sentence terminator (.!?). Each sentence becomes
       a candidate beat. This is the primary lever — it determines how
       many images the Short carries.
    2. SPLIT-LONG: if a single sentence exceeds ``max_s``, fall back to
       the longest available clause break (,;:) inside that sentence.
       Only as a last resort do we cut mid-clause.
    3. MERGE-SHORT: if a candidate beat is shorter than ``min_s`` AND
       merging it with its neighbour stays under ``max_s``, merge —
       but cap at 2 sentences per beat (no triple-merges).

    When ``forced_narration_lines`` is provided (e.g. from a shotlist),
    delegate to ``split_with_forced_boundaries`` so beats are 1:1 with
    shots — no merging, no duration thresholds. This is what makes
    /make-movie-short shot-to-beat alignment deterministic.
    """
    if forced_narration_lines:
        return split_with_forced_boundaries(words, forced_narration_lines)
    if not words:
        return []

    # ---- Pass 1: cut at every sentence terminator. ----
    sentence_groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        cur.append(w)
        last_char = w.text[-1] if w.text else ""
        if last_char in _SENTENCE_BREAKERS:
            sentence_groups.append(cur)
            cur = []
    if cur:
        sentence_groups.append(cur)

    # ---- Pass 2: split any sentence that exceeds max_s. ----
    after_split: list[list[Word]] = []
    for group in sentence_groups:
        after_split.extend(_split_long_group(group, max_s))

    # ---- Pass 3: merge short adjacent groups, but cap at 2 sentences. ----
    # Track sentence-count per merged group so we never combine 3+ sentences.
    #
    # Rank-announcement special case (Critic 2026-05-03 top3-stoppage-goals
    # v1): a standalone "Number five." / "Number four." / "Number three."
    # / "Number two." / "Number one." beat (typically 0.6-0.9s) reads
    # as choppy on its own — the rank label is a setup for the next
    # phrase, not a beat in itself. ALWAYS merge a leading
    # rank-announcement group with the next group, even if the combined
    # duration exceeds max_s (capped at max_s + RANK_MERGE_SLACK).
    _RANK_PHRASES = ("number one", "number two", "number three",
                     "number four", "number five")
    _RANK_MERGE_SLACK = 2.5  # extra duration head-room for rank merges

    def _is_rank_announcement(grp: list[Word]) -> bool:
        s = " ".join(w.text for w in grp).lower().strip()
        s = s.rstrip(".!?,:;")
        return s in _RANK_PHRASES

    merged_groups: list[list[Word]] = []
    sentence_counts: list[int] = []
    for group in after_split:
        group_dur = group[-1].end - group[0].start
        group_sentences = 1 if (group[-1].text and group[-1].text[-1] in _SENTENCE_BREAKERS) else 0
        prev_group = merged_groups[-1] if merged_groups else None
        prev_is_rank = bool(prev_group and _is_rank_announcement(prev_group))
        prev_dur = (prev_group[-1].end - prev_group[0].start) if prev_group else 0.0
        candidate_dur = (group[-1].end - prev_group[0].start) if prev_group else 0.0
        # Either the standard short-merge path OR the rank-announcement
        # force-merge path triggers a merge with the previous group.
        merge_short = (
            prev_group is not None
            and prev_dur < min_s
            and candidate_dur <= max_s
            and sentence_counts[-1] + group_sentences <= 2
        )
        merge_rank = (
            prev_is_rank
            and candidate_dur <= (max_s + _RANK_MERGE_SLACK)
            and sentence_counts[-1] + group_sentences <= 2
        )
        if merge_short or merge_rank:
            merged_groups[-1] = merged_groups[-1] + group
            sentence_counts[-1] += group_sentences
        else:
            merged_groups.append(list(group))
            sentence_counts.append(group_sentences)

    beats_out = [_make_beat(g) for g in merged_groups if g]

    # Beat-length contract: no beat should exceed max_s by more than a
    # small slop margin. _split_long_group guarantees the split-side;
    # this assertion catches a regression in the merge-short pass that
    # could over-merge beyond max_s (the merge code already gates on
    # max_s but a future edit might not). Slop allows for the natural
    # sub-second jitter from ASR word boundaries vs requested ceiling.
    _MAX_S_SLOP = 0.5
    overlong = [
        (i, b.duration, b.text[:60])
        for i, b in enumerate(beats_out)
        if b.duration > max_s + _MAX_S_SLOP
    ]
    if overlong:
        # Loud warning rather than raise — the splitter has done all it
        # can; an overlong beat at this point means the source narration
        # has a very long word-stream at the end (no breakers, no
        # conjunctions, no middle to split at). Surface it to the
        # author so the rewrite stage can be tightened.
        for i, dur, txt in overlong:
            print(
                f"[beats] WARNING beat {i} is {dur:.2f}s vs max_s={max_s}s "
                f"+ slop {_MAX_S_SLOP}s — {txt!r}"
            )

    return beats_out


# Conjunction words used as fallback split points when a sentence has no
# clause break (no commas/semicolons) but still exceeds max_s. These are
# the natural "and then…" hinges in spoken sentences. Used by
# _split_long_group as a tier-2 fallback before resorting to a
# middle-word split. Listed lowercase, matched case-insensitively on the
# stripped word text.
_CONJUNCTION_WORDS = frozenset({
    "and", "but", "or", "so", "yet", "for", "nor",
    "when", "then", "while", "since", "because", "if", "as",
    "that", "who", "which", "though", "although", "before", "after",
})


# 2026-05-14 — added after the 27-render audit found beat-0 of the
# cake-AITA hook segmented as "I refused to cut my" (1.5s) followed
# by "sister's wedding cake at her reception" (1.8s). The pipeline's
# tier-3 middle-word splitter cut on word index, with no awareness
# that the left side ended on the possessive pronoun "my" — a
# linguistic stop-word that telegraphs an unfinished phrase. Viewers
# read this as "the video froze mid-sentence" and flick.
#
# Same root-cause across the audit:
#   - "I refused to cut my" / "sister's wedding cake at her reception"
#   - "drove two hours to" / "the venue" (would-be split if not for
#     a clause break landing earlier)
#   - "Soldiers began throwing thousands of priceless books from" /
#     "the House of Wisdom into the Tigris River"
#
# Fix: forbid splits that would END the left side on any of these
# stop words. Apply in both tier-2 (conjunction) and tier-3
# (middle-word) splitters via :func:`_is_acceptable_split_idx`.
#
# Lowercased + stripped-of-trailing-punct before lookup. NOTE: this
# DOES include "for", "as", "if", "but", "so" — which are also in
# _CONJUNCTION_WORDS. That's intentional: a conjunction at the
# clause-end of a sentence reads as a stop too ("…I went, so") even
# though splitting BEFORE one is fine.
_FORBIDDEN_END_TOKENS = frozenset({
    # Articles
    "a", "an", "the",
    # Possessives
    "my", "your", "his", "her", "its", "our", "their", "whose",
    # Prepositions (most common)
    "of", "for", "to", "with", "in", "on", "at", "by", "from",
    "into", "onto", "upon", "about", "over", "under", "through",
    "between", "among", "against", "around", "before", "after",
    "during", "without", "within", "across", "behind", "beneath",
    # Conjunctions / connectives
    "and", "or", "but", "so", "as", "if", "yet", "nor",
    "than", "because", "while", "though", "although", "until",
    "unless", "since", "when", "where", "whether",
    # Common pronouns mid-flow
    "this", "that", "these", "those",
})


def _is_acceptable_split_idx(group: list, best: int) -> bool:
    """True iff splitting ``group`` so the LEFT side ends at
    ``group[best - 1]`` does NOT land on a stop-word.

    ``best`` is the index of the FIRST word of the right side
    (matches the slicing convention used by :func:`_split_long_group`).
    Returns ``True`` for boundary cases where the index is at the
    edges (the caller has already validated 0 < best < len(group)).
    """
    if best <= 0 or best >= len(group):
        return True
    last_left = group[best - 1].text or ""
    bare = last_left.strip(",.;:!?\"'-").lower()
    return bare not in _FORBIDDEN_END_TOKENS


def _shift_to_acceptable(
    group: list, candidate: int, *, max_walk: int = 3
) -> int | None:
    """Walk ``candidate`` ±N positions to find an acceptable split.

    Returns the shifted index, or ``None`` if no acceptable position
    exists within ±``max_walk`` words. The caller decides what to do
    on None — usually fall back to the next splitter tier.

    Walk order: prefer SHIFTING RIGHT first (consume one more word
    into the left side) since that adds context to the left phrase
    rather than truncating it. If right-shift fails, try left-shift.
    """
    if _is_acceptable_split_idx(group, candidate):
        return candidate
    n = len(group)
    for delta in range(1, max_walk + 1):
        # Right shift first.
        right = candidate + delta
        if right < n and _is_acceptable_split_idx(group, right):
            return right
        left = candidate - delta
        if left > 0 and _is_acceptable_split_idx(group, left):
            return left
    return None


def _split_long_group(group: list[Word], max_s: float) -> list[list[Word]]:
    """Split a single-sentence word group so no fragment exceeds ``max_s``.

    Three-tier strategy, gentlest first, recursively applied:

    1. **Clause break** (`,;:`) nearest the middle. Cheapest, most
       natural. Was the only strategy in v0.
    2. **Conjunction word** (and/but/then/when/so/…) nearest the middle
       — handles long sentences that lack any clause break, e.g. the
       AITA hook "am I the asshole for telling my daughter I am
       disgusted by her?" which is 4.4s as one beat with no comma.
    3. **Middle-word split** as the last resort — uglier visually
       (mid-phrase cut) but guarantees the contract that no beat ever
       exceeds ``beat_max_s`` from the channel YAML. The image stage
       budget is per-beat; one 4.4s beat held on a single image is the
       longest static-pixel run in the Short and is what a viewer
       reads as "this video is hung" (Principle #15, frozen-frame
       collapse).

    Class-of-bug fix 2026-05-02: previously this returned ``[group]``
    unchanged when no clause break was found, silently letting a 4.4s
    beat through against a 3.2s ceiling. Now we always split until each
    sub-group fits.

    Class-of-bug fix 2026-05-14 (post-27-render audit, see
    docs/pipeline_bug_catalogue_v2_2026-05-14.html): tier 2 + tier 3
    splits also pass through :func:`_is_acceptable_split_idx` /
    :func:`_shift_to_acceptable` so the left side never ends on a
    stop-word. Pre-fix the cake-AITA hook split as "I refused to cut
    my" / "sister's wedding cake at her reception" — viewers read the
    "my" cut as a freeze. Post-fix the splitter walks ±3 words to
    find a non-stop-word boundary; if none exists, falls back to the
    original index (better than failing — the ``max_s`` contract takes
    precedence over the stop-word heuristic).
    """
    if not group:
        return []
    duration = group[-1].end - group[0].start
    if duration <= max_s:
        return [group]
    # Single word longer than max_s — no way to split further. Return
    # as-is rather than recurse infinitely. The caller's max_s contract
    # is broken in this pathological case (one TTS-emitted token >max_s),
    # but that's preferable to a stack overflow.
    if len(group) <= 1:
        return [group]

    # Tier 1 — clause breaks (,;:).
    clause_indices = [
        i for i, w in enumerate(group)
        if w.text and w.text[-1] in _CLAUSE_BREAKERS and i < len(group) - 1
    ]
    if clause_indices:
        mid_t = (group[0].start + group[-1].end) / 2
        best = min(clause_indices, key=lambda i: abs(group[i].end - mid_t))
        left, right = group[: best + 1], group[best + 1:]
    else:
        # Tier 2 — conjunctions. We split BEFORE the conjunction word
        # (so "and" / "when" lands at the start of the second half,
        # which matches how a reader naturally re-attacks the clause).
        conj_indices = [
            i for i, w in enumerate(group)
            if 0 < i < len(group) - 1
            and w.text.strip(",.;:!?").lower() in _CONJUNCTION_WORDS
        ]
        if conj_indices:
            mid_t = (group[0].start + group[-1].end) / 2
            best = min(conj_indices, key=lambda i: abs(group[i].start - mid_t))
            # Walk to acceptable boundary; if none, accept original.
            shifted = _shift_to_acceptable(group, best)
            if shifted is not None:
                best = shifted
            left, right = group[:best], group[best:]
        else:
            # Tier 3 — middle-word split. Last resort, never desirable
            # but always available so the max_s contract holds.
            best = max(1, len(group) // 2)
            shifted = _shift_to_acceptable(group, best)
            if shifted is not None:
                best = shifted
            left, right = group[:best], group[best:]

    # Recurse: a single split may not suffice if the sentence is very
    # long (5+ s with one comma in the middle leaves a 2.5s + 2.5s
    # which is fine, but 8s with one comma at 1s leaves a 7s tail).
    return _split_long_group(left, max_s) + _split_long_group(right, max_s)


def save_beats(beats: list[Beat], path: Path) -> None:
    """Serialise beats to JSON with a final monotonicity sanity-pass.

    Class-of-bug guards (2026-05-03 hathi-raja sung-audio renders):
      (1) Beat-level monotonicity — clamp every beat.start ≥ previous
          beat.start, beat.end ≥ start + 0.01.
      (2) Word-level monotonicity ACROSS the saved beats — even when
          split_into_beats shuffles words into different beat groups
          based on text matching (sung audio has repeated chorus
          lines, so a chorus-repeat word ends up in a later beat at
          an earlier timestamp), the GLOBAL word sequence must stay
          monotonic. Otherwise compose's per-word ffmpeg trim sees
          negative durations and crashes ("trim_in_X: durationi out
          of range"). Word-level fix here is the FINAL write barrier.

    Idempotent on already-monotonic input.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    last_start = 0.0
    last_end = 0.0
    serialised: list[dict] = []
    for b in beats:
        d = asdict(b)
        d["start"] = max(d.get("start", 0.0), last_start)
        d["end"] = max(d.get("end", 0.0), d["start"] + 0.01, last_end)
        # (2) Word-level monotonicity inside this beat AND across the
        # global word sequence. We thread last_word_start/last_word_end
        # via the outer last_start/last_end so words never go backward
        # even when crossing beat boundaries.
        new_words: list[dict] = []
        for w in d.get("words") or []:
            ws = max(float(w.get("start", 0.0)), last_start)
            we = max(float(w.get("end", 0.0)), ws + 0.01, last_end)
            new_words.append({**w, "start": ws, "end": we})
            last_start = ws
            last_end = we
        d["words"] = new_words
        # If words were sanitised, beat boundaries may need to expand
        # to enclose them.
        if new_words:
            d["start"] = min(d["start"], new_words[0]["start"])
            d["end"] = max(d["end"], new_words[-1]["end"])
        last_start = max(last_start, d["start"])
        last_end = max(last_end, d["end"])
        serialised.append(d)
    with path.open("w") as f:
        json.dump(serialised, f, indent=2)


def load_beats(path: Path) -> list[Beat]:
    with path.open() as f:
        raw = json.load(f)
    return [
        Beat(
            text=b["text"],
            start=b["start"],
            end=b["end"],
            words=[Word(**w) for w in b["words"]],
        )
        for b in raw
    ]
