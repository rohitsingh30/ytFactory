"""Banner-only re-render for MyStoriesAnimated. Avatar already saved
in v1; banner died mid-render."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml
from PIL import Image

from pipeline.images import build_full_prompt, generate

CHANNEL = "mystoriesanimated"
OUT_DIR = ROOT / "data" / "intermediate" / CHANNEL / "branding"

cfg = yaml.safe_load((ROOT / "channels" / f"{CHANNEL}.yaml").read_text())
STYLE_PREFIX = cfg["image_style_prefix"]
CHARACTER = cfg["character_description"]
SEED = cfg["image_seed"]
PROVIDER = cfg.get("image_provider", "z_image_turbo")

prompt = build_full_prompt(
    style_prefix=STYLE_PREFIX,
    character_description=CHARACTER,
    key_visual="the round-headed character standing centred and slightly turned, one hand raised mid-gesture as if telling a story",
    scene=(
        "wide horizontal composition, single subject centred, "
        "soft pastel background of warm beige and dusty pink wash "
        "spilling left and right, expressive but calm posture"
    ),
    weighted=False,
)

print("[render] banner 1344×768 → 2048×1152")
native = OUT_DIR / "_banner_native.png"
generate(
    prompt=prompt,
    style_prefix="",
    seed=SEED + 1,
    out_path=native,
    width=1344,
    height=768,
    steps=8,
    provider=PROVIDER,
)
img = Image.open(native).convert("RGB")
img.resize((2048, 1152), Image.LANCZOS).save(OUT_DIR / "banner.png")
print("[saved]", OUT_DIR / "banner.png")
