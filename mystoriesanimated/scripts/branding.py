"""One-off: render channel avatar + banner for MyStoriesAnimated.

Aesthetic + character pulled from mystoriesanimated/config.yaml so the
brand matches the look viewers see inside the videos themselves.

Avatar:  1024×1024 native → 800×800 saved (YouTube channel icon spec).
Banner:  1344×768 native → Lanczos upscale to 2048×1152.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml
from PIL import Image

from pipeline.images import build_full_prompt, generate, lint_prompt

CHANNEL = "mystoriesanimated"
OUT_DIR = ROOT / "data" / "intermediate" / CHANNEL / "branding"
OUT_DIR.mkdir(parents=True, exist_ok=True)

cfg = yaml.safe_load((ROOT / "channels" / f"{CHANNEL}.yaml").read_text())
STYLE_PREFIX = cfg["image_style_prefix"]
CHARACTER = cfg["character_description"]
SEED = cfg["image_seed"]
PROVIDER = cfg.get("image_provider", "z_image_turbo")
STEPS = 8  # bump above the channel's per-beat 4 steps for branding quality


# ---------------- Avatar ----------------
# Single round-headed character bust front-on, friendly expression. Big
# head shape survives downscale to 24px. Empty pastel background — no
# props, no second subject, no thought bubble (text-bait).
avatar_prompt = build_full_prompt(
    style_prefix=STYLE_PREFIX,
    character_description=CHARACTER,
    key_visual="centered front-facing portrait bust of the round-headed character smiling gently",
    scene=(
        "the character fills most of the frame, head and shoulders only, "
        "facing forward, soft warm beige background, single subject, "
        "lots of headroom around the round head so the silhouette stays "
        "legible at small sizes"
    ),
    weighted=False,
)


# ---------------- Banner ----------------
# Wide horizontal: the channel character centred, mid-gesture (hand
# raised, mouth slightly open — telling a story). Pastel background only,
# no props or scenery to preserve safe-zone clarity.
banner_prompt = build_full_prompt(
    style_prefix=STYLE_PREFIX,
    character_description=CHARACTER,
    key_visual="the round-headed character standing centred and slightly turned, one hand raised mid-gesture as if telling a story",
    scene=(
        "wide horizontal composition, the character is the only subject, "
        "centred in the frame, lots of empty soft pastel background "
        "(warm beige and dusty pink wash) spilling left and right, "
        "expressive but calm posture, single subject focus"
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
    style_prefix="",
    seed=SEED,
    out_path=avatar_native,
    width=1024,
    height=1024,
    steps=STEPS,
    provider=PROVIDER,
)
img = Image.open(avatar_native).convert("RGB")
img.resize((800, 800), Image.LANCZOS).save(OUT_DIR / "avatar.png")
print("[saved]", OUT_DIR / "avatar.png")


# ---------------- Render banner ----------------
print("\n[render] banner 1344×768 → upscale 2048×1152 →", OUT_DIR / "banner.png")
banner_native = OUT_DIR / "_banner_native.png"
generate(
    prompt=banner_prompt,
    style_prefix="",
    seed=SEED + 1,
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
