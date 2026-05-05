"""Upload a Cosmos Decoded Short or long-form video to YouTube.

Usage:
    .venv/bin/python cosmosdecoded/scripts/upload.py --slug eddington-1919-eclipse-short
    .venv/bin/python cosmosdecoded/scripts/upload.py --slug eddington-1919-eclipse --long-form

Mirrors historyrecapped/scripts/upload.py. The long-form path looks for the
mp4 under cosmosdecoded/long_form/<slug>.mp4 instead of /shorts/<slug>.mp4."""
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
    ap.add_argument("--mp4", default=None, help="override mp4 path")
    ap.add_argument("--title", default=None)
    ap.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"])
    ap.add_argument("--long-form", action="store_true",
                    help="upload from cosmosdecoded/long_form/<slug>.mp4 (default: cosmosdecoded/shorts/<slug>.mp4)")
    ap.add_argument("--skip-critic", action="store_true", default=True)
    args = ap.parse_args()

    channel_yaml = yaml.safe_load((ROOT / "cosmosdecoded/config.yaml").read_text())
    slug = args.slug
    if args.mp4:
        mp4 = Path(args.mp4)
    elif args.long_form:
        mp4 = ROOT / f"cosmosdecoded/long_form/{slug}.mp4"
    else:
        mp4 = ROOT / f"cosmosdecoded/shorts/{slug}.mp4"

    script = json.loads((ROOT / f"cosmosdecoded/narrations/{slug}.json").read_text())

    # raw/<slug>.json may not exist for the Short (which is a derivative of
    # the long-form). Fall back to the long_form_parent's raw if specified.
    raw_path = ROOT / f"cosmosdecoded/raw/{slug}.json"
    if not raw_path.exists():
        parent = script.get("long_form_parent")
        if parent:
            raw_path = ROOT / f"cosmosdecoded/raw/{parent}.json"
    raw = json.loads(raw_path.read_text()) if raw_path.exists() else None

    # Make {raw.url} resolvable in description_template. Cosmos Decoded's
    # config sets `📖 Source: {raw.url}` — point it at the strongest single
    # source the dossier names (the proving paper) so the upload description
    # always carries one citable link.
    if raw and "url" not in raw:
        prov = (raw.get("experiment") or {}).get("proving_paper") or {}
        url = prov.get("doi_or_url") or (raw.get("prediction") or {}).get("predicting_paper", {}).get("doi_or_url")
        if url:
            raw = {**raw, "url": url}

    assert mp4.exists(), f"missing mp4: {mp4}"
    print(f"[upload] {mp4.name} ({mp4.stat().st_size // 1024 // 1024} MB) → Cosmos Decoded ({args.privacy})")

    record = up.upload_short(
        project_root=ROOT,
        channel_yaml=channel_yaml,
        channel_dir="cosmosdecoded",
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
    print(f"  url      : https://youtube.com/shorts/{record.get('video_id')}" if not args.long_form
          else f"  url      : https://youtube.com/watch?v={record.get('video_id')}")
    print(f"  title    : {record.get('title')}")
    print(f"  privacy  : {record.get('privacy')}")
    print("=" * 64)


if __name__ == "__main__":
    main()
