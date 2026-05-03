"""Source-text alignment.

Whisper transcribes TTS output but introduces small errors (e.g. "AITA"
becomes "Ada", or "four hundred dollars" gets dropped). We have the
authoritative source text — we just need timestamps. This module aligns
source words to Whisper words, replacing transcribed text while
preserving timestamps.

Principle #9 (DESIGN.md §14): never silently drop source words. When a
source word has no Whisper anchor (Whisper missed it), insert with a
timestamp interpolated from neighbors. The output covers EVERY source
word; captions never go missing.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .beats import Word


_TOKEN_RE = re.compile(r"\S+")
_AVG_WORD_S = 0.30  # fallback duration when extrapolating beyond anchors


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _strip_for_match(s: str) -> str:
    """Lowercase + strip punctuation for fuzzy matching."""
    return re.sub(r"[^\w]", "", s).lower()


def align_source_to_whisper(source_text: str, whisper_words: list[Word]) -> list[Word]:
    """Return a Word per source token, with timestamps from Whisper or
    interpolated where Whisper missed a token. The output length equals
    ``len(_tokenize(source_text))``.
    """
    src_tokens = _tokenize(source_text)
    if not src_tokens:
        return []
    if not whisper_words:
        # No timestamps at all — spread source evenly over a guessed span.
        total = len(src_tokens) * _AVG_WORD_S
        return [
            Word(
                text=t,
                start=i * _AVG_WORD_S,
                end=(i + 1) * _AVG_WORD_S,
            )
            for i, t in enumerate(src_tokens)
        ]

    src_keys = [_strip_for_match(t) for t in src_tokens]
    whi_keys = [_strip_for_match(w.text) for w in whisper_words]

    # Pass 1: anchor every source token we can directly to a Whisper timestamp.
    n = len(src_tokens)
    src_ts: list[tuple[float, float] | None] = [None] * n

    matcher = SequenceMatcher(a=whi_keys, b=src_keys, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                w = whisper_words[i1 + k]
                src_ts[j1 + k] = (w.start, w.end)
        elif op == "replace":
            n_a, n_b = i2 - i1, j2 - j1
            if n_a == 0 or n_b == 0:
                continue
            # Distribute n_b source tokens evenly over the time span the
            # n_a whisper tokens occupy.
            span_start = whisper_words[i1].start
            span_end = whisper_words[i2 - 1].end
            duration = max(span_end - span_start, _AVG_WORD_S * n_b)
            chunk = duration / n_b
            for k in range(n_b):
                t0 = span_start + chunk * k
                t1 = span_start + chunk * (k + 1)
                src_ts[j1 + k] = (t0, t1)
        # op == "insert": source tokens without a Whisper anchor → leave None,
        # filled in pass 2 by interpolation.
        # op == "delete": whisper tokens not in source → ignore (drop them).

    # Pass 2: fill remaining None entries by interpolating between neighbors.
    i = 0
    while i < n:
        if src_ts[i] is not None:
            i += 1
            continue
        # Find the contiguous run of None entries [run_start, run_end].
        run_start = i
        run_end = i
        while run_end + 1 < n and src_ts[run_end + 1] is None:
            run_end += 1

        # Anchor on the left: end of previous pinned, or 0.
        if run_start > 0 and src_ts[run_start - 1] is not None:
            t_prev = src_ts[run_start - 1][1]
        else:
            t_prev = 0.0

        # Anchor on the right: start of next pinned, or extrapolate forward.
        run_size = run_end - run_start + 1
        if run_end + 1 < n and src_ts[run_end + 1] is not None:
            t_next = src_ts[run_end + 1][0]
        else:
            t_next = t_prev + run_size * _AVG_WORD_S

        if t_next <= t_prev:
            t_next = t_prev + run_size * _AVG_WORD_S

        chunk = (t_next - t_prev) / run_size
        for k in range(run_size):
            idx = run_start + k
            src_ts[idx] = (t_prev + chunk * k, t_prev + chunk * (k + 1))

        i = run_end + 1

    # Build the final list. By construction, every src_ts entry is set.
    aligned: list[Word] = []
    for i in range(n):
        s, e = src_ts[i]  # type: ignore[misc]
        if e <= s:
            e = s + _AVG_WORD_S  # safety clamp
        aligned.append(Word(text=src_tokens[i], start=s, end=e))
    return aligned
