"""Daily-cron upload picker — uploads the next N rendered-but-unuploaded
mp4s from the mystoriesanimated/* niches.

Designed to be run by launchd every ~2.4 hours, 10 times per day, to
spread 100 prerendered shorts across ~10 days. Each invocation:

1. Walks every niche dir under mystoriesanimated/.
2. Finds mp4s that exist but have no upload record.
3. Picks the next --count (default 1) by (niche-rotation, oldest-mtime).
4. Uploads each as private with `publish_at = now + 20 min`.
5. Records the upload so the next run skips it.

Niche rotation: alternates niches between calls so the upload feed
shows visual variety, not 30 AITA-animated in a row. State stored in
mystoriesanimated/.upload_rotation_cursor.

Logs to /tmp/upload_next.log. Exits 0 on success or "nothing to do",
non-zero on any upload error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.upload import upload as up
from pipeline.quality.evals import is_held

CHANNELS_ROOT = PROJECT_ROOT / "mystoriesanimated"
ROTATION_STATE = CHANNELS_ROOT / ".upload_rotation_cursor.json"

# niche dir → variant yaml (used for upload metadata derivation)
NICHE_TO_YAML: dict[str, Path] = {
    "reddit_amitheasshole":  PROJECT_ROOT / "mystoriesanimated/variants/aita_animated.yaml",
    "aita_cooking":           PROJECT_ROOT / "mystoriesanimated/variants/aita_cooking.yaml",
    "reddit_tifu":            PROJECT_ROOT / "mystoriesanimated/variants/tifu.yaml",
    "wiki_oddities":          PROJECT_ROOT / "mystoriesanimated/variants/wiki_oddities.yaml",
    "today_in_history":       PROJECT_ROOT / "mystoriesanimated/variants/today_in_history.yaml",
}

NICHE_ORDER = list(NICHE_TO_YAML.keys())  # rotation order


def _load_cursor() -> int:
    if not ROTATION_STATE.exists():
        return 0
    try:
        return int(json.loads(ROTATION_STATE.read_text()).get("idx", 0))
    except Exception:
        return 0


def _save_cursor(idx: int) -> None:
    ROTATION_STATE.write_text(json.dumps({"idx": idx}))


def _candidates_for_niche(niche: str) -> list[tuple[str, Path]]:
    """Return [(slug, mp4_path)] for unuploaded shorts in this niche,
    sorted by mp4 mtime (oldest first)."""
    shorts_dir = CHANNELS_ROOT / niche / "shorts"
    uploads_dir = CHANNELS_ROOT / niche / "uploads"
    if not shorts_dir.exists():
        return []
    out: list[tuple[str, Path, float]] = []
    for mp4 in shorts_dir.glob("*.mp4"):
        slug = mp4.stem
        if (uploads_dir / f"{slug}.json").exists():
            continue  # already uploaded
        if is_held("mystoriesanimated", slug):
            continue  # held by /ingest-critiques (verdict=FIX/BLOCK)
        out.append((slug, mp4, mp4.stat().st_mtime))
    out.sort(key=lambda t: t[2])
    return [(slug, mp4) for slug, mp4, _ in out]


def _upload_one(niche: str, slug: str, mp4: Path, publish_at_iso: str) -> bool:
    """Upload one short. Returns True on success."""
    yaml_path = NICHE_TO_YAML[niche]
    chan_yaml = yaml.safe_load(yaml_path.read_text())
    channel_dir = f"mystoriesanimated/{niche}"
    script_path = CHANNELS_ROOT / niche / "scripts" / f"{slug}.json"
    raw_path = CHANNELS_ROOT / niche / "raw" / f"{slug}.json"
    if not script_path.exists():
        print(f"  ✗ {slug}: missing script {script_path}")
        return False
    script = json.loads(script_path.read_text())
    raw = json.loads(raw_path.read_text()) if raw_path.exists() else None
    print(f"  uploading {slug} (publish_at={publish_at_iso})")
    try:
        rec = up.upload_short(
            project_root=PROJECT_ROOT,
            channel_yaml=chan_yaml,
            channel_dir=channel_dir,
            slug=slug,
            mp4_path=mp4,
            script=script,
            raw=raw,
            privacy_override="private",  # required for publishAt
            publish_at=publish_at_iso,
            skip_critic=True,  # critic ran during render; don't re-pay
        )
        print(f"  ✓ {slug}: {rec.get('url')}")
        return True
    except Exception as e:
        print(f"  ✗ {slug}: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1,
                    help="How many to upload this run (default 1).")
    ap.add_argument("--publish-after-min", type=int, default=20,
                    help="Schedule publish_at this many minutes from now (≥15).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Pick + print but don't upload.")
    args = ap.parse_args()

    now_utc = datetime.now(timezone.utc)
    publish_at = now_utc + timedelta(minutes=args.publish_after_min)
    publish_at_iso = publish_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    cursor = _load_cursor()
    uploaded = 0
    failed = 0
    tried_niches = 0

    while uploaded + failed < args.count and tried_niches < len(NICHE_ORDER) * args.count:
        niche = NICHE_ORDER[cursor % len(NICHE_ORDER)]
        cands = _candidates_for_niche(niche)
        tried_niches += 1
        if not cands:
            cursor = (cursor + 1) % len(NICHE_ORDER)
            continue
        slug, mp4 = cands[0]
        if args.dry_run:
            print(f"  DRY: {niche}/{slug} → publish_at={publish_at_iso}")
            uploaded += 1
        else:
            ok = _upload_one(niche, slug, mp4, publish_at_iso)
            if ok:
                uploaded += 1
            else:
                failed += 1
        cursor = (cursor + 1) % len(NICHE_ORDER)
        # Bump publish_at slightly so consecutive uploads in this run don't
        # all collide on the same minute.
        publish_at = publish_at + timedelta(minutes=2)
        publish_at_iso = publish_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    _save_cursor(cursor)
    total_left = sum(len(_candidates_for_niche(n)) for n in NICHE_ORDER)
    print(f"\nUploaded {uploaded}, failed {failed}; {total_left} unuploaded remain in queue.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
