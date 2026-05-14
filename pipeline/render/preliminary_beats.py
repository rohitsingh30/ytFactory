"""Pre-TTS beat structure derived from a per-slug shotlist.

The Shorts renderer is strictly sequential by default:

    TTS → narration.wav
      → ASR (whisper word-timestamps on the wav)
        → beats (one per shotlist line, via
          ``pipeline.beats.split_with_forced_boundaries``)
          → prompts.json (LLM prompt-author against beats)
            → images (one per beat)
              → compose

Every downstream stage hard-depends on real word timestamps. For the
shotlist-driven path that Phase-2 stage-overlap targets, however,
the **beat texts** are knowable BEFORE TTS runs — they're the
``shots[].narration_line`` (+ ``closer.narration_line``) values
authored by the make_* skills.

This module exposes two helpers:

* :func:`build_preliminary_beats` — one ``Beat`` per shotlist line,
  with synthetic equal-spaced timing. Image-gen only reads
  ``beat.text`` (and prints ``beat.duration`` cosmetically), so the
  synthetic timings don't affect prompt or image quality.
* :func:`narration_aligns_with_shotlist` — pre-checks that EVERY
  shotlist line is a contiguous-token match in the normalised
  narration, using the SAME ``_norm_token`` + ``_find_subsequence``
  logic ``pipeline.beats.split_with_forced_boundaries`` will use
  later against ASR words. If this returns False, the strict-
  sequential renderer would have raised ``ValueError`` at beat-
  splitting time anyway — we just fail the overlap gate up-front so
  we don't waste cloud cost generating images that the render can't
  use.

Why this lives in its own module
================================

Both helpers are small and pure (no I/O outside reading the
shotlist). Putting them in :mod:`pipeline.render.shorts` would force
every test that touches them to also import the renderer's heavy
diffuser/torch/whisper preamble. Keeping them standalone keeps unit
tests fast and lets future ``footage_only`` / ``sports_doc`` ports
share the same code without circular imports.

Channel-dir scoping (slug-collision safety)
-------------------------------------------

The legacy renderer-internal helper
:func:`pipeline.render.shorts._scan_intermediate` scans EVERY
channel folder under the repo root for the first matching slug.
That's fine for the renderer (which already knows which channel
it's rendering), but it's a slug-collision risk if two channels
ship the same slug. :func:`build_preliminary_beats` accepts an
explicit ``channel_dir`` and only looks under that subtree.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.beats import (
    Beat,
    _find_subsequence,
    _norm_token,
    _norm_tokens,
)


__all__ = [
    "build_preliminary_beats",
    "narration_aligns_with_shotlist",
    "load_shotlist_lines",
]


def load_shotlist_lines(channel_dir: Path, slug: str) -> list[str] | None:
    """Find ``<channel_dir>/.../shotlist/<slug>.json`` (recursive
    glob, so nested ``<channel>/<niche>/shotlist/`` layouts work)
    and return the per-shot ``narration_line`` values plus
    ``closer.narration_line``, in order.

    Returns ``None`` when no shotlist exists, the file is unreadable,
    or every line is empty after stripping. Returns ``[]`` only when
    the JSON parsed but had a ``shots`` array of length zero AND no
    closer (caller treats that as "no shotlist content available" —
    the same as ``None``).

    Channel-scoped (NOT repo-wide) — see module docstring for why.
    """
    if not channel_dir.is_dir():
        return None
    matches = list(channel_dir.rglob(f"shotlist/{slug}.json"))
    if not matches:
        return None
    sp = matches[0]
    try:
        data = json.loads(sp.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    lines: list[str] = []
    for shot in data.get("shots") or []:
        line = (shot.get("narration_line") or "").strip()
        if line:
            lines.append(line)
    closer = data.get("closer") or {}
    closer_line = (closer.get("narration_line") or "").strip()
    if closer_line:
        lines.append(closer_line)
    return lines or None


def build_preliminary_beats(
    channel_dir: Path,
    slug: str,
    *,
    default_beat_s: float = 2.0,
) -> list[Beat] | None:
    """Build a synthetic-timing beat list from the per-slug shotlist
    so the LLM prompt-author + image-gen branches can run BEFORE
    TTS+ASR produce real word timestamps.

    Each shotlist line becomes one ``Beat`` whose ``text`` is the
    raw shotlist line, ``start`` / ``end`` are equal-spaced at
    ``default_beat_s`` (for printing only — the prompt author uses
    ``beat.duration`` cosmetically), and ``words`` is empty.

    Returns ``None`` when no usable shotlist exists. Caller MUST
    treat that as "overlap not eligible — fall back to strict
    sequential".

    The ``Beat`` objects this produces are NOT a substitute for the
    real beats built post-ASR — they're a parallel branch that gets
    validated against the real beats before image_paths are reused
    by compose. See
    :func:`pipeline.render.shorts.make_short_impl` for the
    validation step.
    """
    lines = load_shotlist_lines(channel_dir, slug)
    if not lines:
        return None
    beats: list[Beat] = []
    for i, line in enumerate(lines):
        start = i * default_beat_s
        end = start + default_beat_s
        beats.append(Beat(text=line, start=start, end=end, words=[]))
    return beats


def narration_aligns_with_shotlist(
    narration: str,
    shot_lines: list[str],
) -> bool:
    """True iff every shot line is a contiguous-token match in
    ``narration``, in order, no overlap.

    Uses the SAME ``_norm_token`` + ``_find_subsequence`` logic
    :func:`pipeline.beats.split_with_forced_boundaries` will use
    later against ASR-transcribed words, so a True return here is a
    strong signal that the post-TTS beat split will succeed.

    Empty ``shot_lines`` returns ``False`` (no work to align).
    Empty ``narration`` returns ``False`` (no haystack).

    The caller is expected to pass narration ALREADY normalised
    via :func:`pipeline.audio.normalize_for_tts` — that's the same
    string the renderer feeds into TTS+ASR, so token matching
    behaves identically to the post-TTS path.
    """
    if not shot_lines:
        return False
    haystack = [_norm_token(p) for p in narration.split() if p.strip()]
    if not haystack:
        return False
    cursor = 0
    for line in shot_lines:
        needle = _norm_tokens(line)
        if not needle:
            continue
        idx = _find_subsequence(haystack, needle, start=cursor)
        if idx < 0:
            return False
        cursor = idx + len(needle)
    return True
