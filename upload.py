"""Stage 8 entry point — upload a rendered Short to YouTube.

Two subcommands:

    upload.py auth   --account <name>
        Run the OAuth browser dance once for an account name. The
        refresh token is cached at
        ``~/.config/ytfactory/youtube_token_<account>.json`` so all
        subsequent uploads for that account are non-interactive.

    upload.py run    --slug <slug> --channel <channel.yaml> [opts]
        Find the rendered mp4 + script + raw, build snippet/status
        from the channel YAML's ``upload:`` block, and upload.

Examples:

    # one-time auth for the mystoriesanimated google account
    .venv/bin/python upload.py auth --account mystoriesanimated

    # upload aita02 to the mystoriesanimated channel as private
    .venv/bin/python upload.py run \\
        --channel channels/mystoriesanimated.yaml \\
        --slug aita02 \\
        --privacy private

    # schedule for tomorrow 13:00 UTC
    .venv/bin/python upload.py run \\
        --channel channels/mystoriesanimated.yaml \\
        --slug aita02 \\
        --publish-at 2026-05-02T13:00:00Z
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from pipeline import upload as up


PROJECT_ROOT = Path(__file__).resolve().parent


def _find_intermediate(channel_yaml_path: Path, slug: str) -> tuple[str, dict, dict | None, Path]:
    """Locate channel_dir + load script.json/raw.json/mp4 for a slug.

    Returns (channel_dir, script_dict, raw_dict_or_None, mp4_path).
    Raises SystemExit with a clear message on missing files.
    """
    chan_yaml = yaml.safe_load(channel_yaml_path.read_text()) or {}
    # The channel YAML doesn't always declare its on-disk channel_dir, so
    # we discover it by scanning data/intermediate/* for a scripts/<slug>.json.
    base = PROJECT_ROOT / "data" / "intermediate"
    found: tuple[str, Path] | None = None
    if base.exists():
        for chan_dir in base.iterdir():
            sp = chan_dir / "scripts" / f"{slug}.json"
            if sp.exists():
                found = (chan_dir.name, sp)
                break
    if not found:
        sys.exit(
            f"error: no script found for slug={slug!r} under data/intermediate/*/scripts/. "
            f"Run /make-script for this slug first."
        )
    channel_dir, script_path = found
    script = json.loads(script_path.read_text())

    raw_path = script_path.parent.parent / "raw" / f"{slug}.json"
    raw = None
    if raw_path.exists():
        try:
            raw = json.loads(raw_path.read_text())
        except json.JSONDecodeError:
            raw = None

    mp4 = PROJECT_ROOT / "data" / "shorts" / f"{slug}.mp4"
    if not mp4.exists():
        sys.exit(
            f"error: rendered mp4 not found at {mp4}. "
            f"Run make_shorts.py first."
        )
    return channel_dir, script, raw, mp4


def cmd_auth(args: argparse.Namespace) -> int:
    creds = up.authenticate(args.account, interactive=True)
    print(f"\n✓ authenticated for account={args.account!r}")
    print(f"  token cached at: {up._token_path(args.account)}")
    print(f"  scopes: {creds.scopes}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    channel_yaml_path = Path(args.channel)
    if not channel_yaml_path.exists():
        sys.exit(f"error: channel YAML not found: {channel_yaml_path}")

    chan_yaml = yaml.safe_load(channel_yaml_path.read_text()) or {}
    if "upload" not in chan_yaml:
        sys.exit(
            f"error: {channel_yaml_path} has no `upload:` block. Add one — "
            f"see channels/mystoriesanimated.yaml for the schema."
        )

    channel_dir, script, raw, mp4 = _find_intermediate(channel_yaml_path, args.slug)

    tags_override: list[str] | None = None
    if args.tags is not None:
        tags_override = [t.strip() for t in args.tags.split(",") if t.strip()]

    thumbnail_path: Path | None = None
    if args.thumbnail:
        thumbnail_path = Path(args.thumbnail).resolve()
        if not thumbnail_path.exists():
            sys.exit(f"error: thumbnail not found: {thumbnail_path}")

    record = up.upload_short(
        project_root=PROJECT_ROOT,
        channel_yaml=chan_yaml,
        channel_dir=channel_dir,
        slug=args.slug,
        mp4_path=mp4,
        script=script,
        raw=raw,
        title_override=args.title,
        description_override=args.description,
        privacy_override=args.privacy,
        publish_at=args.publish_at,
        tags_override=tags_override,
        thumbnail_path=thumbnail_path,
        force=args.force,
        skip_critic=args.skip_critic,
        force_critic=args.force_critic,
        min_score_override=args.min_score,
    )
    print(json.dumps(record, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """List uploads for a channel (reads data/uploads/<channel_dir>/*.json)."""
    base = PROJECT_ROOT / "data" / "uploads"
    targets: list[Path] = []
    if args.channel_dir:
        d = base / args.channel_dir
        if d.exists():
            targets = sorted(d.glob("*.json"))
    else:
        if base.exists():
            for sub in sorted(base.iterdir()):
                if sub.is_dir():
                    targets.extend(sorted(sub.glob("*.json")))
    if not targets:
        print("(no upload records)")
        return 0
    for t in targets:
        try:
            rec = json.loads(t.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        print(
            f"  {rec.get('slug',t.stem):40s}  "
            f"{rec.get('privacy','?'):8s}  "
            f"{rec.get('video_id','?'):12s}  "
            f"{rec.get('url','')}"
        )
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="upload.py", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_auth = sub.add_parser("auth", help="Run OAuth browser flow once for an account")
    p_auth.add_argument("--account", default="default",
                        help="Account name (token cached per-account). "
                             "Use one name per YouTube channel.")
    p_auth.set_defaults(func=cmd_auth)

    p_run = sub.add_parser("run", help="Upload a rendered short")
    p_run.add_argument("--channel", required=True, help="Path to channels/<name>.yaml")
    p_run.add_argument("--slug", required=True, help="Short slug (matches data/shorts/<slug>.mp4)")
    p_run.add_argument("--privacy", choices=["private", "unlisted", "public"], default=None,
                       help="Override channel YAML's upload.privacy")
    p_run.add_argument("--publish-at", default=None,
                       help="Schedule publish at RFC3339 timestamp, e.g. 2026-05-02T13:00:00Z. "
                            "Forces privacy=private until that time.")
    p_run.add_argument("--title", default=None, help="Override the auto-derived title")
    p_run.add_argument("--description", default=None, help="Override the templated description")
    p_run.add_argument("--tags", default=None,
                       help="Override channel YAML's upload.tags (comma-separated)")
    p_run.add_argument("--thumbnail", default=None,
                       help="Path to a custom thumbnail (jpg/png, ≤2MB). "
                            "Set after upload via youtube.thumbnails.set.")
    p_run.add_argument("--force", action="store_true",
                       help="Re-upload even if data/uploads/<channel>/<slug>.json exists.")
    p_run.add_argument("--skip-critic", action="store_true",
                       help="Skip the pre-upload critic gate (ship it anyway).")
    p_run.add_argument("--force-critic", action="store_true",
                       help="Re-run the critic even if a cached score.json exists.")
    p_run.add_argument("--min-score", type=int, default=None,
                       help="Override channel YAML's upload.min_score (default 6).")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="List uploaded shorts")
    p_status.add_argument("--channel-dir", default=None,
                          help="Limit to one data/intermediate/<dir> (e.g. reddit_amitheasshole)")
    p_status.set_defaults(func=cmd_status)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
