"""Upload a historyrecapped Short. Pass --slug; mp4 is auto-located.

Usage:
    .venv/bin/python historyrecapped/scripts/upload.py \
        --slug pointe-du-hoc-1944 \
        --title "The most impossible mission of D-Day"
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import yaml

from pipeline import upload as up


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--mp4", default=None, help="override mp4 path; default historyrecapped/shorts/<slug>.mp4")
    ap.add_argument("--title", default=None)
    ap.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"])
    ap.add_argument("--skip-critic", action="store_true", default=True)
    args = ap.parse_args()

    channel_yaml = yaml.safe_load((ROOT / "historyrecapped/config.yaml").read_text())
    slug = args.slug
    mp4 = Path(args.mp4) if args.mp4 else (ROOT / f"historyrecapped/shorts/{slug}.mp4")
    script = json.loads((ROOT / f"historyrecapped/narrations/{slug}.json").read_text())
    raw = json.loads((ROOT / f"historyrecapped/raw/{slug}.json").read_text())

    assert mp4.exists(), f"missing mp4: {mp4}"
    print(f"[upload] {mp4.name} ({mp4.stat().st_size // 1024 // 1024} MB) → History Recapped ({args.privacy})")

    record = up.upload_short(
        project_root=ROOT,
        channel_yaml=channel_yaml,
        channel_dir="historyrecapped",
        slug=slug,
        mp4_path=mp4,
        script=script,
        raw=raw,
        title_override=args.title,
        privacy_override=args.privacy,
        skip_critic=args.skip_critic,
    )
    print()
    print("=" * 64)
    print("UPLOAD OK")
    print(f"  video_id : {record.get('video_id')}")
    print(f"  url      : https://youtube.com/shorts/{record.get('video_id')}")
    print(f"  title    : {record.get('title')}")
    print(f"  privacy  : {record.get('privacy')}")
    print("=" * 64)


if __name__ == "__main__":
    main()
