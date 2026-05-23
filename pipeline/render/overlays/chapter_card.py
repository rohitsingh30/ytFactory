"""Chapter-card OverlayProducer (full-frame chapter slate).

One layer-30 OverlayElement per chapter / section in the timeline.
Each card shows the chapter number + title; held for
``spec.chapter_card.duration_s`` seconds at the start of each chapter.

Plugin activation: ``spec.chapter_cards = True`` in the wizard.

The PNG-rendering body was inlined from the legacy
``pipeline.render.sports_doc._render_chapter_card`` on 2026-05-14
as part of the bigbang follow-up.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    AudioResult,
    OverlayElement,
    OverlayProducer,
    Timeline,
    register_plugin,
)
from pipeline.render.telemetry_helpers import track_event

_logger = logging.getLogger(__name__)


def _render_chapter_card_png(
    chapter_index: int,
    title: str,
    out_path: Path,
    *,
    canvas_w: int,
    canvas_h: int,
    bg_rgba: tuple,
    number_color: tuple,
    number_font_size: int,
    title_font_size: int,
) -> Path:
    """Full-frame chapter card: deep teal slab + orange chapter
    number + bold white title.

    Inlined from legacy ``pipeline.render.sports_doc._render_chapter_card``
    2026-05-14. Behavior unchanged.
    """
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (canvas_w, canvas_h), tuple(bg_rgba))
    draw = ImageDraw.Draw(img)

    bold_paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
    ]
    title_font = number_font = None
    for fp in bold_paths:
        if Path(fp).exists():
            try:
                title_font = ImageFont.truetype(fp, title_font_size)
                number_font = ImageFont.truetype(fp, number_font_size)
                break
            except Exception:
                continue
    if title_font is None:
        title_font = ImageFont.load_default()
        number_font = ImageFont.load_default()

    chapter_label = f"CHAPTER {chapter_index:02d}"
    cb = draw.textbbox((0, 0), chapter_label, font=number_font)
    cw, ch = cb[2] - cb[0], cb[3] - cb[1]
    cx = (canvas_w - cw) // 2 - cb[0]
    cy = canvas_h // 2 - title_font_size - 30 - cb[1]
    draw.text((cx, cy), chapter_label, font=number_font, fill=tuple(number_color))

    # Word-wrap title to ~30 chars per line
    words = title.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= 30:
            cur = f"{cur} {w}"
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)

    y = canvas_h // 2 + 10
    for line in lines:
        tb = draw.textbbox((0, 0), line, font=title_font)
        tw = tb[2] - tb[0]
        tx = (canvas_w - tw) // 2 - tb[0]
        draw.text((tx, y - tb[1]), line, font=title_font, fill=(255, 255, 255, 255))
        y += title_font_size + 12

    img.save(str(out_path))
    return out_path


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

        out_dir = audio.narration_path.parent / "chapter_cards"
        out_dir.mkdir(parents=True, exist_ok=True)

        elements: list[OverlayElement] = []
        for i, seg in enumerate(chapters):
            png_path = out_dir / f"cc_{i:03d}.png"
            try:
                t0 = time.perf_counter()
                _render_chapter_card_png(
                    chapter_index=i + 1,
                    title=seg.text,
                    out_path=png_path,
                    canvas_w=spec.output_resolution[0],
                    canvas_h=spec.output_resolution[1],
                    bg_rgba=spec.chapter_card.bg_rgba,
                    number_color=spec.chapter_card.number_color,
                    number_font_size=spec.chapter_card.number_font_size,
                    title_font_size=spec.chapter_card.title_font_size,
                )
                duration_ms = int((time.perf_counter() - t0) * 1000)
            except Exception:  # noqa: BLE001
                continue
            track_event(
                "overlay.render",
                category="pipeline",
                duration_ms=duration_ms,
                metadata={
                    "kind": "chap",
                    "count": 1,
                    "total_chars": len(seg.text or ""),
                    "duration_ms": duration_ms,
                },
            )
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
