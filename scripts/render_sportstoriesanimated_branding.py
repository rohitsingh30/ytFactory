"""One-off: render channel avatar + banner for SportsStoriesAnimated.

Aesthetic comes from channels/sportstoriesanimated.yaml — Tifo Football
hand-drawn editorial line-art on cream parchment.

Avatar:  1024×1024 native → 800×800 saved (YouTube channel icon spec).
Banner:  1344×768 native (16:9, multiple of 16) → 2048×1152 Lanczos
         upscale (YouTube channel art spec). The safe zone for TV/mobile
         is the centre 1235×338, so we compose around the centre.

Provider: z_image_turbo. Highest open-source prompt adherence on Apple
Silicon at ~half Flux's RAM (per pipeline/images.py docstring).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml
from PIL import Image

from pipeline.images import build_full_prompt, generate, lint_prompt

CHANNEL = "sportstoriesanimated"
OUT_DIR = ROOT / "data" / "intermediate" / CHANNEL / "branding"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Pull aesthetic + seed from the canonical channel config so brand and
# per-beat stills share a visual lineage.
cfg = yaml.safe_load((ROOT / "channels" / f"{CHANNEL}.yaml").read_text())
STYLE_PREFIX = cfg["image_style_prefix"]
BASE_SEED = cfg["image_seed"]  # 1872

PROVIDER = "z_image_turbo"
STEPS = 8  # Z-Image-Turbo recommended NFE for full quality


# ---------------- Avatar ----------------
# Iconic single subject readable at 24px feed thumbnail. A classic
# black-and-white panel football mid-arc is the most universal sports
# semaphore — clean silhouette survives aggressive downscaling.
avatar_prompt = build_full_prompt(
    style_prefix=STYLE_PREFIX,
    character_description=None,
    key_visual="a single classic black and white panel football suspended mid-flight with one soft curved motion arc behind it",
    scene=(
        "centered composition, the football is the only object in frame, "
        "bold confident inked outline, cream parchment background with "
        "subtle paper grain, lots of breathing room around the ball so the "
        "shape stays readable when scaled down to a tiny circular avatar"
    ),
    weighted=False,  # z_image_turbo doesn't parse compel weights
)

# ---------------- Banner ----------------
# Centre 1235×338 of a 2048×1152 banner is the cross-device safe zone.
# At 1344×768 native that maps to roughly the centre 810×225. Compose the
# striker dead-centre with negative space spilling left + right so the
# edges of the parchment can fade off-screen on TVs without losing the
# focal subject on mobile.
banner_prompt = build_full_prompt(
    style_prefix=STYLE_PREFIX,
    character_description=None,
    key_visual="a lone footballer silhouette frozen mid bicycle kick with the ball arcing above their boot",
    scene=(
        "wide horizontal editorial composition, the player and ball are "
        "centered in the frame, a faint distant stadium curve traced in "
        "the lower background as a quiet backdrop, expansive empty cream "
        "parchment to the left and right of the figure, dramatic but "
        "restrained, single subject focus"
    ),
    weighted=False,
)


def _check(prompt: str, label: str) -> None:
    warnings = lint_prompt(prompt)
    if warnings:
        print(f"[lint:{label}] WARNINGS:")
        for w in warnings:
            print(f"  - {w}")
    else:
        print(f"[lint:{label}] clean")


_check(avatar_prompt, "avatar")
_check(banner_prompt, "banner")


# ---------------- Render avatar ----------------
print("\n[render] avatar 1024×1024 →", OUT_DIR / "avatar.png")
avatar_native = OUT_DIR / "_avatar_native.png"
generate(
    prompt=avatar_prompt,
    style_prefix="",  # already baked into prompt
    seed=BASE_SEED,
    out_path=avatar_native,
    width=1024,
    height=1024,
    steps=STEPS,
    provider=PROVIDER,
)
# Downscale to YouTube avatar spec (800×800).
img = Image.open(avatar_native).convert("RGB")
img.resize((800, 800), Image.LANCZOS).save(OUT_DIR / "avatar.png")
print("[saved]", OUT_DIR / "avatar.png")


# ---------------- Render banner ----------------
print("\n[render] banner 1344×768 → upscale 2048×1152 →", OUT_DIR / "banner.png")
banner_native = OUT_DIR / "_banner_native.png"
generate(
    prompt=banner_prompt,
    style_prefix="",
    seed=BASE_SEED + 1,  # different seed so banner doesn't echo the avatar
    out_path=banner_native,
    width=1344,
    height=768,
    steps=STEPS,
    provider=PROVIDER,
)
img = Image.open(banner_native).convert("RGB")
img.resize((2048, 1152), Image.LANCZOS).save(OUT_DIR / "banner.png")
print("[saved]", OUT_DIR / "banner.png")

print("\nDone.")
print(f"  Avatar:  {OUT_DIR / 'avatar.png'}")
print(f"  Banner:  {OUT_DIR / 'banner.png'}")
