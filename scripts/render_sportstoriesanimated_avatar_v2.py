"""Avatar re-roll. v1 rendered an American football because the bare
word "football" biases toward gridiron in the training set. Force the
soccer reading with explicit terminology + visual cues (hexagon and
pentagon panels, spherical, on the grass)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml
from PIL import Image

from pipeline.images import build_full_prompt, generate

CHANNEL = "sportstoriesanimated"
OUT_DIR = ROOT / "data" / "intermediate" / CHANNEL / "branding"

cfg = yaml.safe_load((ROOT / "channels" / f"{CHANNEL}.yaml").read_text())
STYLE_PREFIX = cfg["image_style_prefix"]
SEED = cfg["image_seed"]

prompt = build_full_prompt(
    style_prefix=STYLE_PREFIX,
    character_description=None,
    key_visual=(
        "a single classic spherical association football soccer ball with "
        "black hexagonal and white pentagonal panels suspended mid-flight "
        "with one soft curved motion arc behind it"
    ),
    scene=(
        "centered composition, the round soccer ball is the only object "
        "in frame, bold confident inked outline, cream parchment "
        "background, lots of empty space around the ball, no american "
        "football no rugby ball, perfectly round sphere"
    ),
    weighted=False,
)

print("[render] avatar v2 1024x1024")
native = OUT_DIR / "_avatar_v2_native.png"
generate(
    prompt=prompt,
    style_prefix="",
    seed=SEED + 7,  # different seed to break the prior interpretation
    out_path=native,
    width=1024,
    height=1024,
    steps=8,
    provider="z_image_turbo",
)
img = Image.open(native).convert("RGB")
img.resize((800, 800), Image.LANCZOS).save(OUT_DIR / "avatar.png")
print("[saved]", OUT_DIR / "avatar.png")
