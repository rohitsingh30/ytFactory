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

from pipeline.render.contracts import (
    AudioResult,
    Segment,
    Timeline,
    TimelineBuilder,
    register_plugin,
)

_logger = logging.getLogger(__name__)


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
            return [Segment(
                start_s=0.0,
                end_s=audio.duration_s,
                text=script.get("narration", ""),
                anchor_id="full",
                kind="section",
            )]

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
                        seg.text = sec.get("body") or sec.get("narration") or seg.text
                        seg.anchor_id = str(sec.get("id") or f"sec_{i:03d}")
                _logger.info("asr_anchors: cloud whisper anchored %d sections",
                             len(cloud_segs))
                return cloud_segs
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
            return self._anchor_sections_to_words(sections, words, kind, audio.duration_s)
        except Exception as exc:  # noqa: BLE001
            # Cloud whisper down + no laptop whisper available.
            # Fall back to chunk_timings if present, else equal-share.
            _logger.warning("asr_anchors: local whisper also unavailable (%s) — "
                            "using chunk_timings or equal-share", exc)
            return self._fallback(sections, audio, kind)

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
        out: list[Segment] = []
        prev_end = 0.0
        for i, sec in enumerate(sections):
            title = (sec.get("title") or "").strip()
            start_s = self._find_anchor_start(title, words, after_s=prev_end)
            if start_s is None:
                # Unanchored section — fall back to equal-share offset
                # so the timeline is monotonic. The visualize plugin
                # will still show something for this section.
                start_s = (i / len(sections)) * total_s
            # End = next section's start (or total duration for the last).
            end_s = total_s
            for next_i in range(i + 1, len(sections)):
                next_title = (sections[next_i].get("title") or "").strip()
                next_start = self._find_anchor_start(next_title, words, after_s=start_s)
                if next_start is not None:
                    end_s = next_start
                    break
            out.append(Segment(
                start_s=start_s,
                end_s=end_s,
                text=sec.get("body") or sec.get("narration") or "",
                anchor_id=str(sec.get("id") or f"sec_{i:03d}"),
                kind=kind,
            ))
            prev_end = start_s
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
                    text=sections[i].get("body") or sections[i].get("narration") or "",
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
                text=sections[i].get("body") or sections[i].get("narration") or "",
                anchor_id=str(sections[i].get("id") or f"sec_{i:03d}"),
                kind=kind,
            )
            for i in range(n)
        ]


register_plugin("timeline", "asr_anchors", AsrAnchors())
assert isinstance(AsrAnchors(), TimelineBuilder)


__all__ = ["AsrAnchors"]
