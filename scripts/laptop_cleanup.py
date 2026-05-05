#!/usr/bin/env python3
"""Laptop disk reclaim — wipes legacy state that lives in cloud now.

After the migration the cloud is the source of truth. This script reclaims
disk on the laptop by deleting categories of data that we don't need
locally anymore. Model weights and per-channel branding stay put.

Usage:
    .venv/bin/python scripts/laptop_cleanup.py --dry-run
    .venv/bin/python scripts/laptop_cleanup.py            # actually delete
    .venv/bin/python scripts/laptop_cleanup.py --keep-shorts  # keep finished mp4s

Categories (in delete order, smallest+safest first):

    data/_jobs/         — telemetry, lives in Firestore now
    data/uploads/       — YT upload metadata, in Firestore
    data/tts_tests/     — dev-only TTS smoke artifacts
    data/scratch/       — per-job scratch (replaced by agent runner scratch)
    data/critiques/     — critique outputs, can be regenerated
    data/intermediate/  — per-stage JSON / WAV / PNG (in GCS now)
            — finished mp4s (in GCS, lifecycle-purged)
    data/cache/<slug>/  — per-render content-hashed image cache,
                          older than --cache-age-h hours

What we keep:
    .env, channels/, pipeline/, web/static/voice_samples/,
    data/cache/* model weight directories (anything that doesn't look
    like a per-render slug), data/research/, mystoriesanimated/cooking_bg_queue.yaml.

Run before #13 (decommission monolith) so the disk reclaim is visible.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "data"

# Channels with per-render scratch dirs that the renderer recreates on
# demand. Keeping this list explicit (rather than scanning every dir at
# repo root) prevents accidentally pruning dirs that AREN'T channel
# dirs (control/, workers/, etc.).
CHANNEL_DIRS = (
    "airecap",
    "cosmosdecoded",
    "hindutavaanimated",
    "historyrecapped",
    "mystoriesanimated",
    "rhymetimejunction",
    "sportstoriesanimated",
)

# Per-render artefacts under <channel>/cache/<slug>/ that are
# regenerated on demand. ALL safe to delete — the renderers re-create
# via mkdir(parents=True, exist_ok=True). Add new render-stage
# subdirs here when a future renderer adds them.
RENDER_SCRATCH_SUBDIRS = (
    "tts_chunks",
    "long_clips",
    "panel_segments",
    "panels",
    "captions",
    "chapter_cards",
    "_flagged_words",
)
RENDER_SCRATCH_FILES = (
    "_concat_clips.txt",
    "_concat_list.txt",
    "_silence.wav",
    "_tail_slow.mp4",
    "_head.mp4",
)


def _human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PiB"


def _dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def _wipe_dir(p: Path, *, dry_run: bool) -> int:
    if not p.exists():
        return 0
    size = _dir_size(p)
    print(f"  {p.relative_to(PROJECT_ROOT)}{_human(size):>12}{'  (dry)' if dry_run else ''}")
    if not dry_run:
        shutil.rmtree(p, ignore_errors=True)
        # Recreate the empty dir so existing code that expects it doesn't break.
        p.mkdir(parents=True, exist_ok=True)
    return size


def _wipe_old_cache(*, dry_run: bool, cache_age_h: float) -> int:
    """Delete data/cache/<slug>/ entries older than cache_age_h.

    Anything that doesn't look like a per-render slug (no `img_*.png` files)
    is left alone — that's where model weights live.
    """
    cache = DATA / "cache"
    if not cache.exists():
        return 0
    cutoff = time.time() - cache_age_h * 3600
    total = 0
    for sub in sorted(cache.iterdir()):
        if not sub.is_dir():
            continue
        # Heuristic: per-render slugs hold per-beat PNGs.
        is_per_render = any(sub.glob("img_*.png")) or any(sub.glob("prompts.json"))
        if not is_per_render:
            continue
        mtime = sub.stat().st_mtime
        if mtime > cutoff:
            continue
        size = _dir_size(sub)
        total += size
        print(f"  {sub.relative_to(PROJECT_ROOT)}{_human(size):>12}"
              f"  (age {(time.time()-mtime)/3600:.0f}h){'  (dry)' if dry_run else ''}")
        if not dry_run:
            shutil.rmtree(sub, ignore_errors=True)
    return total


def _wipe_render_scratch(*, dry_run: bool, render_age_h: float) -> int:
    """Delete <channel>/cache/<slug>/{tts_chunks,long_clips,panel_segments,
    panels,captions,chapter_cards,_flagged_words}/ entries older than
    ``render_age_h`` (default 14 days).

    Walks every channel listed in ``CHANNEL_DIRS``. The mtime check is
    on the slug dir itself (the parent), not on individual subdirs —
    a recently-touched render keeps ALL of its scratch stages, even if
    one stage is older. That matches how the renderer reuses cached
    artefacts when re-running with the same slug.

    The slug dir itself is left in place (just emptied of large
    subdirs); narration.wav / beats.json / video.mp4 stay so a re-run
    doesn't pay the TTS / ASR / trim costs again. Only the
    GB-class subdirs (chunked TTS wavs, trimmed clip mp4s, panel PNGs,
    caption PNGs) are pruned.
    """
    cutoff = time.time() - render_age_h * 3600
    total = 0
    for chan in CHANNEL_DIRS:
        cache_root = PROJECT_ROOT / chan / "cache"
        if not cache_root.exists():
            continue
        for slug_dir in sorted(cache_root.iterdir()):
            if not slug_dir.is_dir() or slug_dir.name.startswith("_"):
                continue
            try:
                mtime = slug_dir.stat().st_mtime
            except OSError:
                continue
            if mtime > cutoff:
                continue
            for sub_name in RENDER_SCRATCH_SUBDIRS:
                sub = slug_dir / sub_name
                if not sub.exists():
                    continue
                size = _dir_size(sub)
                if size == 0:
                    if not dry_run:
                        try:
                            sub.rmdir()
                        except OSError:
                            pass
                    continue
                total += size
                print(f"  {sub.relative_to(PROJECT_ROOT)}{_human(size):>12}"
                      f"  (age {(time.time()-mtime)/3600/24:.0f}d){'  (dry)' if dry_run else ''}")
                if not dry_run:
                    shutil.rmtree(sub, ignore_errors=True)
            for fname in RENDER_SCRATCH_FILES:
                f = slug_dir / fname
                if not f.exists():
                    continue
                try:
                    sz = f.stat().st_size
                except OSError:
                    continue
                total += sz
                print(f"  {f.relative_to(PROJECT_ROOT)}{_human(sz):>12}{'  (dry)' if dry_run else ''}")
                if not dry_run:
                    f.unlink(missing_ok=True)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="show what would be deleted; no actual changes")
    ap.add_argument("--keep-shorts", action="store_true", help="don't delete  (finished mp4s)")
    ap.add_argument("--cache-age-h", type=float, default=24.0,
                    help="purge per-render image cache older than this many hours (default 24)")
    ap.add_argument("--render-age-h", type=float, default=14.0 * 24.0,
                    help="purge per-channel render scratch (tts_chunks/, long_clips/, panel_segments/, "
                         "panels/, captions/, chapter_cards/) older than this many hours (default 336 = 14 days)")
    ap.add_argument("--skip-render-scratch", action="store_true",
                    help="skip the per-channel render scratch sweep (only do data/ cleanup)")
    args = ap.parse_args()

    print(f"\nLaptop disk reclaim (project root: {PROJECT_ROOT})")
    print(f"  mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"  data/ cache age cutoff: {args.cache_age_h}h")
    print(f"  per-channel render scratch age cutoff: {args.render_age_h}h "
          f"({args.render_age_h/24:.1f} days)\n")

    print("Wiping ephemeral runtime state:")
    total = 0
    total += _wipe_dir(DATA / "_jobs", dry_run=args.dry_run)
    total += _wipe_dir(DATA / "uploads", dry_run=args.dry_run)
    total += _wipe_dir(DATA / "tts_tests", dry_run=args.dry_run)
    total += _wipe_dir(DATA / "scratch", dry_run=args.dry_run)
    total += _wipe_dir(DATA / "critiques", dry_run=args.dry_run)
    total += _wipe_dir(DATA / "intermediate", dry_run=args.dry_run)

    if not args.keep_shorts:
        print("\nWiping finished shorts (in GCS):")
        total += _wipe_dir(DATA / "shorts", dry_run=args.dry_run)

    print(f"\nWiping per-render image cache (age >= {args.cache_age_h}h):")
    total += _wipe_old_cache(dry_run=args.dry_run, cache_age_h=args.cache_age_h)

    if not args.skip_render_scratch:
        print(f"\nWiping per-channel render scratch (age >= {args.render_age_h/24:.1f} days):")
        total += _wipe_render_scratch(dry_run=args.dry_run, render_age_h=args.render_age_h)

    print(f"\nTotal {'would be ' if args.dry_run else ''}reclaimed: {_human(total)}")
    if args.dry_run:
        print("Re-run without --dry-run to actually delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
