"""Build a custom YouTube thumbnail for a long-form sleep episode.

Usage:
    .venv/bin/python historyrecapped/scripts/build_long_form_thumbnail.py \
        --slug pacific-war-1941-1942-sleep \
        --frame-time 60 \
        --title "THE PACIFIC WAR" \
        --subtitle "Pearl Harbor → Midway   1941 – 1942"

Output: historyrecapped/thumbnails/<slug>.jpg (1280x720, JPEG, < 2 MB)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageFont

ROOT = Path(__file__).resolve().parent.parent.parent

OUT_W = 1280
OUT_H = 720


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Pick the best available system font."""
    candidates = (
        ["/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
         "/System/Library/Fonts/Avenir Next.ttc",
         "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
         "/System/Library/Fonts/Helvetica.ttc"]
        if bold else
        ["/System/Library/Fonts/Avenir Next.ttc",
         "/System/Library/Fonts/Supplemental/Georgia.ttf",
         "/System/Library/Fonts/Helvetica.ttc",
         "/System/Library/Fonts/Supplemental/Arial.ttf"]
    )
    for fp in candidates:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    return ImageFont.load_default()


def extract_frame(mp4: Path, t: float, out_jpg: Path) -> Path:
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", f"{t}", "-i", str(mp4),
         "-frames:v", "1",
         str(out_jpg)],
        check=True,
    )
    return out_jpg


def _gradient_overlay(size: Tuple[int, int]) -> Image.Image:
    """Soft dark gradient from top to bottom — heavier at the bottom for text legibility."""
    w, h = size
    grad = Image.new("L", (1, h), 0)
    for y in range(h):
        # 0 (transparent) at top, 180 (mostly opaque) near bottom 60%
        alpha = int(min(200, 30 + (y / h) ** 1.6 * 200))
        grad.putpixel((0, y), alpha)
    grad = grad.resize((w, h))
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    overlay.putalpha(grad)
    return overlay


def build_thumbnail(
    base_jpg: Path,
    out_jpg: Path,
    title: str,
    subtitle: str,
    channel_mark: str = "HISTORY RECAPPED · for sleep",
) -> Path:
    img = Image.open(base_jpg).convert("RGB")
    # Auto-crop pillarbars/letterbars (the DroneScapes source is 4:3 content
    # padded into 16:9, plus our renderer adds blurred-letterbox bars too).
    # Sample the middle row to find the content x-range; sample the middle
    # column for the y-range. Threshold = 15/255 to forgive subtle blur fill.
    import numpy as _np
    arr = _np.asarray(img.convert("L"))
    mid_row = arr[arr.shape[0] // 2]
    nonblack_x = mid_row > 15
    if nonblack_x.any():
        x0 = int(_np.argmax(nonblack_x))
        x1 = int(len(nonblack_x) - _np.argmax(nonblack_x[::-1]))
    else:
        x0, x1 = 0, arr.shape[1]
    mid_col = arr[:, arr.shape[1] // 2]
    nonblack_y = mid_col > 15
    if nonblack_y.any():
        y0 = int(_np.argmax(nonblack_y))
        y1 = int(len(nonblack_y) - _np.argmax(nonblack_y[::-1]))
    else:
        y0, y1 = 0, arr.shape[0]
    if (x1 - x0) < img.size[0] - 20 or (y1 - y0) < img.size[1] - 20:
        print(f"[thumb] auto-crop bars: x={x0}..{x1} y={y0}..{y1} (was {img.size})")
        img = img.crop((x0, y0, x1, y1))
    # Cover-fit to 1280x720.
    w, h = img.size
    target_ratio = OUT_W / OUT_H
    src_ratio = w / h
    if src_ratio > target_ratio:
        # too wide → crop sides
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        # too tall → crop top/bottom
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    img = img.resize((OUT_W, OUT_H), Image.LANCZOS)

    # Slight desaturation + darken for evening register.
    img = ImageEnhance.Color(img).enhance(0.85)
    img = ImageEnhance.Brightness(img).enhance(0.85)

    # Add bottom-heavy dark gradient for text legibility.
    base = img.convert("RGBA")
    base.alpha_composite(_gradient_overlay((OUT_W, OUT_H)))
    img = base

    draw = ImageDraw.Draw(img)

    # Title — large, soft white, centered horizontally, bottom third.
    title_font = _font(96, bold=True)
    sub_font = _font(40)
    mark_font = _font(22)

    # Measure
    tb = draw.textbbox((0, 0), title, font=title_font)
    tw = tb[2] - tb[0]; th = tb[3] - tb[1]
    sb = draw.textbbox((0, 0), subtitle, font=sub_font)
    sw = sb[2] - sb[0]; sh = sb[3] - sb[1]
    mb = draw.textbbox((0, 0), channel_mark, font=mark_font)
    mw = mb[2] - mb[0]; mh = mb[3] - mb[1]

    # Channel mark goes at the TOP-right corner so it doesn't crowd the title.
    # Layout: title centered in the lower third, subtitle just above it.
    margin_bottom = 100
    title_y = OUT_H - margin_bottom - th
    sub_y = title_y - sh - 32

    title_x = (OUT_W - tw) // 2 - tb[0]
    sub_x = (OUT_W - sw) // 2 - sb[0]
    mark_x = OUT_W - mw - 32 - mb[0]
    mark_y = 24 - mb[1]

    # Soft drop shadow for title (single offset, no doubling).
    draw.text((title_x + 3, title_y + 4), title, font=title_font, fill=(0, 0, 0, 220))
    draw.text((title_x, title_y), title, font=title_font, fill=(245, 240, 225, 255))

    # Subtitle — slightly warm off-white.
    draw.text((sub_x + 2, sub_y + 3), subtitle, font=sub_font, fill=(0, 0, 0, 200))
    draw.text((sub_x, sub_y), subtitle, font=sub_font, fill=(220, 215, 200, 255))

    # Channel mark — small, dim, top-right corner.
    draw.text((mark_x, mark_y), channel_mark, font=mark_font, fill=(180, 180, 180, 220))

    # Soft inner-edge vignette via a gaussian darker on the edges.
    # Implemented as a radial gradient overlay rather than alpha mask
    # (alpha mask was creating black side bars in the JPEG output).
    vmask = Image.new("L", (OUT_W, OUT_H), 0)
    vd = ImageDraw.Draw(vmask)
    vd.ellipse([-OUT_W//5, -OUT_H//5, OUT_W + OUT_W//5, OUT_H + OUT_H//5], fill=255)
    vmask = vmask.filter(ImageFilter.GaussianBlur(radius=120))
    # Build an RGBA layer that's pure black with alpha = 255 - vmask (darker
    # at the corners) and composite onto the image.
    inv = Image.eval(vmask, lambda v: int((255 - v) * 0.5))  # max ~50% darken in corners
    dark_layer = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    dark_layer.putalpha(inv)
    img.alpha_composite(dark_layer)

    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(str(out_jpg), "JPEG", quality=88, optimize=True)
    return out_jpg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--frame-time", type=float, default=60.0,
                    help="seconds into the source/rendered mp4 to grab as base frame")
    ap.add_argument("--frame-source", default="rendered",
                    choices=["rendered", "source"],
                    help="rendered mp4 has burned captions; source has no captions but is harder to time-align")
    ap.add_argument("--frame-source-path", default=None,
                    help="explicit path to a source mp4 (for --frame-source=source)")
    ap.add_argument("--title", required=True)
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--channel-mark", default="HISTORY RECAPPED · for sleep")
    args = ap.parse_args()

    if args.frame_source == "source":
        if args.frame_source_path:
            mp4 = Path(args.frame_source_path)
        else:
            # Default — first source clip from the shotlist
            import json as _json
            sl = _json.loads((ROOT / f"historyrecapped/shotlist/{args.slug}.json").read_text())
            mp4 = ROOT / "historyrecapped/footage/long_sources" / sl["clips"][0]["source"]
    else:
        mp4 = ROOT / "historyrecapped/shorts" / f"{args.slug}.mp4"
    if not mp4.exists():
        raise SystemExit(f"missing mp4: {mp4}")
    base = ROOT / "historyrecapped/thumbnails" / f"_base_{args.slug}.jpg"
    out = ROOT / "historyrecapped/thumbnails" / f"{args.slug}.jpg"
    print(f"[thumb] extracting frame at {args.frame_time}s of {mp4.name}…")
    extract_frame(mp4, args.frame_time, base)
    print(f"[thumb] composing → {out.name}")
    build_thumbnail(base, out, args.title, args.subtitle, args.channel_mark)
    size_kb = out.stat().st_size // 1024
    print(f"[done] {out} ({size_kb} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
