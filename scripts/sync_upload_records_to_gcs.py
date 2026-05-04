"""One-shot: push every local upload record to GCS.

Walks every ``<channel>/uploads/**/*.json`` and uploads each to
``gs://<bucket>/upload-records/<channel>/[<niche>/]<slug>.json`` —
the location ``control.dashboard_routes._enumerate_uploads`` reads.

Use after enabling GCS-backed dashboard reads, or any time the
laptop and the bucket drift. Idempotent: re-uploading the same
content is a no-op from the dashboard's POV.

Requires ADC: ``gcloud auth application-default login``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from control import storage  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="list only, don't push")
    ap.add_argument(
        "--channel",
        default=None,
        help="restrict to one channel (e.g. historyrecapped)",
    )
    args = ap.parse_args()

    pushed = 0
    skipped = 0
    failed = 0

    for chan_dir in sorted(REPO_ROOT.iterdir()):
        if not chan_dir.is_dir() or not (chan_dir / "config.yaml").exists():
            continue
        if args.channel and chan_dir.name != args.channel:
            continue
        uploads_dir = chan_dir / "uploads"
        if not uploads_dir.exists():
            continue
        for f in sorted(uploads_dir.rglob("*.json")):
            try:
                rec = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError) as e:
                print(f"[skip] {f.relative_to(REPO_ROOT)}: {e}")
                skipped += 1
                continue
            if not rec.get("video_id"):
                print(f"[skip] {f.relative_to(REPO_ROOT)}: no video_id")
                skipped += 1
                continue

            rel_key = storage.upload_record_rel_key(f, REPO_ROOT)
            uri = storage.upload_record_uri(rel_key)
            if args.dry_run:
                print(f"[dry] {f.relative_to(REPO_ROOT)} → {uri}")
                pushed += 1
                continue
            try:
                storage.upload_bytes(
                    json.dumps(rec, indent=2).encode("utf-8"),
                    uri,
                    content_type="application/json",
                )
                print(f"[push] {f.relative_to(REPO_ROOT)} → {uri}")
                pushed += 1
            except Exception as e:
                print(f"[fail] {f.relative_to(REPO_ROOT)}: {type(e).__name__}: {e}")
                failed += 1

    print(f"\nsummary: pushed={pushed} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
