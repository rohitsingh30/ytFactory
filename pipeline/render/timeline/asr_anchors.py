"""ASR-anchored section TimelineBuilder for the long engine.

Calls cloud whisper on the narrated wav to get word timestamps, then
matches each authored section/chapter title to its location in the
narration. Replaces the long_form.py / sports_doc.py "trust the
chunked-TTS chunk timings" approach with an actual ASR-verified
alignment.

Why ASR-anchored instead of just trusting authored offsets
----------------------------------------------------------

Pre-2026-05-14 the long engine derived section timings from
``synth_long_narration``'s reported chunk durations. That works when
TTS is single-pass per section but breaks down with:

- Chunk size mismatch (a section that's 800 chars gets split into 3
  chunks; the "section start_s" is the first chunk's start, but if
  the user later splits it differently the offset drifts).
- Cloud-TTS provider redeploys that change the silence between
  chunks (the join_silence_s field doesn't capture this).
- Hand-edited narration.wav after the fact (some channels do this
  for pronunciation fixes; the chunk timings then lie).

Whisper-anchoring the AUTHORED titles against the actual wav is the
robust fix — same approach ``sports_doc.py``'s
``_align_anchors_to_narration`` already uses for footage_plan
anchors. This plugin generalises it to all section starts.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from pipeline.beats import Word
from pipeline.render.contracts import (
    AudioResult,
    Segment,
    Timeline,
    TimelineBuilder,
    register_plugin,
)
from pipeline.render.telemetry_helpers import emit_timeline_telemetry

_logger = logging.getLogger(__name__)


# ---- per-word timing synthesis -----------------------------------------
#
# 2026-05-24 — synthesize uniform per-word timings on Segments that
# don't carry ASR-derived word timings. The long-form path skips
# ASR (it uses authored TTS chunk timings or the title-match anchor
# path), so every Segment shipped with ``words=None``. Downstream,
# the ``overlays.word_caption_pngs`` plugin detected the missing
# word timings and fell through to a "render the whole section as
# one PNG" path → one ~150-second-wide static text strip per section
# burned across the bottom of the frame for the entire 26-minute
# render (Bug C in
# data/critiques/i-ve-been-flying-…-845bdb0d.bugs.md).
#
# The shorts path (asr_beats) emits real Word objects from ASR and
# must NOT be synthesised over — synthesis only fires when
# ``words is None or len(words) == 0``.


def _synthesize_word_timings(
    text: str,
    start_s: float,
    end_s: float,
) -> list[Word]:
    """Split ``text`` on whitespace and distribute uniformly across
    ``[start_s, end_s]``.

    Tokens that are pure-punctuation (no alphanumeric content) are
    dropped — ASR streams occasionally emit standalone "/" or ","
    tokens and the synthesizer must not turn them into 0.5s floating
    glyphs. Punctuation attached to a word ("1990.", "10,000") is
    kept verbatim.

    Returns ``[]`` when text is empty/whitespace-only OR when the
    span is non-positive. Callers (``_ensure_word_timings`` /
    overlays.word_caption_pngs) must handle the empty case
    gracefully — a zero-duration segment can't have word timings.
    """
    span = float(end_s) - float(start_s)
    if span <= 0.0:
        return []
    raw_tokens = (text or "").split()
    # Drop pure-punctuation tokens (no alphanumerics).
    tokens = [t for t in raw_tokens if any(ch.isalnum() for ch in t)]
    if not tokens:
        return []
    per_word = span / len(tokens)
    out: list[Word] = []
    cursor = float(start_s)
    for i, tok in enumerate(tokens):
        # The final word ends EXACTLY at end_s to absorb float drift
        # across very long spans (otherwise a 26-minute section split
        # into 4000 words could drift by tens of milliseconds).
        word_end = float(end_s) if i == len(tokens) - 1 else cursor + per_word
        out.append(Word(text=tok, start=cursor, end=word_end))
        cursor = word_end
    return out


def _ensure_word_timings(segments: list[Segment]) -> list[Segment]:
    """For each segment, fill in synthesized per-word timings IF the
    segment doesn't already carry ASR-emitted word timings.

    Real ASR-emitted ``words`` (the short-engine ``asr_beats`` path)
    pass through verbatim — this function never replaces them.

    Mutates each segment IN PLACE and also returns the list so
    callers can use it inline at every ``return`` point in
    ``AsrAnchors.build``.
    """
    for seg in segments:
        if seg.words is None or len(seg.words) == 0:
            seg.words = _synthesize_word_timings(
                seg.text, seg.start_s, seg.end_s,
            )
    return segments


class AsrAnchors:
    """Whisper-anchored Timeline keyed by authored section/chapter ids.

    Output Segments have ``kind="section"`` (or ``"chapter"`` for
    overlay_timeline mode) and ``anchor_id`` matching the authored
    section ``id``. Visualize / overlay producers correlate against
    these ids without parsing.

    Falls back to chunked-TTS chunk_timings when whisper is unavailable
    AND ``audio.chunk_timings`` is populated — preserves long-form's
    pre-2026-05-14 behavior as a safety net.
    """

    def build(
        self,
        spec: Any,  # RenderSpec
        script: dict[str, Any],
        audio: AudioResult,
    ) -> Timeline:
        sections = script.get("sections") or script.get("chapters") or []
        if not sections:
            # Single-section fallback: one Segment covering the whole
            # narration. Matches the behavior of long_form when an
            # envelope ships without sections[].
            timeline = [Segment(
                start_s=0.0,
                end_s=audio.duration_s,
                text=script.get("narration", ""),
                anchor_id="full",
                kind="section",
            )]
            emit_timeline_telemetry(
                timeline,
                total_seconds=audio.duration_s,
                unanchored_count=0,
                anchor_method="single_section",
            )
            return _ensure_word_timings(timeline)

        kind = "chapter" if "chapters" in script else "section"
        anchors = [(s.get("title") or "").strip() for s in sections]

        # Cloud path first.
        try:
            from pipeline.asr_cloudrun import align_via_cloud  # noqa: PLC0415
            cloud_segs = align_via_cloud(
                audio.narration_path,
                mode="anchors",
                anchors=anchors,
            )
            if cloud_segs:
                # Re-attach the right kind + section body to each cloud-returned segment
                # (the cloud service returns kind="section", but we want chapter when
                # the script uses that key).
                for i, seg in enumerate(cloud_segs):
                    seg.kind = kind
                    if i < len(sections):
                        sec = sections[i]
                        # 2026-05-15 — accept ``text`` as a section-body
                        # alias. cosmosdecoded + several historyrecapped
                        # script variants populate ``text`` (not ``body``
                        # / ``narration``); the prior ``body`` /
                        # ``narration`` only chain emitted Segments with
                        # text="" → empty scene → ``longform_panels``
                        # raised ValueError("panel 0 missing 'scene'
                        # field") → 26 minutes of solid black on the
                        # 2026-05-15 cosmos hubble render (job
                        # f37bb01a). See
                        # tests/render/timeline/test_asr_anchors_section_text_fields.py.
                        seg.text = (
                            sec.get("body")
                            or sec.get("text")
                            or sec.get("narration")
                            or seg.text
                        )
                        seg.anchor_id = str(sec.get("id") or f"sec_{i:03d}")
                _logger.info("asr_anchors: cloud whisper anchored %d sections",
                             len(cloud_segs))
                emit_timeline_telemetry(
                    cloud_segs,
                    total_seconds=audio.duration_s,
                    unanchored_count=max(0, len(sections) - len(cloud_segs)),
                    anchor_method="cloud_anchors",
                )
                return _ensure_word_timings(cloud_segs)
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("CLOUDRUN_ASR_DISABLE_FALLBACK") == "1":
                raise
            _logger.warning("asr_anchors: cloud whisper failed (%s) — "
                            "falling back to local whisper", exc)

        # Local fallback — whisper_mlx via pipeline.asr.
        try:
            from pipeline.asr import transcribe  # noqa: PLC0415
            result = transcribe(audio.narration_path)
            words: list[dict] = []
            for seg in result.get("segments", []):
                for w in seg.get("words", []) or []:
                    words.append({
                        "text": (w.get("word") or w.get("text") or "").strip(),
                        "start_s": float(w.get("start", 0)),
                        "end_s": float(w.get("end", 0)),
                    })
            timeline = self._anchor_sections_to_words(sections, words, kind, audio.duration_s)
            emit_timeline_telemetry(
                timeline,
                total_seconds=audio.duration_s,
                unanchored_count=0,
                anchor_method="local_anchors",
            )
            return _ensure_word_timings(timeline)
        except Exception as exc:  # noqa: BLE001
            # Cloud whisper down + no laptop whisper available.
            # Fall back to chunk_timings if present, else equal-share.
            _logger.warning("asr_anchors: local whisper also unavailable (%s) — "
                            "using chunk_timings or equal-share", exc)
            timeline = self._fallback(sections, audio, kind)
            emit_timeline_telemetry(
                timeline,
                total_seconds=audio.duration_s,
                unanchored_count=len(sections),
                anchor_method="chunk_or_equal_fallback",
            )
            return _ensure_word_timings(timeline)

    def _anchor_sections_to_words(
        self,
        sections: list[dict],
        words: list[dict],
        kind: str,
        total_s: float,
    ) -> Timeline:
        # Find each section title's position in the word stream by
        # token-prefix match. First match wins. Same approach as
        # sports_doc._align_anchors_to_narration.
        #
        # 2026-05-15 (v16) — TWO-PASS computation. Pre-fix, end_s
        # defaulted to ``total_s`` and only narrowed if a LATER
        # section's anchor was found. When NO later anchor matched
        # (the common case — section titles are usually paraphrased
        # in narration, so first-6-words sliding-window match fails),
        # every section's end_s stayed at ``total_s`` → segments
        # overlapped → ``Segment.end_s - start_s`` produced
        # ``hold_s`` values like 1415s for a 1500s narration →
        # downstream ffmpeg rendered 42,456 frames for ONE panel
        # before the next started, the cloud-run JOB hit its 1-hour
        # wall, render killed.
        #
        # Surfaced by Pompeii long-form (job ba3e7578) on the 2026-
        # 05-15 v15 deploy: panels gen'd correctly (10/10) but
        # ``[seg ] 1/10 1415.2s zoom→1.08 (42456f)`` proved seg 0
        # owned all 1415 seconds → ffmpeg stuck on encode, JOB
        # cancelled at 38min in.
        #
        # Pinned by tests/render/timeline/test_asr_anchors_section_text_fields.py
        # ::AnchoredSegmentsAreNonOverlappingTest.

        # Pass 1: derive start_s for every section, monotonically.
        starts: list[float] = []
        prev_end = 0.0
        for i, sec in enumerate(sections):
            title = (sec.get("title") or "").strip()
            start_s = self._find_anchor_start(title, words, after_s=prev_end)
            if start_s is None:
                # Unanchored — fall back to equal-share offset.
                start_s = (i / len(sections)) * total_s
            # Monotonicity guard: never go backward in time. (Equal-
            # share fallback can technically produce an offset earlier
            # than prev_end if a previous section was anchored late;
            # clamp.)
            start_s = max(start_s, prev_end)
            starts.append(start_s)
            prev_end = start_s

        # Pass 2: end_s[i] = starts[i+1] (or total_s for last section).
        out: list[Segment] = []
        for i, sec in enumerate(sections):
            end_s = starts[i + 1] if i + 1 < len(sections) else total_s
            out.append(Segment(
                start_s=starts[i],
                end_s=end_s,
                # 2026-05-15 — accept ``text`` alias. See cloud-path
                # comment above for the cosmos hubble regression.
                text=sec.get("body") or sec.get("text") or sec.get("narration") or "",
                anchor_id=str(sec.get("id") or f"sec_{i:03d}"),
                kind=kind,
            ))
        return out

    def _find_anchor_start(
        self,
        anchor_text: str,
        words: list[dict],
        after_s: float = 0.0,
    ) -> float | None:
        if not anchor_text:
            return None
        anchor_tokens = anchor_text.lower().split()[:6]  # match first 6 words
        if not anchor_tokens:
            return None
        # Sliding-window contiguous match.
        for i in range(len(words) - len(anchor_tokens) + 1):
            window = [w.get("text", "").strip().lower() for w in words[i:i + len(anchor_tokens)]]
            if window == anchor_tokens and float(words[i].get("start_s", 0)) >= after_s:
                return float(words[i].get("start_s", 0))
        return None

    def _fallback(
        self,
        sections: list[dict],
        audio: AudioResult,
        kind: str,
    ) -> Timeline:
        # Use chunk_timings if available; else equal-share.
        if audio.chunk_timings and len(audio.chunk_timings) == len(sections):
            return [
                Segment(
                    start_s=start_s,
                    end_s=end_s,
                    # 2026-05-15 — ``text`` alias accepted; see _anchor
                    # path above for the cosmos hubble regression.
                    text=sections[i].get("body") or sections[i].get("text") or sections[i].get("narration") or "",
                    anchor_id=str(sections[i].get("id") or f"sec_{i:03d}"),
                    kind=kind,
                )
                for i, (start_s, end_s) in enumerate(audio.chunk_timings)
            ]
        # Equal-share fallback.
        n = len(sections)
        share = audio.duration_s / max(n, 1)
        return [
            Segment(
                start_s=i * share,
                end_s=(i + 1) * share,
                text=sections[i].get("body") or sections[i].get("text") or sections[i].get("narration") or "",
                anchor_id=str(sections[i].get("id") or f"sec_{i:03d}"),
                kind=kind,
            )
            for i in range(n)
        ]


register_plugin("timeline", "asr_anchors", AsrAnchors())
assert isinstance(AsrAnchors(), TimelineBuilder)


__all__ = ["AsrAnchors"]
