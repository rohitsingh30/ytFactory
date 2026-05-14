"""Word-by-word PNG caption OverlayProducer (short-engine default).

Generates one PNG per beat (TikTok-style: one large word centred at
``H * 0.78``). Each PNG becomes a layer-40 OverlayElement covering
its beat's [start_s, end_s] window.

Today's impl delegates to
:func:`pipeline.compose.render_word_caption_pngs` — the existing
helper that ``shorts.py`` already calls. The bigbang PR moves the
body inline.

Style defaults from ``spec.caption_style.font_size_minimal/standard/dense``
(driven by ``spec.captions_density``).
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


class WordCaptionPngs:
    """One PNG per beat, centred bottom-bias.

    Output: one OverlayElement per timeline segment (each is layer 40,
    asset_path = the rendered PNG, region = None so the FinalMux's
    layer-40 default placement applies).

    Pre-2026-05-14 this lived inline in ``shorts.py`` /
    ``compose.py``. The plugin wraps those helpers.
    """

    def produce(
        self,
        spec: Any,  # RenderSpec
        timeline: Timeline,
        audio: AudioResult,
    ) -> list[OverlayElement]:
        if not timeline:
            return []

        # Resolve font size from CaptionsDensity + spec.caption_style.
        font_size = self._font_size_for_density(spec)

        out_dir = audio.narration_path.parent / "word_captions"
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Word-caption rendering is in pipeline.captions, NOT
            # pipeline.compose. The bigbang PR collapses these helpers
            # under pipeline.render.overlays.* and removes this delegation.
            from pipeline.captions import render_word_caption  # noqa: PLC0415
        except ImportError:
            # Helper not available on this branch — return empty so the
            # engine still produces a video (without word captions).
            # The bigbang PR makes this hard-required.
            return []

        elements: list[OverlayElement] = []
        for i, seg in enumerate(timeline):
            png_path = out_dir / f"word_{i:03d}.png"
            try:
                # render_word_caption signature today:
                # render_word_caption(text, out_path, canvas_w=..., font_size=..., text_rgba=...).
                render_word_caption(
                    seg.text,
                    png_path,
                    canvas_w=spec.output_resolution[0],
                    font_size=font_size,
                )
            except Exception:  # noqa: BLE001
                # Skip individual rendering failures so a corrupted
                # font / weird text glyph doesn't blow the whole render.
                continue
            elements.append(OverlayElement(
                start_s=seg.start_s,
                end_s=seg.end_s,
                layer=40,
                asset_path=png_path,
                extras={"text": seg.text, "anchor_id": seg.anchor_id},
            ))
        return elements

    def _font_size_for_density(self, spec: Any) -> int:
        # CaptionsDensity → font size: minimal=biggest, dense=smallest.
        density_value = (
            spec.captions_density.value
            if hasattr(spec.captions_density, "value")
            else str(spec.captions_density)
        )
        if density_value == "minimal":
            return spec.caption_style.font_size_minimal
        if density_value == "dense":
            return spec.caption_style.font_size_dense
        return spec.caption_style.font_size_standard


register_plugin("overlays", "word_caption_pngs", WordCaptionPngs())
assert isinstance(WordCaptionPngs(), OverlayProducer)


__all__ = ["WordCaptionPngs"]
