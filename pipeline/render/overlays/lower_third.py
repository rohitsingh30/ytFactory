"""Lower-third OverlayProducer (speaker name + handle).

Generates one PNG per talking-head segment (or per anchored
foreground footage clip) showing the speaker's name + social handle.
Default style: deep-teal slab + orange accent + bold white text
(matches sports_doc historical aesthetic; channels override via
``spec.lower_third`` config).

Plugin activation: ``spec.lower_thirds = True`` in the wizard.

The PNG-rendering body was inlined from the legacy
``pipeline.render.sports_doc._render_lower_third`` on 2026-05-14
as part of the bigbang follow-up. Bigbang itself stripped the
legacy module's orchestration entry points; this commit removes
the helper-import dependency so ``sports_doc.py`` can be deleted.
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


def _render_lower_third_png(
    speaker: str,
    handle: str,
    out_path: Path,
    *,
    bg_rgba: tuple,
    text_rgba: tuple,
    accent_rgba: tuple,
    font_size: int,
    handle_font_size: int,
) -> Path:
    """Slab lower-third: deep teal block, bold white speaker, orange
    underline, small handle below.

    Inlined from legacy ``pipeline.render.sports_doc._render_lower_third``
    2026-05-14. Behavior unchanged.
    """
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

    out_path.parent.mkdir(parents=True, exist_ok=True)

    plain_paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    name_font = handle_font = None
    for fp in plain_paths:
        if Path(fp).exists():
            try:
                name_font = ImageFont.truetype(fp, font_size)
                handle_font = ImageFont.truetype(fp, handle_font_size)
                break
            except Exception:
                continue
    if name_font is None:
        name_font = ImageFont.load_default()
        handle_font = ImageFont.load_default()

    pad_x, pad_y = 24, 14
    accent_h = 4
    handle_text = handle if handle else ""
    dummy = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dummy)
    nb = dd.textbbox((0, 0), speaker, font=name_font)
    hb = dd.textbbox((0, 0), handle_text, font=handle_font) if handle_text else (0, 0, 0, 0)
    nw = nb[2] - nb[0]
    nh = nb[3] - nb[1]
    hw = hb[2] - hb[0]
    hh = (hb[3] - hb[1]) if handle_text else 0
    inner_w = max(nw, hw)
    inner_h = nh + (8 + hh if handle_text else 0)
    img_w = inner_w + pad_x * 2
    img_h = inner_h + pad_y * 2 + accent_h

    img = Image.new("RGBA", (img_w, img_h), tuple(bg_rgba))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, img_h - accent_h, img_w, img_h), fill=tuple(accent_rgba))
    y = pad_y
    draw.text((pad_x - nb[0], y - nb[1]), speaker, font=name_font, fill=tuple(text_rgba))
    if handle_text:
        y += nh + 8
        # handle in slightly dimmed white
        draw.text((pad_x - hb[0], y - hb[1]), handle_text, font=handle_font,
                  fill=(text_rgba[0], text_rgba[1], text_rgba[2], 200))

    img.save(str(out_path))
    return out_path


class LowerThird:
    """One layer-20 OverlayElement per Segment carrying speaker
    metadata.

    Pulls speaker info from ``Segment.text`` (when it parses as
    ``"Speaker Name | @handle"``) or skips the Segment otherwise.
    The bigbang PR adds a richer schema where Segments carry
    explicit speaker/handle fields.

    Style defaults read from ``spec.lower_third`` config — channels
    can override per-render.
    """

    def produce(
        self,
        spec: Any,
        timeline: Timeline,
        audio: AudioResult,
    ) -> list[OverlayElement]:
        out_dir = audio.narration_path.parent / "lower_thirds"
        out_dir.mkdir(parents=True, exist_ok=True)

        elements: list[OverlayElement] = []
        for i, seg in enumerate(timeline):
            speaker, handle = self._parse_speaker(seg.text)
            if not speaker:
                continue
            png_path = out_dir / f"lt_{i:03d}.png"
            try:
                _render_lower_third_png(
                    speaker=speaker,
                    handle=handle,
                    out_path=png_path,
                    bg_rgba=spec.lower_third.bg_rgba,
                    text_rgba=spec.lower_third.text_rgba,
                    accent_rgba=spec.lower_third.accent_rgba,
                    font_size=spec.lower_third.font_size,
                    handle_font_size=spec.lower_third.handle_font_size,
                )
            except Exception:  # noqa: BLE001
                continue
            hold = max(seg.end_s - seg.start_s, spec.lower_third.hold_min_s)
            elements.append(OverlayElement(
                start_s=seg.start_s,
                end_s=seg.start_s + hold,
                layer=20,
                asset_path=png_path,
                extras={"speaker": speaker, "handle": handle},
            ))
        return elements

    def _parse_speaker(self, text: str) -> tuple[str, str]:
        """Parse ``"Name | @handle"`` from segment text. Returns
        ``("", "")`` if the format doesn't match."""
        if "|" not in text:
            return "", ""
        name, handle = text.split("|", 1)
        return name.strip(), handle.strip()


register_plugin("overlays", "lower_third", LowerThird())
assert isinstance(LowerThird(), OverlayProducer)


__all__ = ["LowerThird"]
