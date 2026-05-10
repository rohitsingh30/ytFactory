#!/usr/bin/env python3
"""Author the channel banner + icon for 'History Recapped'.

Design: war-history book aesthetic — sepia/khaki canvas, ink-grain texture,
serif typography, embedded medal/aircraft mark. No emoji, no clip-art.
"""
import math, random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT_DIR = Path("/Users/rohit/ytFactory/historyrecapped/branding")
OUT_DIR.mkdir(parents=True, exist_ok=True)

GEORGIA = "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"
GEORGIA_ITAL = "/System/Library/Fonts/Supplemental/Georgia Italic.ttf"
BIG_CASLON = "/System/Library/Fonts/Supplemental/BigCaslon.ttf"

# Channel palette (lifted from historyrecapped/config.yaml image_style_prefix)
KHAKI_DARK = (52, 46, 36)       # near-black charcoal
KHAKI_MID = (107, 89, 60)       # khaki olive
SEPIA = (160, 122, 78)          # warm sepia
CREAM = (236, 222, 192)         # parchment cream
DUST_RED = (172, 79, 56)        # accent — only used sparingly
GOLD = (210, 169, 96)           # foil gold for the medal mark


def grain_overlay(size: tuple[int, int], strength: int = 18, seed: int = 1944) -> Image.Image:
    """Random 1-bit speckle noise blurred into a soft grain — gives the
    flat sepia plates a hand-printed feel without looking digital."""
    rng = random.Random(seed)
    w, h = size
    noise = Image.new("L", (w, h), 0)
    px = noise.load()
    for y in range(h):
        for x in range(w):
            v = rng.randint(0, 255)
            if v > 220:
                px[x, y] = min(255, v + strength)
    noise = noise.filter(ImageFilter.GaussianBlur(radius=1.2))
    return noise


def vignette(size: tuple[int, int], dark: int = 90) -> Image.Image:
    """Radial darken at corners — softens the rectangle into something
    that reads as a printed plate."""
    w, h = size
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    cx, cy = w / 2, h / 2
    rmax = math.hypot(cx, cy)
    for r in range(int(rmax), 0, -10):
        f = max(0, 1 - r / rmax)
        v = int(dark * (1 - f) ** 1.6)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=v)
    return img.filter(ImageFilter.GaussianBlur(radius=40))


def base_canvas(w: int, h: int) -> Image.Image:
    """Build a sepia-khaki background with subtle grain + vignette."""
    base = Image.new("RGB", (w, h), KHAKI_MID)
    # Vertical gradient: top a touch lighter, bottom darker — feels like
    # a printed page held up to a lamp.
    grad = Image.new("L", (1, h))
    for y in range(h):
        f = y / max(1, h - 1)
        grad.putpixel((0, y), int(40 + 30 * f))
    grad = grad.resize((w, h))
    overlay = Image.merge("RGB", (
        grad.point(lambda v: int(v * 0.6)),
        grad.point(lambda v: int(v * 0.5)),
        grad.point(lambda v: int(v * 0.3)),
    ))
    base = Image.blend(base, overlay, 0.35)
    # Grain
    grain = grain_overlay((w, h)).convert("RGB")
    base = Image.blend(base, grain, 0.10)
    # Vignette
    v = vignette((w, h), dark=110)
    dark = Image.new("RGB", (w, h), KHAKI_DARK)
    base = Image.composite(dark, base, v)
    return base


def medal_mark(diameter: int) -> Image.Image:
    """A simple star-on-disc medal mark — channel logogram. Rendered as
    a self-contained transparent PNG so we can paste it onto canvases."""
    canvas = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    cx = cy = diameter // 2
    r_outer = diameter * 0.46
    r_inner = diameter * 0.40
    # Outer ring
    d.ellipse((cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer),
              fill=GOLD, outline=KHAKI_DARK, width=max(2, diameter // 80))
    # Inner field
    d.ellipse((cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner),
              fill=DUST_RED, outline=KHAKI_DARK, width=max(2, diameter // 100))
    # Five-point star
    pts = []
    r1 = diameter * 0.30
    r2 = diameter * 0.13
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        r = r1 if i % 2 == 0 else r2
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    d.polygon(pts, fill=GOLD, outline=KHAKI_DARK)
    # A subtle inner ring of dots ("rivets")
    for i in range(36):
        ang = i * 2 * math.pi / 36
        rr = r_outer - max(3, diameter // 50)
        x = cx + rr * math.cos(ang)
        y = cy + rr * math.sin(ang)
        rad = max(2, diameter // 220)
        d.ellipse((x - rad, y - rad, x + rad, y + rad), fill=KHAKI_DARK)
    return canvas


# ---------- ICON (800x800) ------------------------------------------------
def build_icon() -> Path:
    SIZE = 800
    canvas = base_canvas(SIZE, SIZE)
    # Centred medal mark
    medal = medal_mark(int(SIZE * 0.55))
    mw, mh = medal.size
    canvas.paste(medal, ((SIZE - mw) // 2, int(SIZE * 0.10)), medal)
    # Channel name — two-line stack under the mark
    d = ImageDraw.Draw(canvas)
    line1 = "HISTORY"
    line2 = "RECAPPED"
    f1 = ImageFont.truetype(BIG_CASLON, 110)
    f2 = ImageFont.truetype(BIG_CASLON, 78)
    bbox1 = d.textbbox((0, 0), line1, font=f1)
    bbox2 = d.textbbox((0, 0), line2, font=f2)
    w1, h1 = bbox1[2] - bbox1[0], bbox1[3] - bbox1[1]
    w2, h2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
    y_text = int(SIZE * 0.62)
    d.text(((SIZE - w1) // 2, y_text), line1, fill=CREAM, font=f1,
           stroke_width=3, stroke_fill=KHAKI_DARK)
    d.text(((SIZE - w2) // 2, y_text + h1 + 12), line2, fill=GOLD, font=f2,
           stroke_width=3, stroke_fill=KHAKI_DARK)
    out = OUT_DIR / "icon_800.png"
    canvas.save(out)
    return out


# ---------- BANNER (2560x1440) -------------------------------------------
# YouTube channel banner spec: full canvas 2560x1440, but only the centred
# 1546x423 region is guaranteed visible on mobile. ALL content MUST live
# inside that safe rectangle. The full canvas just bleeds the background
# for desktop/TV viewers.
def build_banner() -> Path:
    W, H = 2560, 1440
    SAFE_W, SAFE_H = 1546, 423
    sx0 = (W - SAFE_W) // 2     # 507
    sy0 = (H - SAFE_H) // 2     # 508
    canvas = base_canvas(W, H)

    # Medal sits at the left edge of the safe area, vertically centred.
    medal_dim = int(SAFE_H * 0.85)   # ~360
    medal = medal_mark(medal_dim)
    medal_x = sx0 + 30
    medal_y = sy0 + (SAFE_H - medal_dim) // 2
    canvas.paste(medal, (medal_x, medal_y), medal)

    d = ImageDraw.Draw(canvas)
    # Text column starts 60px right of the medal, fills the remaining
    # safe-area width.
    text_x = medal_x + medal_dim + 60
    text_max_w = (sx0 + SAFE_W) - text_x

    # Pick the largest font size that fits "History Recapped" within
    # text_max_w. Stepping down by 5pt; Big Caslon at ~150pt is the
    # ceiling for this width.
    name = "History Recapped"
    name_size = 160
    while name_size > 60:
        f = ImageFont.truetype(BIG_CASLON, name_size)
        bb = d.textbbox((0, 0), name, font=f)
        if bb[2] - bb[0] <= text_max_w - 10:
            break
        name_size -= 4
    f_name = ImageFont.truetype(BIG_CASLON, name_size)
    f_tag = ImageFont.truetype(GEORGIA_ITAL, max(34, name_size // 4))
    f_kicker = ImageFont.truetype(GEORGIA, max(22, name_size // 6))

    tag = "Real footage. Real stories."
    kicker = "WW2  ·  COLD WAR  ·  THE BATTLES THAT MATTERED"

    name_bb = d.textbbox((0, 0), name, font=f_name)
    name_w = name_bb[2] - name_bb[0]
    name_h = name_bb[3] - name_bb[1]
    tag_bb = d.textbbox((0, 0), tag, font=f_tag)
    tag_w = tag_bb[2] - tag_bb[0]
    tag_h = tag_bb[3] - tag_bb[1]
    kicker_bb = d.textbbox((0, 0), kicker, font=f_kicker)
    kicker_h = kicker_bb[3] - kicker_bb[1]

    block_h = name_h + 18 + tag_h + 18 + kicker_h
    name_y = sy0 + (SAFE_H - block_h) // 2

    d.text((text_x, name_y), name, fill=CREAM, font=f_name,
           stroke_width=4, stroke_fill=KHAKI_DARK)
    rule_y = name_y + name_h + 14
    d.line([(text_x, rule_y), (text_x + max(name_w, tag_w), rule_y)],
           fill=GOLD, width=3)
    d.text((text_x, rule_y + 8), tag, fill=GOLD, font=f_tag)
    d.text((text_x, rule_y + 8 + tag_h + 18), kicker, fill=CREAM, font=f_kicker)

    out = OUT_DIR / "banner_2560x1440.png"
    canvas.save(out)
    return out


if __name__ == "__main__":
    icon = build_icon()
    banner = build_banner()
    print(f"icon:   {icon}  ({icon.stat().st_size // 1024} KB)")
    print(f"banner: {banner}  ({banner.stat().st_size // 1024} KB)")
