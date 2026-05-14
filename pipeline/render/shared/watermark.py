"""Shared watermark PNG renderer for every render engine + plugin.

Pre-2026-05-14 lived inside ``pipeline/render/long_form.py``;
sports_doc.py imported it from there. Promoted here as part of the
4-renderer-to-2-engine consolidation (plan.md).

Style defaults (font_size=28, text_color=(255,255,255,140), italic=False)
are the Sleepy Time History watermark spec — a faint white sans-serif
in the top-right corner. Channels override via the
``WatermarkConfig`` block on RenderSpec (see
``files/knob_inventory.txt`` for the full list of overridable
fields).

Function body is byte-equivalent to the long_form.py original.
"""
from __future__ import annotations

from pathlib import Path


def render_watermark_png(
    text: str,
    out_path: Path,
    font_size: int = 28,
    text_color: tuple = (255, 255, 255, 140),  # ~55% opacity white
    italic: bool = False,
) -> Path:
    """Generate a transparent PNG of the channel name for top-right overlay.

    Match the Sleepy Time History watermark spec: faint white sans-serif
    in the top-right corner of every frame, low opacity so it doesn't
    dominate. Cached at ``<branding_dir>/watermark_topright.png`` —
    re-render only when the file is missing.
    """
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

    italic_paths = [
        "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf",
    ]
    plain_paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Avenir.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    font_paths = (italic_paths + plain_paths) if italic else plain_paths
    font: ImageFont.FreeTypeFont | None = None
    for fp in font_paths:
        if Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, font_size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    dummy = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    bb = ImageDraw.Draw(dummy).textbbox((0, 0), text, font=font)
    w = bb[2] - bb[0] + 6
    h = bb[3] - bb[1] + 6
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((-bb[0] + 3, -bb[1] + 3), text, font=font, fill=text_color)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))
    return out_path


__all__ = ["render_watermark_png"]
