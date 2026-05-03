"""Production-pipeline batch upload — push every rendered Short that
hasn't been uploaded yet to YouTube.

Walks ``data/intermediate/*/scripts/*.json``, finds the matching
``<slug>.mp4`` and the (optional) ``raw/<slug>.json``,
skips anything that already has a record under ``data/uploads/``, and
shows you the candidate list. Pass ``--apply`` to actually upload.

The point of this script is not just to ship in batch — it's also the
"test of the production pipeline" in that the same code paths
``upload.py run`` uses are exercised here for every slug, surfacing
any per-slug issues (missing raw.json, malformed script.title_options,
etc.) before they hit the channel.

Examples:

    # 1. Dry-run — list candidates, don't upload.
    .venv/bin/python scripts/bulk_upload.py \\
        --channel mystoriesanimated/config.yaml

    # 2. Same, but only AITA-class slugs.
    .venv/bin/python scripts/bulk_upload.py \\
        --channel mystoriesanimated/config.yaml \\
        --filter aita

    # 3. Apply — upload everything as PUBLIC.
    .venv/bin/python scripts/bulk_upload.py \\
        --channel mystoriesanimated/config.yaml \\
        --apply --privacy public
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

# Make ``import pipeline.upload`` resolve when run from anywhere.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import upload as up  # noqa: E402


def _read_cached_score(slug: str) -> int | None:
    """Best-effort read of an existing critique score.json (no critic run)."""
    p = PROJECT_ROOT / "data" / "critiques" / slug / f"{slug}.score.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        s = d.get("score")
        return int(s) if s is not None else None
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def discover_candidates(*, name_filter: str | None) -> list[dict]:
    """List every (script, mp4) pair that hasn't been uploaded yet.

    Returns a list of dicts with: slug, channel_dir, mp4_path, script,
    raw, has_upload_record, title (auto-derived), cached_score.
    """
    out: list[dict] = []
    inter = PROJECT_ROOT / "data" / "intermediate"
    shorts = PROJECT_ROOT / "data" / "shorts"
    if not inter.exists():
        return out

    for chan_dir in sorted(inter.iterdir()):
        scripts = chan_dir / "scripts"
        if not scripts.exists():
            continue
        for sp in sorted(scripts.glob("*.json")):
            slug = sp.stem
            if name_filter and name_filter.lower() not in slug.lower():
                continue
            mp4 = shorts / f"{slug}.mp4"
            if not mp4.exists():
                continue
            try:
                script = json.loads(sp.read_text())
            except json.JSONDecodeError:
                continue
            raw_path = chan_dir / "raw" / f"{slug}.json"
            raw = None
            if raw_path.exists():
                try:
                    raw = json.loads(raw_path.read_text())
                except json.JSONDecodeError:
                    raw = None
            existing = up.existing_upload(PROJECT_ROOT, chan_dir.name, slug)
            title = (script.get("title_options") or [None])[0] or script.get("hook") or slug
            out.append({
                "slug": slug,
                "channel_dir": chan_dir.name,
                "mp4_path": mp4,
                "mp4_size_mb": round(mp4.stat().st_size / 1_048_576, 2),
                "script": script,
                "raw": raw,
                "raw_present": raw is not None,
                "uploaded_already": existing,
                "title": title,
                "cached_score": _read_cached_score(slug),
            })
    return out


def print_table(rows: list[dict], min_score: int) -> None:
    if not rows:
        print("(no candidates found)")
        return
    print(f"{'#':>3}  {'STATUS':10s}  {'CH':24s}  {'SIZE':>6s}  {'RAW':3s}  {'SCORE':>5s}  TITLE / SLUG")
    print("-" * 120)
    for i, r in enumerate(rows, 1):
        st = "UPLOADED" if r["uploaded_already"] else "pending"
        raw = "yes" if r["raw_present"] else "no"
        cs = r["cached_score"]
        if cs is None:
            score_cell = "?"
        elif cs < min_score:
            score_cell = f"✗{cs}"
        else:
            score_cell = f"✓{cs}"
        title = (r["title"] or "")[:60]
        print(
            f"{i:>3}  {st:10s}  {r['channel_dir'][:24]:24s}  "
            f"{r['mp4_size_mb']:>6.2f}  {raw:3s}  {score_cell:>5s}  {title}"
        )
        print(f"      slug: {r['slug']}")
    pending = sum(1 for r in rows if not r["uploaded_already"])
    blocked = sum(1 for r in rows if not r["uploaded_already"]
                  and r["cached_score"] is not None and r["cached_score"] < min_score)
    print(
        f"\n{len(rows)} total / {pending} pending / "
        f"{blocked} would-be-blocked by critic gate (min_score={min_score})"
    )


def apply_uploads(
    rows: list[dict],
    *,
    channel_yaml_path: Path,
    privacy: str,
    sleep_s: float,
    skip_critic: bool,
    force_critic: bool,
    min_score_override: int | None,
) -> None:
    chan_yaml = yaml.safe_load(channel_yaml_path.read_text()) or {}
    if "upload" not in chan_yaml:
        sys.exit(f"error: {channel_yaml_path} has no upload: block")

    pending = [r for r in rows if not r["uploaded_already"]]
    if not pending:
        print("nothing to upload — all candidates already have records.")
        return

    failures: list[tuple[str, str]] = []
    blocked: list[tuple[str, str]] = []
    successes: list[dict] = []
    for i, r in enumerate(pending, 1):
        print(f"\n[{i}/{len(pending)}] {r['slug']}  ({r['channel_dir']})")
        try:
            rec = up.upload_short(
                project_root=PROJECT_ROOT,
                channel_yaml=chan_yaml,
                channel_dir=r["channel_dir"],
                slug=r["slug"],
                mp4_path=r["mp4_path"],
                script=r["script"],
                raw=r["raw"],
                privacy_override=privacy,
                skip_critic=skip_critic,
                force_critic=force_critic,
                min_score_override=min_score_override,
            )
            successes.append(rec)
        except up.UploadError as e:
            # Critic gate failures land here too — keep them separate
            # from network/api errors so the summary tells the operator
            # which need a re-render vs which need a retry.
            msg = str(e)
            if "critic score" in msg:
                print(f"   ⏸ BLOCKED BY CRITIC: {msg.splitlines()[0]}")
                blocked.append((r["slug"], msg))
            else:
                print(f"   ✗ FAILED: {msg}")
                failures.append((r["slug"], msg))
        except Exception as e:
            print(f"   ✗ FAILED: {e}")
            failures.append((r["slug"], str(e)))
        if i < len(pending) and sleep_s > 0:
            print(f"   sleeping {sleep_s}s before next upload (quota courtesy)…")
            time.sleep(sleep_s)

    print("\n" + "=" * 70)
    print(
        f"DONE  ·  {len(successes)} uploaded  ·  "
        f"{len(blocked)} blocked by critic  ·  {len(failures)} failed"
    )
    if successes:
        print("\nUploaded:")
        for s in successes:
            print(f"   {s['slug']:50s}  {s['url']}")
    if blocked:
        print("\nBlocked by critic gate (re-render to fix, or pass --skip-critic):")
        for slug, err in blocked:
            print(f"   {slug:50s}  {err.splitlines()[0]}")
    if failures:
        print("\nFailures:")
        for slug, err in failures:
            print(f"   {slug:50s}  {err}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", required=True,
                    help="Path to channels/<name>.yaml — provides the upload: block")
    ap.add_argument("--filter", default=None,
                    help="Substring filter on slug (e.g. 'aita', 'tifu')")
    ap.add_argument("--apply", action="store_true",
                    help="Actually upload. Without this, prints the candidate table only.")
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"], default=None,
                    help="Override channel YAML's upload.privacy. Recommended for live runs.")
    ap.add_argument("--sleep", type=float, default=2.0,
                    help="Seconds to sleep between uploads (default 2 — courtesy gap "
                         "for YouTube's quota and processing pipeline).")
    ap.add_argument("--skip-critic", action="store_true",
                    help="Skip the pre-upload critic gate (ship every candidate).")
    ap.add_argument("--force-critic", action="store_true",
                    help="Re-run the critic even if a cached score.json exists.")
    ap.add_argument("--min-score", type=int, default=None,
                    help="Override channel YAML's upload.min_score (default 6).")
    args = ap.parse_args()

    channel_yaml_path = (PROJECT_ROOT / args.channel).resolve()
    if not channel_yaml_path.exists():
        sys.exit(f"error: channel YAML not found: {channel_yaml_path}")

    chan_yaml = yaml.safe_load(channel_yaml_path.read_text()) or {}
    cfg_min = (chan_yaml.get("upload") or {}).get("min_score", 6)
    effective_min_score = args.min_score if args.min_score is not None else cfg_min

    rows = discover_candidates(name_filter=args.filter)
    print_table(rows, min_score=effective_min_score)

    if not args.apply:
        print("\n(dry-run — pass --apply to actually upload)")
        return

    pending = [r for r in rows if not r["uploaded_already"]]
    privacy = args.privacy or "private"
    gate_msg = "SKIPPED" if args.skip_critic else f"min_score={effective_min_score}"
    print(f"\n>>> APPLY MODE: about to upload {len(pending)} videos to "
          f"{channel_yaml_path.name} as PRIVACY={privacy.upper()} "
          f"(critic gate: {gate_msg}) <<<")
    apply_uploads(rows, channel_yaml_path=channel_yaml_path,
                  privacy=privacy, sleep_s=args.sleep,
                  skip_critic=args.skip_critic,
                  force_critic=args.force_critic,
                  min_score_override=args.min_score)


if __name__ == "__main__":
    main()
