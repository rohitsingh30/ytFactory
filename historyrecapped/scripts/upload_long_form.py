"""Upload a long-form sleep video to History Recapped (private first).

Schema:
    historyrecapped/shorts/<slug>.mp4              — rendered 1080p video
    historyrecapped/thumbnails/<slug>.jpg          — custom thumbnail
    historyrecapped/narrations/<slug>.json         — script (uses .hook)

Usage:
    .venv/bin/python historyrecapped/scripts/upload_long_form.py \
        --slug pacific-war-1941-1942-sleep \
        --title "The Pacific War: Pearl Harbor to Midway · 1941–1942 · 45-Minute Sleep Documentary" \
        --privacy private    # always private first per ContentID dispute playbook
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import yaml

from pipeline import upload as up


def build_description(slug: str, hook: str, source_url: str | None) -> str:
    parts = [
        hook,
        "",
        "A long-form history documentary, narrated calmly, designed to be",
        "listened to as you drift off. All visuals are HD-restored archival",
        "footage; the narration is original.",
        "",
    ]
    if source_url:
        parts += [f"📖 Visual source: {source_url}", ""]
    parts += [
        "━━━━━━━━━━━━━━━━━━━━",
        "🌙 If you enjoyed this, a like and subscribe genuinely helps the channel.",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "#sleephistory #history #ww2 #pacificwar #pearlharbor #midway #sleep #relaxing #documentary #historyrecapped",
    ]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--privacy", default="private",
                    choices=["public", "unlisted", "private"],
                    help="ALWAYS use private/unlisted first to watch for ContentID claims")
    ap.add_argument("--source-url", default="https://www.youtube.com/watch?v=RxcnJNnOiaw",
                    help="visual source YouTube URL for description attribution")
    args = ap.parse_args()

    channel_yaml = yaml.safe_load((ROOT / "historyrecapped/config.yaml").read_text())
    slug = args.slug
    mp4 = ROOT / f"historyrecapped/shorts/{slug}.mp4"
    thumb = ROOT / f"historyrecapped/thumbnails/{slug}.jpg"
    narration = json.loads((ROOT / f"historyrecapped/narrations/{slug}.json").read_text())
    hook = narration.get("hook") or args.title

    if not mp4.exists():
        raise SystemExit(f"missing mp4: {mp4}")
    if not thumb.exists():
        raise SystemExit(f"missing thumbnail: {thumb} — run build_long_form_thumbnail.py first")

    description = build_description(slug, hook, args.source_url)
    tags = [
        "sleep history", "history documentary", "sleep documentary",
        "relaxing history", "calm narration", "world war 2 sleep",
        "pacific war", "pearl harbor", "midway",
        "history to fall asleep to", "long form history",
        "archival footage", "historyrecapped",
    ]

    size_mb = mp4.stat().st_size // 1024 // 1024
    print(f"[upload-long] {mp4.name} ({size_mb} MB) → History Recapped ({args.privacy})")
    print(f"[upload-long] title    : {args.title}")
    print(f"[upload-long] thumbnail: {thumb.name}")
    print(f"[upload-long] privacy  : {args.privacy} (ContentID watch period)")

    record = up.youtube_upload(
        mp4_path=mp4,
        title=args.title,
        description=description,
        tags=tags,
        category_id="27",   # Education
        privacy=args.privacy,
        made_for_kids=False,
        account="historyrecapped",
        thumbnail_path=thumb,
    )

    print()
    print("=" * 64)
    print("UPLOAD OK")
    print(f"  video_id : {record.get('video_id')}")
    print(f"  url      : https://youtu.be/{record.get('video_id')}")
    print(f"  title    : {args.title}")
    print(f"  privacy  : {args.privacy}")
    print(f"  category : 27 (Education)")
    print("=" * 64)
    print()
    print("NEXT STEPS:")
    print("  1. Wait 24h for ContentID scan")
    print("  2. If a claim hits → dispute via Studio → Content → DISPUTE")
    print("     Reason: 'Public domain content'")
    print("     Explanation: see historyrecapped/learnings/long_form_sources.md §3")
    print("  3. If clean after 24h → flip privacy to public via Studio")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
