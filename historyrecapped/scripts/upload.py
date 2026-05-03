"""Upload the v2 100%-footage Battle of Britain Short to History Recapped."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml

from pipeline import upload as up

channel_yaml = yaml.safe_load((ROOT / "historyrecapped/config.yaml").read_text())
slug = "battle-of-britain-few"
mp4 = ROOT / "historyrecapped/shorts/battle-of-britain-few-100footage-v2.mp4"
script = json.loads((ROOT / f"historyrecapped/narrations/{slug}.json").read_text())
raw = json.loads((ROOT / f"historyrecapped/raw/{slug}.json").read_text())

assert mp4.exists(), f"missing mp4: {mp4}"
print(f"[upload] {mp4.name} ({mp4.stat().st_size // 1024 // 1024} MB) → History Recapped (public)")

record = up.upload_short(
    project_root=ROOT,
    channel_yaml=channel_yaml,
    channel_dir="historyrecapped",
    slug=slug,
    mp4_path=mp4,
    script=script,
    raw=raw,
    title_override="The 2,937 men who saved Britain",
    privacy_override="public",
    skip_critic=True,   # manual 100%-footage build — no critic.json on disk
)
print()
print("=" * 64)
print("UPLOAD OK")
print(f"  video_id : {record.get('video_id')}")
print(f"  url      : https://youtube.com/shorts/{record.get('video_id')}")
print(f"  title    : {record.get('title')}")
print(f"  privacy  : {record.get('privacy')}")
print("=" * 64)
