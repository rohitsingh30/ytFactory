"""Chapter-card OverlayProducer (full-frame chapter slate).

One layer-30 OverlayElement per chapter / section in the timeline.
Each card shows the chapter number + title; held for
``spec.chapter_card.duration_s`` seconds at the start of each chapter.

Plugin activation: ``spec.chapter_cards = True`` in the wizard.

Today's impl is a delegating wrapper around the existing sports_doc
helper. Bigbang PR moves the body inline.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    AudioResult,
    OverlayElement,
    OverlayProducer,
    Timeline,
    register_plugin,
)

_logger = logging.getLogger(__name__)


class ChapterCard:
    """One full-frame chapter card per Segment whose ``kind`` is
    ``"chapter"`` or ``"section"``.

    Fast-skip beats — chapter cards belong on long-form / overlay-
    timeline renders, not short-form per-beat slideshows.
    """

    def produce(
        self,
        spec: Any,
        timeline: Timeline,
        audio: AudioResult,
    ) -> list[OverlayElement]:
        chapters = [s for s in timeline if s.kind in {"chapter", "section"}]
        if not chapters:
            return []

        try:
            from pipeline.render.sports_doc import _render_chapter_card  # noqa: PLC0415
        except ImportError:
            _logger.warning("chapter_card: sports_doc helper unavailable — "
                            "no chapter cards emitted")
            return []

        out_dir = audio.narration_path.parent / "chapter_cards"
        out_dir.mkdir(parents=True, exist_ok=True)

        elements: list[OverlayElement] = []
        for i, seg in enumerate(chapters):
            png_path = out_dir / f"cc_{i:03d}.png"
            try:
                _render_chapter_card(
                    chapter_number=i + 1,
                    title=seg.text,
                    out_path=png_path,
                    bg_rgba=spec.chapter_card.bg_rgba,
                    number_color=spec.chapter_card.number_color,
                    number_font_size=spec.chapter_card.number_font_size,
                    title_font_size=spec.chapter_card.title_font_size,
                    canvas_w=spec.output_resolution[0],
                    canvas_h=spec.output_resolution[1],
                )
            except Exception:  # noqa: BLE001
                continue
            elements.append(OverlayElement(
                start_s=seg.start_s,
                end_s=seg.start_s + spec.chapter_card.duration_s,
                layer=30,
                asset_path=png_path,
                extras={"chapter_number": i + 1, "title": seg.text},
            ))
        return elements


register_plugin("overlays", "chapter_card", ChapterCard())
assert isinstance(ChapterCard(), OverlayProducer)


__all__ = ["ChapterCard"]
