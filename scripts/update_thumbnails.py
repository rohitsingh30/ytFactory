"""Re-thumbnail already-uploaded videos.

Walks ``data/uploads/<channel_dir>/<slug>.json``, composes a fresh
auto-thumbnail for each (via ``pipeline.thumbnails``), and pushes it to
YouTube via ``youtube.thumbnails.set`` (no re-upload, just the thumb).

Use cases:
  - Backfill thumbs on shorts uploaded before the composer existed.
  - Roll a redesign of the thumbnail style across the catalog without
    re-rendering the underlying mp4s.

Examples:

    # Dry-run — list everything we'd re-thumb.
    .venv/bin/python scripts/update_thumbnails.py

    # Apply to every uploaded short.
    .venv/bin/python scripts/update_thumbnails.py --apply

    # Just one slug + custom headline.
    .venv/bin/python scripts/update_thumbnails.py \\
        --apply --slug aita-birth-pool --headline "I SAID NO"

The upload record JSON is patched with the new ``thumbnail_path`` after
each successful set so you can audit what was applied.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import upload as up      # noqa: E402
from pipeline import thumbnails as th  # noqa: E402


def find_records(*, slug_filter: str | None) -> list[dict]:
    """Return [{record_path, video_id, slug, channel_dir, account}] for every
    upload record we have on disk."""
    out: list[dict] = []
    base = PROJECT_ROOT / "data" / "uploads"
    if not base.exists():
        return out
    for chan_dir in sorted(base.iterdir()):
        if not chan_dir.is_dir():
            continue
        for f in sorted(chan_dir.glob("*.json")):
            try:
                rec = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            slug = rec.get("slug") or f.stem
            if slug_filter and slug_filter != slug:
                continue
            vid = rec.get("video_id")
            if not vid:
                continue
            out.append({
                "record_path": f,
                "video_id": vid,
                "slug": slug,
                "channel_dir": chan_dir.name,
                "account": rec.get("account") or "default",
                "url": rec.get("url") or "",
                "title": rec.get("title") or "",
                "current_thumbnail_path": rec.get("thumbnail_path"),
            })
    return out


def find_channel_yaml_for(channel_dir: str) -> Path | None:
    """Pick the channel YAML to drive thumbnail style from.

    1. niches.NICHE_CHANNEL reverse-lookup (preferred — same map the
       website uses).
    2. heuristic by directory name.
    """
    try:
        from pipeline import niches
        for _niche, (cdir, ypath) in niches.NICHE_CHANNEL.items():
            if cdir == channel_dir:
                return PROJECT_ROOT / ypath
    except Exception:
        pass
    # Heuristic fallback.
    candidates = sorted((PROJECT_ROOT / "channels").glob("*.yaml"))
    for c in candidates:
        if c.stem.replace("_", "") in channel_dir.replace("_", ""):
            return c
    return candidates[0] if candidates else None


def find_script_for(channel_dir: str, slug: str) -> dict:
    sp = PROJECT_ROOT / "data" / "intermediate" / channel_dir / "scripts" / f"{slug}.json"
    if sp.exists():
        try:
            return json.loads(sp.read_text())
        except json.JSONDecodeError:
            pass
    return {"slug": slug}


def compose_and_set_one(
    rec: dict,
    *,
    headline_override: str | None,
    style_override: str | None,
    scene_index_override: int | None,
    apply: bool,
) -> tuple[str, str]:
    """Compose + (optionally) set thumbnail. Returns (status, message)."""
    slug = rec["slug"]
    channel_dir = rec["channel_dir"]
    video_id = rec["video_id"]
    account = rec["account"]

    cache_dir = PROJECT_ROOT / "data" / "cache" / slug
    if not th.list_scene_frames(cache_dir):
        return ("skip", "no scene frames in data/cache/<slug>/")

    chan_yaml_path = find_channel_yaml_for(channel_dir)
    if chan_yaml_path is None:
        return ("error", "no channel YAML found")
    chan_yaml = yaml.safe_load(chan_yaml_path.read_text()) or {}
    script = find_script_for(channel_dir, slug)

    out_path = cache_dir / "auto_thumb.jpg"
    p = th.auto_thumbnail(
        slug=slug, cache_dir=cache_dir, script=script,
        channel_yaml=chan_yaml, channel_dir=channel_dir,
        out_path=out_path,
        headline_override=headline_override,
        scene_index_override=scene_index_override,
        style_override=style_override,
    )
    if p is None:
        return ("error", "auto_thumbnail returned None")

    if not apply:
        return ("dry-run", f"would set thumb {p.name} on {video_id}")

    try:
        up.set_thumbnail(video_id, p, account=account)
    except up.UploadError as e:
        return ("error", str(e))
    except Exception as e:
        return ("error", f"unexpected: {e}")

    # Patch the upload record so it reflects the new thumb.
    try:
        rec_path: Path = rec["record_path"]
        existing = json.loads(rec_path.read_text())
        existing["thumbnail_path"] = str(p)
        existing["thumbnail_set"] = True
        existing["thumbnail_set_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec_path.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        print(f"   [warn] couldn't patch record {rec_path.name}: {e}")

    return ("ok", f"set thumbnail on {video_id} ({p.stat().st_size // 1024} KB)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually push to YouTube. Default dry-runs the composition.")
    ap.add_argument("--slug", default=None,
                    help="Limit to a single slug.")
    ap.add_argument("--headline", default=None,
                    help="Override the auto-derived headline (also writes a new auto_thumb.jpg).")
    ap.add_argument("--style", default=None, choices=sorted(th._STYLES),
                    help="Force a thumbnail style (e.g. 'aita', 'tifu', 'oddities', 'tih').")
    ap.add_argument("--scene-index", type=int, default=None,
                    help="Use img_NN.png as the base instead of img_00.png.")
    ap.add_argument("--sleep", type=float, default=1.5,
                    help="Seconds between API calls (quota courtesy; default 1.5).")
    args = ap.parse_args()

    records = find_records(slug_filter=args.slug)
    if not records:
        print("(no upload records found under data/uploads/*)")
        return

    print(f"\nfound {len(records)} uploaded short(s)\n")
    print(f"{'#':>3}  {'STATUS':10s}  {'CH':24s}  {'VIDEO':12s}  TITLE")
    print("-" * 110)
    ok = err = skipped = 0
    for i, r in enumerate(records, 1):
        status, msg = compose_and_set_one(
            r,
            headline_override=args.headline,
            style_override=args.style,
            scene_index_override=args.scene_index,
            apply=args.apply,
        )
        marker = {
            "ok": "✓",
            "dry-run": "·",
            "skip": "—",
            "error": "✗",
        }.get(status, "?")
        print(
            f"{i:>3}  {marker} {status:8s}  {r['channel_dir'][:24]:24s}  "
            f"{r['video_id']:12s}  {(r['title'] or r['slug'])[:48]}"
        )
        print(f"      {msg}")
        if status == "ok":
            ok += 1
        elif status == "error":
            err += 1
        elif status == "skip":
            skipped += 1
        if args.apply and i < len(records) and args.sleep > 0:
            time.sleep(args.sleep)

    print()
    print("=" * 70)
    if args.apply:
        print(f"DONE  ·  {ok} thumbnails set  ·  {skipped} skipped  ·  {err} failed")
    else:
        print(f"(dry-run — pass --apply to push to YouTube)")


if __name__ == "__main__":
    main()
