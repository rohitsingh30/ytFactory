"""ASR-derived beat TimelineBuilder for the short engine.

Calls cloud whisper (``cloud/asr-whisper/`` deployed as
``ytfactory-asr-whisper`` Cloud Run service) on the narrated wav to
get word-level timestamps, then groups the words into beats matching
the authored shotlist via :mod:`pipeline.beats`'s
``split_with_forced_boundaries`` helper.

Cloud→laptop fallback
---------------------

Cloud whisper is the default. On
:class:`pipeline.asr_cloudrun.CloudRunAsrUnavailable` (DNS fail,
5xx, timeout, or simply ``CLOUDRUN_ASR_URL`` unset) the plugin falls
back to local ``pipeline.asr.transcribe`` (whisper_mlx). Same pattern
as cloud TTS / image services. Set
``CLOUDRUN_ASR_DISABLE_FALLBACK=1`` in tests to hard-error instead.

Output Segments have ``kind="beat"`` and ``anchor_id="beat_<i>"``
(zero-padded to 3 digits) so visualize plugins like
``ai_beat_slideshow`` can correlate one image per beat.
"""
from __future__ import annotations

import logging
import math
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
from pipeline.render.telemetry_helpers import emit_timeline_telemetry

_logger = logging.getLogger(__name__)


class AsrBeats:
    """Whisper-derived per-beat Timeline.

    Default flow: cloud whisper via :func:`pipeline.asr_cloudrun.align_via_cloud`.
    On failure: local ``pipeline.asr.transcribe`` + beat-grouping via
    ``pipeline.beats.split_with_forced_boundaries``.

    Output Segments have ``kind="beat"`` and ``anchor_id="beat_<i>"``
    (zero-padded to 3 digits) so visualize plugins like
    ``ai_beat_slideshow`` can correlate one image per beat.
    """

    def build(
        self,
        spec: Any,  # RenderSpec
        script: dict[str, Any],
        audio: AudioResult,
    ) -> Timeline:
        # Cloud path first — call in mode="words" so we get per-word
        # timings, then group into beats locally with the word-level
        # data preserved on each Segment. Pre-2026-05-17 we called
        # mode="beats" which returned only beat-level segments (text +
        # start_s + end_s — no word timings). That broke the
        # word_caption_pngs overlay producer, which fell back to
        # rendering the whole beat sentence in one tiny pill instead
        # of TikTok-style one-word-at-a-time captions.
        try:
            from pipeline.asr_cloudrun import align_via_cloud  # noqa: PLC0415
            from pipeline.beats import (  # noqa: PLC0415
                Word, split_into_beats,
            )
            word_segments = align_via_cloud(
                audio.narration_path,
                mode="words",
                language=self._language_hint(spec),
            )
            if word_segments:
                _logger.info(
                    "asr_beats: cloud whisper returned %d word-segments",
                    len(word_segments),
                )
                # Convert word-segments → Word objects → grouped Beats
                # → Segments with .words preserved.
                words_list = [
                    Word(text=ws.text, start=ws.start_s, end=ws.end_s)
                    for ws in word_segments
                ]
                # Per-channel beat duration cap — falls back to 2.8s.
                max_s = float(getattr(spec, "beat_max_s", None) or 2.8)
                # 2026-05-19 — pin the beat count to the authored
                # script's shot/beat list so it matches what the
                # prompts-authoring stage produced. Without forced
                # boundaries the cloud path splits purely by audio
                # timing (variable count); ai_beat_slideshow then
                # refuses to render with "prompts.json has N entries
                # but timeline has M segments". Local fallback already
                # used forced boundaries (line below in _local_fallback);
                # this brings the cloud path back to parity.
                forced_lines = self._forced_beat_texts(script)
                beats = split_into_beats(
                    words_list,
                    max_s=max_s,
                    forced_narration_lines=forced_lines or None,
                )
                timeline = [
                    Segment(
                        start_s=float(b.start),
                        end_s=float(b.end),
                        text=b.text,
                        anchor_id=f"beat_{i:03d}",
                        kind="beat",
                        words=list(b.words),
                    )
                    for i, b in enumerate(beats)
                ]
                emit_timeline_telemetry(
                    timeline,
                    total_seconds=audio.duration_s,
                    unanchored_count=0,
                    anchor_method="cloud_words",
                )
                return timeline
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("CLOUDRUN_ASR_DISABLE_FALLBACK") == "1":
                raise
            _logger.warning("asr_beats: cloud whisper failed (%s) — "
                            "falling back to local whisper", exc)

        # Local fallback — whisper_mlx via pipeline.asr.
        return self._local_fallback(spec, script, audio)

    def _local_fallback(
        self,
        spec: Any,
        script: dict[str, Any],
        audio: AudioResult,
    ) -> Timeline:
        try:
            from pipeline.asr import transcribe  # noqa: PLC0415
            from pipeline.beats import split_with_forced_boundaries  # noqa: PLC0415
        except ImportError as exc:
            _logger.error("asr_beats: local whisper unavailable: %s", exc)
            timeline: Timeline = []
            emit_timeline_telemetry(
                timeline,
                total_seconds=audio.duration_s,
                unanchored_count=len(self._forced_beat_texts(script)),
                anchor_method="local_unavailable",
            )
            return timeline

        result = transcribe(audio.narration_path)
        # Flatten word-level timestamps. Whisper occasionally emits
        # word entries with start/end set to None (e.g. on speech
        # detected but timing model couldn't anchor it), or with the
        # word text missing. Skip those rather than crashing the
        # timeline build on a NoneType float coercion — losing a few
        # word-anchors degrades caption accuracy slightly; crashing
        # loses the whole render.
        words: list[dict] = []
        for seg in result.get("segments", []):
            for w in seg.get("words", []) or []:
                text = (w.get("word") or w.get("text") or "").strip()
                if not text:
                    continue
                start = w.get("start")
                end = w.get("end")
                if start is None or end is None:
                    continue
                try:
                    start_f = float(start)
                    end_f = float(end)
                except (TypeError, ValueError):
                    continue
                if not (math.isfinite(start_f) and math.isfinite(end_f)):
                    continue
                if end_f < start_f:
                    end_f = start_f
                words.append({"text": text, "start": start_f, "end": end_f})

        # Defensive empty-alignment guard. Whisper can return zero word
        # alignments on near-silent audio, very short clips, or non-
        # speech (music intros). Falling through with words=[] silently
        # builds an empty beat timeline → captions stay blank, image
        # plugin emits no panels → unwatchable render. Better to fail
        # loud here so the caller can route to a different timeline
        # plugin (e.g. ``asr_anchors`` or ``forced_lines``-only path).
        if not words:
            _logger.error(
                "asr_beats: Whisper produced zero usable word alignments "
                "from %s — refusing to build an empty timeline. The "
                "engine should fall back to a non-ASR timeline plugin "
                "(forced_lines / asr_anchors).",
                audio.narration_path,
            )
            timeline: Timeline = []
            emit_timeline_telemetry(
                timeline,
                total_seconds=audio.duration_s,
                unanchored_count=len(self._forced_beat_texts(script)),
                anchor_method="local_empty_alignment",
            )
            return timeline

        forced_lines = self._forced_beat_texts(script)
        try:
            beats = split_with_forced_boundaries(
                words=words,
                forced_narration_lines=forced_lines,
            )
        except TypeError as exc:
            # Wrong kwarg name → re-raise with a clear message so the
            # signature drift is obvious next time. asr_beats called
            # ``forced_lines=`` from 2026-05-14 → 2026-05-15 against a
            # function that expects ``forced_narration_lines=`` —
            # silently swallowed by the BLE001 catch below, producing
            # a 100+ word-segment fallback timeline that broke captions
            # AND visuals on every cloud short. Surfaced by job
            # f1e319a3 canary on 2026-05-15.
            _logger.error(
                "asr_beats: split_with_forced_boundaries TypeError — "
                "fix the kwarg drift: %s", exc,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            _logger.warning("asr_beats: beat-split failed (%s) — "
                            "falling back to one segment per word", exc)
            timeline = [
                Segment(
                    start_s=w["start"], end_s=w["end"],
                    text=w["text"], anchor_id=f"word_{i:04d}", kind="word",
                )
                for i, w in enumerate(words)
            ]
            emit_timeline_telemetry(
                timeline,
                total_seconds=audio.duration_s,
                unanchored_count=len(timeline),
                anchor_method="local_word_fallback",
            )
            return timeline

        timeline = [
            Segment(
                start_s=float(b.start),
                end_s=float(b.end),
                text=b.text,
                anchor_id=f"beat_{i:03d}",
                kind="beat",
                # Thread word-level timings (post-2026-05-17) so the
                # word_caption_pngs overlay producer can do TRUE one-
                # word-at-a-time captions with per-word enable windows.
                # Pre-fix the plugin squashed the whole beat text into
                # a single PNG → tiny illegible captions.
                words=list(b.words) if getattr(b, "words", None) else None,
            )
            for i, b in enumerate(beats)
        ]
        emit_timeline_telemetry(
            timeline,
            total_seconds=audio.duration_s,
            unanchored_count=0,
            anchor_method="local_whisper",
        )
        return timeline

    def _language_hint(self, spec: Any) -> str | None:
        # Channel YAML may provide ``language`` at root level (hi / en / etc).
        # Fall back to None (whisper auto-detect).
        return getattr(spec, "language", None) or (spec.extra or {}).get("language")

    def _forced_beat_texts(self, script: dict[str, Any]) -> list[str]:
        # Short scripts authored by /make-script have either a shots[]
        # list (with narration_line per shot) or beats[] — support both.
        shots = script.get("shots") or []
        if shots:
            lines = [s.get("narration_line", "") for s in shots
                     if s.get("narration_line")]
            closer = (script.get("closer") or {}).get("narration_line")
            if closer:
                lines.append(closer)
            return lines
        beats = script.get("beats") or []
        return [b.get("text", "") for b in beats if b.get("text")]


register_plugin("timeline", "asr_beats", AsrBeats())
assert isinstance(AsrBeats(), TimelineBuilder)


__all__ = ["AsrBeats"]
