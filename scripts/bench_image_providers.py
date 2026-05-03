"""A/B benchmark — same prompt, same seed, multiple image providers.

Use this to decide whether to switch ``image_provider`` in a channel
YAML. Renders each prompt through every requested provider, prints
wall-clock per image, and saves outputs side-by-side under
``data/_bench/<run_id>/`` so the visual comparison is one folder open.

Both mflux providers (Flux Schnell, Z-Image-Turbo) bind MLX device
streams to the calling thread, so we run sequentially on the main
thread — never parallelise the providers here or you'll trip
"There is no Stream(gpu, 1)".

Usage:
    .venv/bin/python scripts/bench_image_providers.py \\
        --providers mflux z_image_turbo \\
        --channel channels/aita_animated.yaml \\
        --prompts data/cache/<slug>/prompts.json \\
        --beats 0 1 2

If --prompts is omitted, falls back to a small built-in scene set so
the script works on a fresh checkout without rendering a Short first.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Built-in fallback prompts — chosen to mirror the AITA-channel style
# (one human subject, cozy interior, mid-shot) so a fresh Schnell vs
# Z-Image comparison isn't dominated by a degenerate prompt.
_BUILTIN_PROMPTS = [
    {
        "key_visual": "a young woman holding apartment keys, proud",
        "scene": "Amelia standing in the doorway of a small pastel apartment, "
                 "morning sunlight, hand on hip, chin lifted, warm-beige walls",
    },
    {
        "key_visual": "a teenage girl scrolling her phone on a bedroom rug",
        "scene": "younger sister with short blonde hair sitting cross-legged on "
                 "a dusty-pink rug, slouched and bored, plush throw nearby",
    },
    {
        "key_visual": "a frustrated mother in a kitchen, arms crossed",
        "scene": "middle-aged woman in a sage-green kitchen, arms crossed, "
                 "lips pressed thin, late-afternoon window light",
    },
]


def _load_prompts_from_file(path: Path, beats: list[int] | None) -> list[dict[str, str]]:
    raw = json.loads(path.read_text())
    if beats is None:
        return raw[:3]  # default to first 3
    return [raw[i] for i in beats if 0 <= i < len(raw)]


def _bench_one(
    *,
    provider: str,
    full_prompt: str,
    seed: int,
    out_path: Path,
    width: int,
    height: int,
    steps: int,
) -> tuple[float, Path]:
    """Generate one image, return (wall_time_s, out_path)."""
    from pipeline import images

    t0 = time.time()
    images.generate(
        prompt=full_prompt,
        style_prefix="",  # already baked into full_prompt by caller
        seed=seed,
        out_path=out_path,
        width=width,
        height=height,
        steps=steps,
        provider=provider,
    )
    return time.time() - t0, out_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--providers", nargs="+", required=True,
        choices=["mflux", "z_image_turbo", "sdxl_lightning", "sd_turbo"],
        help="Image providers to compare (run sequentially in order).",
    )
    ap.add_argument(
        "--channel", default="channels/aita_animated.yaml",
        help="Channel YAML to source style_prefix / character_description / "
             "dims / steps from. Default: channels/aita_animated.yaml.",
    )
    ap.add_argument(
        "--prompts", type=Path, default=None,
        help="Path to a prompts.json (object form: key_visual + scene). "
             "If omitted, uses a built-in 3-prompt set.",
    )
    ap.add_argument(
        "--beats", type=int, nargs="*", default=None,
        help="Indices into prompts.json to render. Default: [0,1,2].",
    )
    ap.add_argument(
        "--seed", type=int, default=12345,
        help="Locked seed for fair comparison across providers.",
    )
    ap.add_argument(
        "--steps-override", type=int, default=None,
        help="Override channel YAML's image_steps (e.g. 8 to test "
             "Z-Image-Turbo at its sweet spot).",
    )
    ap.add_argument(
        "--steps-sweep", type=int, nargs="+", default=None,
        help="Render at multiple step counts per provider (e.g. "
             "--steps-sweep 2 4 6 8) to map the per_step Pareto frontier. "
             "Mutually exclusive with --steps-override; ignored if both set.",
    )
    ap.add_argument(
        "--out-dir", type=Path,
        default=PROJECT_ROOT / "data" / "_bench",
        help="Output root. A timestamped subfolder is created under it.",
    )
    args = ap.parse_args()

    cfg_path = PROJECT_ROOT / args.channel
    cfg: dict[str, Any] = yaml.safe_load(cfg_path.read_text())

    width = int(cfg.get("image_width", 768))
    height = int(cfg.get("image_height", 1344))
    # If a sweep is requested, run every (provider, steps) cell. Otherwise
    # use the single override value (or fall back to the channel YAML).
    if args.steps_sweep:
        steps_list = sorted({int(s) for s in args.steps_sweep})
    else:
        steps_list = [int(args.steps_override or cfg.get("image_steps", 4))]
    style_prefix = cfg.get("image_style_prefix", "")
    character_description = cfg.get("character_description", "")

    if args.prompts:
        prompts = _load_prompts_from_file(args.prompts, args.beats)
    else:
        idx = args.beats or [0, 1, 2]
        prompts = [_BUILTIN_PROMPTS[i] for i in idx if 0 <= i < len(_BUILTIN_PROMPTS)]

    if not prompts:
        print("no prompts to render")
        return 1

    run_id = time.strftime("%Y%m%d-%H%M%S")
    out_root = args.out_dir / run_id
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"[bench] run_id={run_id}")
    print(f"[bench] channel={args.channel}")
    print(f"[bench] providers={args.providers}")
    print(f"[bench] dims={width}x{height} steps_sweep={steps_list} seed={args.seed}")
    print(f"[bench] prompts={len(prompts)}  out={out_root}")

    from pipeline import images

    # (provider, steps) → list of (beat_idx, scene[:40], wall_time_s, out_path)
    results: dict[tuple[str, int], list[tuple[int, str, float, Path]]] = {
        (p, s): [] for p in args.providers for s in steps_list
    }

    # Provider-major (then steps-major) loop: load each model once, render
    # every prompt through it at every step count before moving to the
    # next provider. Avoids paying the cold-load multiple times per
    # provider. Prompt-major would be fairer for warm-state comparison
    # but unrealistic for a single-Short workflow where one provider is
    # picked and used for every beat.
    for provider in args.providers:
        for steps in steps_list:
            print(f"\n[bench] === provider={provider} steps={steps} ===")
            for i, p in enumerate(prompts):
                full_prompt = images.build_full_prompt(
                    style_prefix=style_prefix,
                    character_description=character_description,
                    key_visual=p.get("key_visual", ""),
                    scene=p.get("scene", ""),
                    weighted=(provider != "mflux"),
                )
                out_path = out_root / provider / f"steps_{steps:02d}" / f"beat_{i:02d}.png"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                print(f"  [{i+1}/{len(prompts)}] {p.get('scene','')[:60]!r}")
                dt, _ = _bench_one(
                    provider=provider,
                    full_prompt=full_prompt,
                    seed=args.seed,
                    out_path=out_path,
                    width=width,
                    height=height,
                    steps=steps,
                )
                print(f"     wall={dt:.1f}s")
                results[(provider, steps)].append(
                    (i, p.get("scene", "")[:40], dt, out_path)
                )

    # ---- Summary + Pareto frontier --------------------------------
    print("\n[bench] === summary ===")
    print(f"  {'provider':<16s} {'steps':>5s} {'cold':>7s} {'warm_avg':>9s} "
          f"{'per_step':>9s} {'total':>7s}")
    cells: list[dict] = []
    for (provider, steps), rows in results.items():
        if not rows:
            continue
        cold = rows[0][2]
        warm = rows[1:]
        warm_avg = (sum(r[2] for r in warm) / len(warm)) if warm else float("nan")
        per_step = warm_avg / steps if warm else float("nan")
        total = sum(r[2] for r in rows)
        cells.append({
            "provider": provider, "steps": steps, "cold_s": round(cold, 2),
            "warm_avg_s": round(warm_avg, 2), "per_step_s": round(per_step, 3),
            "total_s": round(total, 2),
            "n_warm": len(warm),
        })
        print(f"  {provider:<16s} {steps:>5d} {cold:>6.1f}s {warm_avg:>8.1f}s "
              f"{per_step:>8.2f}s {total:>6.1f}s")

    # Pareto frontier: for each provider, the steps count with the
    # lowest warm_avg. For an N-beat video this is the engineering
    # answer: "use this provider with this many steps."
    print(f"\n[bench] === recommendation per provider ===")
    by_provider: dict[str, list[dict]] = {}
    for c in cells:
        by_provider.setdefault(c["provider"], []).append(c)
    recommendations: list[dict] = []
    for provider, prov_cells in by_provider.items():
        best = min(prov_cells, key=lambda c: c["warm_avg_s"])
        # Quality vs speed tradeoff: highest steps that's within 30% of
        # the fastest configuration. Lets the user trade some speed for
        # better convergence if the prompt is hard.
        threshold = best["warm_avg_s"] * 1.30
        quality_pick = max(
            (c for c in prov_cells if c["warm_avg_s"] <= threshold),
            key=lambda c: c["steps"],
            default=best,
        )
        recommendations.append({
            "provider": provider,
            "fastest_steps": best["steps"],
            "fastest_warm_s": best["warm_avg_s"],
            "quality_steps": quality_pick["steps"],
            "quality_warm_s": quality_pick["warm_avg_s"],
        })
        print(f"  {provider:<16s}  fastest={best['steps']}-step "
              f"({best['warm_avg_s']:.1f}s)  "
              f"quality={quality_pick['steps']}-step "
              f"({quality_pick['warm_avg_s']:.1f}s, ≤30% slower)")

    # Cross-provider pick — lowest warm_avg overall.
    if cells:
        winner = min(cells, key=lambda c: c["warm_avg_s"])
        per_video = winner["warm_avg_s"] * len(prompts)  # rough, ignores cold
        print(f"\n[bench] OVERALL FASTEST: {winner['provider']} @ "
              f"{winner['steps']}-step → {winner['warm_avg_s']:.1f}s/img warm. "
              f"For an N={len(prompts)}-beat video that's "
              f"~{per_video:.0f}s of GPU time (excl. cold load).")

    # Persist a machine-readable summary alongside the images so future
    # comparisons can chart provider trend over time without re-running.
    (out_root / "summary.json").write_text(json.dumps({
        "run_id": run_id,
        "channel": str(args.channel),
        "dims": [width, height],
        "steps_sweep": steps_list,
        "seed": args.seed,
        "providers": args.providers,
        "cells": cells,
        "recommendations": recommendations,
        "results": {
            f"{provider}@{steps}": [
                {"beat": i, "scene": s, "wall_s": round(dt, 2),
                 "path": str(p.relative_to(out_root))}
                for (i, s, dt, p) in rows
            ]
            for (provider, steps), rows in results.items()
        },
    }, indent=2))
    print(f"\n[bench] summary: {out_root}/summary.json")
    print(f"[bench] open {out_root} to compare images side-by-side")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
