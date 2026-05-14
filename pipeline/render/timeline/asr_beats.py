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
        # Cloud path first.
        try:
            from pipeline.asr_cloudrun import align_via_cloud  # noqa: PLC0415
            segments = align_via_cloud(
                audio.narration_path,
                mode="beats",
                language=self._language_hint(spec),
            )
            if segments:
                _logger.info("asr_beats: cloud whisper returned %d beats", len(segments))
                return segments
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
            return []

        result = transcribe(audio.narration_path)
        # Flatten word-level timestamps.
        words: list[dict] = []
        for seg in result.get("segments", []):
            for w in seg.get("words", []) or []:
                words.append({
                    "text": (w.get("word") or w.get("text") or "").strip(),
                    "start": float(w.get("start", 0)),
                    "end": float(w.get("end", 0)),
                })

        forced_lines = self._forced_beat_texts(script)
        try:
            beats = split_with_forced_boundaries(
                words=words,
                forced_lines=forced_lines,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("asr_beats: beat-split failed (%s) — "
                            "falling back to one segment per word", exc)
            return [
                Segment(
                    start_s=w["start"], end_s=w["end"],
                    text=w["text"], anchor_id=f"word_{i:04d}", kind="word",
                )
                for i, w in enumerate(words)
            ]

        return [
            Segment(
                start_s=float(b.start),
                end_s=float(b.end),
                text=b.text,
                anchor_id=f"beat_{i:03d}",
                kind="beat",
            )
            for i, b in enumerate(beats)
        ]

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
