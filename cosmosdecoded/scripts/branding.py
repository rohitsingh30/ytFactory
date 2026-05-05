#!/usr/bin/env python3
"""Author the channel banner + icon for 'Cosmos Decoded'.

Design: physics + space documentary aesthetic — deep observatory navy,
star-field grain, gold orbital mark, premier serif typography. Sober.
NOT sci-fi neon, NOT generic astronomy clip-art. The visual cue is
"page from a 1920s Royal Society proceedings, photographed at night".
"""
import math, random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT_DIR = Path("/Users/rohit/ytFactory/cosmosdecoded/branding")
OUT_DIR.mkdir(parents=True, exist_ok=True)

GEORGIA = "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"
GEORGIA_ITAL = "/System/Library/Fonts/Supplemental/Georgia Italic.ttf"
BIG_CASLON = "/System/Library/Fonts/Supplemental/BigCaslon.ttf"

# Channel palette — observatory midnight + warm star gold + cool cyan accent.
# Distinct from HistoryRecapped's warm sepia/khaki. Intentionally cool-leaning
# so the gold orbital mark + star highlights pop.
DEEP_NAVY = (8, 14, 36)            # near-black observatory void
INDIGO = (28, 38, 70)              # midnight blue mid-tone
OBSERVATORY_BLUE = (62, 95, 145)   # telescope-dome blue, used sparingly
STAR_GOLD = (240, 200, 90)         # warm star highlight + orbital ring
AURORA_CYAN = (110, 200, 220)      # cool accent for the inner orbit
PARCHMENT = (228, 220, 200)        # cool cream for body text
DUST_BLUE = (96, 122, 165)         # tertiary midtone


def starfield_overlay(size: tuple[int, int], seed: int = 1919) -> Image.Image:
    """Sparse pinprick stars on a black canvas, blurred just enough to
    look like long-exposure photography rather than digital pixels.
    Density tuned for "scattered, not crowded" — a few hundred bright
    points across a 2K canvas."""
    rng = random.Random(seed)
    w, h = size
    stars = Image.new("L", (w, h), 0)
    px = stars.load()
    n_stars = max(120, (w * h) // 18000)
    for _ in range(n_stars):
        x = rng.randint(0, w - 1)
        y = rng.randint(0, h - 1)
        # Bias toward dim stars with rare bright ones.
        v = rng.randint(60, 255) if rng.random() < 0.18 else rng.randint(20, 90)
        px[x, y] = v
        # Some stars get a 1px aura
        if v > 200 and 0 < x < w - 1 and 0 < y < h - 1:
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                px[x + dx, y + dy] = max(px[x + dx, y + dy], v // 3)
    return stars.filter(ImageFilter.GaussianBlur(radius=0.6))


def grain_overlay(size: tuple[int, int], strength: int = 14, seed: int = 1925) -> Image.Image:
    """Subtle film-grain noise — keeps the flat navy from looking
    digital-flat. Lower density than the HistoryRecapped sepia grain
    because a starfield already supplies texture."""
    rng = random.Random(seed)
    w, h = size
    noise = Image.new("L", (w, h), 0)
    px = noise.load()
    for y in range(h):
        for x in range(w):
            v = rng.randint(0, 255)
            if v > 232:
                px[x, y] = min(255, v + strength)
    noise = noise.filter(ImageFilter.GaussianBlur(radius=1.0))
    return noise


def vignette(size: tuple[int, int], dark: int = 110) -> Image.Image:
    """Radial darken at corners — softens the canvas into a telescope
    field-of-view feel."""
    w, h = size
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    cx, cy = w / 2, h / 2
    rmax = math.hypot(cx, cy)
    for r in range(int(rmax), 0, -10):
        f = max(0, 1 - r / rmax)
        v = int(dark * (1 - f) ** 1.5)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=v)
    return img.filter(ImageFilter.GaussianBlur(radius=40))


def base_canvas(w: int, h: int) -> Image.Image:
    """Build a deep-navy canvas with vertical gradient (slightly lighter
    near the top — suggestion of a sky), starfield, grain, vignette."""
    base = Image.new("RGB", (w, h), DEEP_NAVY)
    # Vertical gradient: top a touch lighter (like dusk turning to night).
    grad = Image.new("L", (1, h))
    for y in range(h):
        f = y / max(1, h - 1)
        # Top: ~+18 brightness; bottom: deep dark.
        grad.putpixel((0, y), int(40 - 22 * f))
    grad = grad.resize((w, h))
    overlay = Image.merge("RGB", (
        grad.point(lambda v: int(v * 0.6)),     # subtle warm cast at top
        grad.point(lambda v: int(v * 0.7)),
        grad.point(lambda v: int(v * 1.1)),     # blue dominant
    ))
    base = Image.blend(base, overlay, 0.45)
    # Starfield — additive blend so dark stays dark
    stars = starfield_overlay((w, h)).convert("RGB")
    base = Image.eval(Image.blend(base, Image.new("RGB", (w, h), PARCHMENT), 0.0), lambda x: x)
    # Composite stars: paste with luminance mask
    star_mask = starfield_overlay((w, h))
    star_layer = Image.new("RGB", (w, h), PARCHMENT)
    base.paste(star_layer, (0, 0), star_mask)
    # Film grain
    grain = grain_overlay((w, h)).convert("RGB")
    base = Image.blend(base, grain, 0.06)
    # Vignette
    v = vignette((w, h), dark=120)
    dark = Image.new("RGB", (w, h), (2, 4, 14))
    base = Image.composite(dark, base, v)
    return base


def orbital_mark(diameter: int) -> Image.Image:
    """A stylised orbit/atom mark — three concentric ellipses at angles
    suggesting a 3D orbital, with a single bright star-gold dot on the
    outermost ring. Reads as 'physics + space' without being literally
    either an atom or a planet (both clichés). All-vector, transparent."""
    canvas = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    cx = cy = diameter // 2

    # Outer disc — barely-visible halo so the mark has presence at small sizes.
    halo_r = int(diameter * 0.49)
    halo = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    halo_d = ImageDraw.Draw(halo)
    halo_d.ellipse((cx - halo_r, cy - halo_r, cx + halo_r, cy + halo_r),
                   fill=(*OBSERVATORY_BLUE, 60))
    halo = halo.filter(ImageFilter.GaussianBlur(radius=diameter * 0.04))
    canvas = Image.alpha_composite(canvas, halo)
    d = ImageDraw.Draw(canvas)

    # Three orbital ellipses — outer tilted ~0deg, middle 60deg, inner 120deg.
    # Each rendered as a separate rotated layer for clean angle control.
    orbital_specs = [
        # (rx_factor, ry_factor, angle_deg, color, line_w_factor)
        (0.46, 0.18, 0,    STAR_GOLD,         0.012),   # outer, near-flat
        (0.40, 0.15, 60,   AURORA_CYAN,       0.010),   # middle
        (0.34, 0.13, 120,  DUST_BLUE,         0.008),   # inner
    ]
    for rx_f, ry_f, ang, color, lw_f in orbital_specs:
        rx = int(diameter * rx_f)
        ry = int(diameter * ry_f)
        lw = max(2, int(diameter * lw_f))
        # Render the ellipse on a transparent layer, then rotate.
        layer = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.ellipse((cx - rx, cy - ry, cx + rx, cy + ry),
                   outline=(*color, 230), width=lw)
        layer = layer.rotate(ang, resample=Image.BICUBIC, center=(cx, cy))
        canvas = Image.alpha_composite(canvas, layer)
        d = ImageDraw.Draw(canvas)

    # Star dot on the outer orbital — placed at ~30deg from horizontal
    # so it doesn't sit dead-centre on the rim. Bright gold with a
    # small glow.
    rx_outer = int(diameter * 0.46)
    ry_outer = int(diameter * 0.18)
    theta = math.radians(-32)   # upper-right quadrant
    sx = cx + rx_outer * math.cos(theta)
    sy = cy + ry_outer * math.sin(theta)
    star_glow_r = int(diameter * 0.05)
    glow_layer = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gd.ellipse((sx - star_glow_r, sy - star_glow_r, sx + star_glow_r, sy + star_glow_r),
               fill=(*STAR_GOLD, 110))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=diameter * 0.02))
    canvas = Image.alpha_composite(canvas, glow_layer)
    d = ImageDraw.Draw(canvas)
    star_r = int(diameter * 0.022)
    d.ellipse((sx - star_r, sy - star_r, sx + star_r, sy + star_r), fill=STAR_GOLD)

    # Central nucleus — a small dim cyan-gold dot at the centre to anchor
    # the eye and read as "the thing being orbited".
    core_r = int(diameter * 0.018)
    core_glow = int(diameter * 0.045)
    glow2 = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    g2d = ImageDraw.Draw(glow2)
    g2d.ellipse((cx - core_glow, cy - core_glow, cx + core_glow, cy + core_glow),
                fill=(*AURORA_CYAN, 90))
    glow2 = glow2.filter(ImageFilter.GaussianBlur(radius=diameter * 0.018))
    canvas = Image.alpha_composite(canvas, glow2)
    d = ImageDraw.Draw(canvas)
    d.ellipse((cx - core_r, cy - core_r, cx + core_r, cy + core_r), fill=PARCHMENT)

    return canvas


# ---------- ICON (800x800) ------------------------------------------------
def build_icon() -> Path:
    SIZE = 800
    canvas = base_canvas(SIZE, SIZE)
    # Centred orbital mark — sized to fill the upper half generously.
    mark = orbital_mark(int(SIZE * 0.62))
    mw, mh = mark.size
    canvas.paste(mark, ((SIZE - mw) // 2, int(SIZE * 0.06)), mark)

    # Channel name — two-line stack under the mark.
    d = ImageDraw.Draw(canvas)
    line1 = "COSMOS"
    line2 = "DECODED"
    f1 = ImageFont.truetype(BIG_CASLON, 110)
    f2 = ImageFont.truetype(BIG_CASLON, 90)
    bbox1 = d.textbbox((0, 0), line1, font=f1)
    bbox2 = d.textbbox((0, 0), line2, font=f2)
    w1, h1 = bbox1[2] - bbox1[0], bbox1[3] - bbox1[1]
    w2, h2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
    y_text = int(SIZE * 0.66)
    d.text(((SIZE - w1) // 2, y_text), line1, fill=PARCHMENT, font=f1,
           stroke_width=3, stroke_fill=DEEP_NAVY)
    d.text(((SIZE - w2) // 2, y_text + h1 + 6), line2, fill=STAR_GOLD, font=f2,
           stroke_width=3, stroke_fill=DEEP_NAVY)
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

    # Mark sits at the left edge of the safe area, vertically centred.
    mark_dim = int(SAFE_H * 0.95)   # ~402
    mark = orbital_mark(mark_dim)
    mark_x = sx0 + 10
    mark_y = sy0 + (SAFE_H - mark_dim) // 2
    canvas.paste(mark, (mark_x, mark_y), mark)

    d = ImageDraw.Draw(canvas)
    # Text column starts 50px right of the mark, fills remaining safe width.
    text_x = mark_x + mark_dim + 50
    text_max_w = (sx0 + SAFE_W) - text_x

    # Pick the largest font size that fits "Cosmos Decoded" within text_max_w.
    name = "Cosmos Decoded"
    name_size = 170
    while name_size > 60:
        f = ImageFont.truetype(BIG_CASLON, name_size)
        bb = d.textbbox((0, 0), name, font=f)
        if bb[2] - bb[0] <= text_max_w - 10:
            break
        name_size -= 4
    f_name = ImageFont.truetype(BIG_CASLON, name_size)
    f_tag = ImageFont.truetype(GEORGIA_ITAL, max(34, name_size // 4))
    f_kicker = ImageFont.truetype(GEORGIA, max(22, name_size // 6))

    tag = "How did we know?"
    kicker = "PHYSICS  ·  SPACE  ·  THE EXPERIMENTS THAT PROVED IT"

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

    d.text((text_x, name_y), name, fill=PARCHMENT, font=f_name,
           stroke_width=4, stroke_fill=DEEP_NAVY)
    rule_y = name_y + name_h + 14
    d.line([(text_x, rule_y), (text_x + max(name_w, tag_w), rule_y)],
           fill=STAR_GOLD, width=3)
    d.text((text_x, rule_y + 8), tag, fill=STAR_GOLD, font=f_tag)
    d.text((text_x, rule_y + 8 + tag_h + 18), kicker, fill=PARCHMENT, font=f_kicker)

    out = OUT_DIR / "banner_2560x1440.png"
    canvas.save(out)
    return out


if __name__ == "__main__":
    icon = build_icon()
    banner = build_banner()
    print(f"icon:   {icon}  ({icon.stat().st_size // 1024} KB)")
    print(f"banner: {banner}  ({banner.stat().st_size // 1024} KB)")
