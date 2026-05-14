"""Sentence-level ASS captions OverlayProducer (long-engine default).

Wraps :func:`pipeline.render.long_form.build_captions_ass` so the new
long_engine can call a Protocol method instead of importing the
renderer-internal helper directly.

Style defaults match the historic Sleepy Time History yellow-italic
caption signature (see ``CaptionStyleConfig`` in spec.py). Channels
that want bold-white captions (sports_doc) override
``spec.caption_style.text_rgba`` + ``spec.caption_style.italic`` in
their ``defaults: { long: { caption_style: ... } }`` block.

Output contract: emits ONE :class:`OverlayElement` covering
``[timeline[0].start_s, timeline[-1].end_s]`` whose ``asset_path``
points at a generated ``.ass`` file. Compose plugins burn the ASS
via ffmpeg's ``ass=`` filter at mux time — same render path
``long_form.final_mux`` uses today.

The bigbang PR moves the body fully into this module so long_form.py
can be deleted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    AudioResult,
    OverlayElement,
    OverlayProducer,
    Timeline,
    register_plugin,
)


class SentenceCaptionAss:
    """Generates a libass-burned subtitle file from the timeline.

    Today's impl is a thin wrapper that calls
    :func:`pipeline.render.long_form.build_captions_ass` on the timeline's
    text + per-segment timestamps. The output ``.ass`` file is then
    composited as a single layer-40 OverlayElement spanning the full
    timeline duration.
    """

    def produce(
        self,
        spec: Any,  # RenderSpec
        timeline: Timeline,
        audio: AudioResult,
    ) -> list[OverlayElement]:
        if not timeline:
            return []

        from pipeline.render.long_form import build_captions_ass  # noqa: PLC0415

        # build_captions_ass expects (cues, out_path, total_duration_s).
        # cues = list[(start_s, end_s, text)]. Build from timeline.
        cues = [(s.start_s, s.end_s, s.text) for s in timeline if s.text]
        if not cues:
            return []

        out_path = audio.narration_path.parent / "captions.ass"
        # Defer to the existing helper. It accepts caption-style
        # kwargs we map from spec.caption_style; for now use defaults
        # (the bigbang PR plumbs spec.caption_style fields end-to-end).
        build_captions_ass(
            cues=cues,
            out_path=out_path,
            total_duration_s=audio.duration_s,
            play_res_x=spec.caption_style.play_res_x,
            play_res_y=spec.caption_style.play_res_y,
        )

        return [OverlayElement(
            start_s=cues[0][0],
            end_s=cues[-1][1],
            layer=40,  # captions layer per contracts.py convention
            asset_path=out_path,
            extras={"format": "ass", "n_cues": len(cues)},
        )]


register_plugin("overlays", "sentence_caption_ass", SentenceCaptionAss())
assert isinstance(SentenceCaptionAss(), OverlayProducer)


__all__ = ["SentenceCaptionAss"]
