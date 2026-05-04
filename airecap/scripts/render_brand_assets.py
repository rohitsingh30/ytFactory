"""Render airecap channel icon + X header banner via Z-Image-Turbo.

Outputs:
    airecap/branding/icon_1024.png      — square mark, will become X avatar
    airecap/branding/icon_400.png       — X avatar size (resized from 1024)
    airecap/branding/banner_1344x448.png — Z-Image-Turbo native 3:1
    airecap/branding/banner_1500x500.png — X header (final)

Run:
    .venv/bin/python airecap/scripts/render_brand_assets.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from pipeline import images as img_mod  # noqa: E402

OUT_DIR = ROOT / "airecap" / "branding"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Match the channel's image_style_prefix from airecap/config.yaml so the
# brand reads as the same world as the content.
STYLE_PREFIX = (
    "clean isometric editorial illustration, flat vector geometry, "
    "restrained tech palette of indigo cobalt slate cream and accent amber, "
    "soft drop shadows, clear focal subject on uncluttered background, "
    "modern startup-blog aesthetic, single clear concept per frame, no text"
)

ICON_PROMPT = (
    "centered bold geometric mark on a flat indigo background, "
    "abstract neural-network glyph: three connected hexagonal nodes "
    "arranged in a triangle with subtle data-flow lines between them, "
    "amber accent on the top node, perfectly symmetrical, "
    "vector-clean edges, soft long shadows from upper-left, "
    "no human figures, no robots, no faces, no text, no letters, "
    "single clear focal subject, plenty of negative space around the mark, "
    "premium tech-brand identity"
)

BANNER_PROMPT = (
    "ultra-wide cinematic isometric illustration, three floating glass "
    "panels arranged left-to-right showing geometric representations of "
    "a code editor, a video timeline, and a 3D scene viewport, all "
    "connected by glowing amber data threads weaving between them, "
    "indigo-cobalt sky background fading to deep slate at the edges, "
    "soft long shadows, sharp clean vector edges, "
    "lower-left area visually calmer for profile-picture overlay, "
    "no human figures, no robots, no faces, no text, no letters, "
    "sense of AI threading through creative tools, premium tech-brand identity"
)

ICON_SEED = 4096       # power of two — matches channel image_seed
BANNER_SEED = 8192     # different seed for variety
STEPS = 4              # M2 Max compile-disabled path, matches channel


def _render(out: Path, prompt: str, seed: int, w: int, h: int) -> Path:
    print(f"[brand] rendering {out.name} ({w}x{h}, seed={seed})…")
    img_mod.generate(
        prompt=prompt,
        style_prefix=STYLE_PREFIX,
        seed=seed,
        out_path=out,
        width=w,
        height=h,
        steps=STEPS,
        provider="z_image_turbo",
    )
    print(f"[brand]   wrote {out}  ({out.stat().st_size // 1024} KB)")
    return out


def main() -> int:
    # Icon: native 1024x1024, then resize to 400x400 (X avatar).
    icon_native = OUT_DIR / "icon_1024.png"
    _render(icon_native, ICON_PROMPT, ICON_SEED, 1024, 1024)

    icon_400 = OUT_DIR / "icon_400.png"
    Image.open(icon_native).resize((400, 400), Image.LANCZOS).save(icon_400)
    print(f"[brand]   downsized → {icon_400}")

    # Banner: native 1344x448 (Z-Image-Turbo's 3:1 max), then upsize to
    # X's 1500x500.
    banner_native = OUT_DIR / "banner_1344x448.png"
    _render(banner_native, BANNER_PROMPT, BANNER_SEED, 1344, 448)

    banner_x = OUT_DIR / "banner_1500x500.png"
    Image.open(banner_native).resize((1500, 500), Image.LANCZOS).save(banner_x)
    print(f"[brand]   upsized → {banner_x}")

    print()
    print("Done. Upload to X:")
    print(f"  Profile picture: {icon_400}")
    print(f"  Header (banner): {banner_x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
