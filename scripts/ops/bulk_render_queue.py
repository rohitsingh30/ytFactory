"""Bulk render queue for the 100-variety-shorts pipeline.

Walks every script in mystoriesanimated/<niche>/scripts/ and renders
each one with the appropriate variant YAML. Runs N renders in
parallel (default 2 = cloudrun_z_image_turbo --max-instances). Skips
scripts that already have an mp4. Tolerates per-render failures and
keeps going.

Variant routing
---------------
Some niches map 1-to-1 to a YAML (tifu, aita_cooking, wiki, tih).
Some niches share scripts across two YAMLs (reddit_amitheasshole
splits between aita_animated and aita_text). For the shared niche,
this controller takes a `--split-aita ANIMATED:TEXT` ratio (default
30:30) and routes scripts alphabetically — first half → animated,
second half → text. This avoids slug collisions because each script
renders exactly once.

Usage
-----
    .venv/bin/python scripts/bulk_render_queue.py \\
        --channels mystoriesanimated/reddit_amitheasshole \\
                   mystoriesanimated/aita_cooking \\
                   mystoriesanimated/reddit_tifu \\
                   mystoriesanimated/wiki_oddities \\
                   mystoriesanimated/today_in_history \\
        --split-aita 30:30 \\
        --parallel 2 \\
        --log /tmp/bulk_render.log

Notes
-----
- This script does NOT chain TTS / image deps — it just calls
  scripts/make_shorts.py per slug, which handles all stages.
- Sources .env from the repo root before each render so cloud URLs
  are populated (until task #6 fix is everywhere).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# niche dir → (default variant yaml, optional split rule for shared dirs)
ROUTES: dict[str, str] = {
    "mystoriesanimated/aita_cooking":          "mystoriesanimated/variants/aita_cooking.yaml",
    "mystoriesanimated/reddit_tifu":           "mystoriesanimated/variants/tifu.yaml",
    "mystoriesanimated/wiki_oddities":         "mystoriesanimated/variants/wiki_oddities.yaml",
    "mystoriesanimated/today_in_history":      "mystoriesanimated/variants/today_in_history.yaml",
    "mystoriesanimated/reddit_amitheasshole":  "mystoriesanimated/variants/aita_animated.yaml",  # default; --split-aita overrides
}

AITA_NICHE = "mystoriesanimated/reddit_amitheasshole"


def _yaml_for_script(script_path: Path, niche_dir: str, split_aita: tuple[int, int]) -> str:
    """Pick variant YAML. For the shared aita niche, route
    alphabetically by slug into the animated/text split."""
    if niche_dir == AITA_NICHE:
        # Discover all scripts in this niche, sort, route by index.
        all_scripts = sorted(p.stem for p in (PROJECT_ROOT / niche_dir / "scripts").glob("*.json"))
        idx = all_scripts.index(script_path.stem)
        animated_n = split_aita[0]
        if idx < animated_n:
            return "mystoriesanimated/variants/aita_animated.yaml"
        return "mystoriesanimated/variants/aita_text.yaml"
    return ROUTES[niche_dir]


def _render_one(script_path: Path, yaml_path: str, log_path: Path) -> tuple[str, bool, str]:
    """Run one render. Returns (slug, ok, msg).

    Sources .env so cloud URLs are populated. The render itself
    handles all retries; we just observe pass/fail at the
    subprocess level.
    """
    slug = script_path.stem
    cmd = (
        "set -a && source .env && set +a && "
        f"PYTHONPATH=. .venv/bin/python scripts/make_shorts.py "
        f"--channel {yaml_path} --script {script_path}"
    )
    t0 = time.time()
    log = log_path.open("a")
    log.write(f"\n\n=========== START {slug} ({yaml_path}) — {time.strftime('%H:%M:%S')} ===========\n")
    log.flush()
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            cwd=PROJECT_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=3600,  # 60-min hard cap per render
        )
        dt = time.time() - t0
        if proc.returncode == 0:
            return slug, True, f"OK in {dt:.0f}s"
        return slug, False, f"exit {proc.returncode} after {dt:.0f}s"
    except subprocess.TimeoutExpired:
        return slug, False, f"TIMEOUT >3600s"
    except Exception as e:
        return slug, False, f"EXC {e}"
    finally:
        log.close()


def _enumerate_jobs(channels: list[str], split_aita: tuple[int, int]) -> list[tuple[Path, str]]:
    """Build the (script, yaml) job list, skipping anything with an mp4 already."""
    jobs: list[tuple[Path, str]] = []
    for niche in channels:
        scripts_dir = PROJECT_ROOT / niche / "scripts"
        shorts_dir = PROJECT_ROOT / niche / "shorts"
        if not scripts_dir.exists():
            print(f"  [skip] no scripts in {niche}")
            continue
        for sp in sorted(scripts_dir.glob("*.json")):
            mp4 = shorts_dir / f"{sp.stem}.mp4"
            if mp4.exists():
                continue  # already rendered, skip
            yaml_path = _yaml_for_script(sp, niche, split_aita)
            jobs.append((sp, yaml_path))
    return jobs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", nargs="+", required=True,
                    help="Niche dirs to walk")
    ap.add_argument("--split-aita", default="30:30",
                    help="N:M alphabetical split for reddit_amitheasshole "
                         "(N → animated, M → text). Default 30:30.")
    ap.add_argument("--parallel", type=int, default=2,
                    help="Concurrent renders (default 2 = "
                         "cloudrun_z_image_turbo --max-instances).")
    ap.add_argument("--log", default="/tmp/bulk_render.log",
                    help="Output log path (per-render stdout appended)")
    ap.add_argument("--dry-run", action="store_true",
                    help="List jobs, don't render")
    args = ap.parse_args()

    parts = args.split_aita.split(":")
    split_aita = (int(parts[0]), int(parts[1]))

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)

    jobs = _enumerate_jobs(args.channels, split_aita)
    print(f"Found {len(jobs)} jobs to render (parallel={args.parallel})")
    if args.dry_run:
        for sp, yp in jobs[:20]:
            print(f"  {sp.relative_to(PROJECT_ROOT)} → {yp}")
        if len(jobs) > 20:
            print(f"  ... and {len(jobs)-20} more")
        return 0

    if not jobs:
        print("Nothing to do.")
        return 0

    print(f"Tail the log: tail -f {args.log}")
    t_start = time.time()
    done = 0
    failed: list[tuple[str, str]] = []
    with ProcessPoolExecutor(max_workers=args.parallel) as pool:
        futs = {pool.submit(_render_one, sp, yp, log_path): (sp, yp) for sp, yp in jobs}
        for f in as_completed(futs):
            done += 1
            slug, ok, msg = f.result()
            yp = futs[f][1]
            elapsed = time.time() - t_start
            mark = "✓" if ok else "✗"
            print(f"  [{done}/{len(jobs)}] {mark} {slug:60s} | {yp.split('/')[-1]:30s} | {msg} | t={elapsed:.0f}s")
            if not ok:
                failed.append((slug, msg))

    print(f"\nDone. {done - len(failed)} ok / {len(failed)} failed in {time.time()-t_start:.0f}s")
    for slug, msg in failed:
        print(f"  ✗ {slug}: {msg}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
