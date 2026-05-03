"""YouTube public-stats fetcher for the research dashboard.

Pulls viewCount / likeCount / commentCount / favoriteCount per uploaded
video using the YouTube Data API v3 ``videos.list?part=statistics``.
Works under the ``youtube.readonly`` scope already granted to every
upload account (see pipeline/upload.py SCOPES) — no re-auth required.

Skipped here on purpose:

  - **CTR (impressions click-through rate)** and **average view duration
    / retention** require the YouTube Analytics API and a separate
    ``https://www.googleapis.com/auth/yt-analytics.readonly`` scope.
    Adding that forces a re-auth across both production Google accounts.
    If you want it, add the scope to upload.py SCOPES, run
    ``python upload.py auth --account mystoriesanimated`` (and ditto for
    sportstoriesanimated), then call into ``youtubeAnalytics`` v2's
    ``reports.query`` with metrics='estimatedMinutesWatched,averageViewPercentage,impressionClickThroughRate'.

The fetcher writes one JSON per slug to ``data/research/analytics/<slug>.json``
so the index builder can join them in. Re-running is idempotent and
quota-friendly: the API allows up to 50 video IDs per call, and we batch
per-account.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYTICS_DIR = PROJECT_ROOT / "data" / "research" / "analytics"


@dataclass
class VideoStats:
    slug: str
    video_id: str
    account: str
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    favorite_count: int | None
    fetched_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "video_id": self.video_id,
            "account": self.account,
            "view_count": self.view_count,
            "like_count": self.like_count,
            "comment_count": self.comment_count,
            "favorite_count": self.favorite_count,
            "fetched_at": self.fetched_at,
        }


def _enumerate_uploads() -> list[tuple[str, str, str, Path]]:
    """Yield (account, slug, video_id, upload_record_path) for every upload.

    Post-reorg layout: every YouTube channel is a top-level repo folder
    (`historyrecapped/`, `mystoriesanimated/`, etc.) with `config.yaml` and
    its own `uploads/` subdir. Niches inside a channel (e.g.
    `mystoriesanimated/reddit_amitheasshole/`) ship their upload records
    under `uploads/<niche>/<slug>.json`. ``account`` is the channel slug
    (NOT the niche dir) since the OAuth token is keyed by channel.
    """
    out: list[tuple[str, str, str, Path]] = []
    for chan_dir in sorted(PROJECT_ROOT.iterdir()):
        if not chan_dir.is_dir() or not (chan_dir / "config.yaml").exists():
            continue
        uploads_dir = chan_dir / "uploads"
        if not uploads_dir.exists():
            continue
        # Recursive: handles flat `uploads/<slug>.json` AND nested
        # `uploads/<niche>/<slug>.json` (mystoriesanimated variants).
        for f in sorted(uploads_dir.rglob("*.json")):
            try:
                rec = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            vid = rec.get("video_id")
            slug = rec.get("slug") or f.stem
            if not vid:
                continue
            out.append((chan_dir.name, slug, vid, f))
    return out


def _fetch_for_account(account: str, video_ids: list[str]) -> dict[str, dict]:
    """Returns {video_id: {viewCount, likeCount, commentCount, favoriteCount}}.

    Empty dict if auth fails — the caller treats absence as "no stats yet"
    rather than an error, since this is best-effort enrichment.
    """
    if not video_ids:
        return {}

    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    from pipeline.upload import authenticate

    try:
        creds = authenticate(account=account, interactive=False)
    except Exception:
        return {}

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    out: dict[str, dict] = {}
    # The API caps at 50 ids per request.
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        try:
            resp = youtube.videos().list(
                part="statistics", id=",".join(chunk)
            ).execute()
        except HttpError:
            continue
        for item in resp.get("items") or []:
            out[item["id"]] = item.get("statistics") or {}
    return out


def _fetch_channel_for_account(account: str) -> dict | None:
    """Returns {subscriberCount, viewCount, videoCount, channel_id, fetched_at}
    for the OAuth-bound channel, or None if auth/API fails.

    Uses ``channels.list?part=statistics&mine=true`` — 1 quota unit per
    call, hidden subscriber counts come back as 0.
    """
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    from pipeline.upload import authenticate

    try:
        creds = authenticate(account=account, interactive=False)
    except Exception:
        return None

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    try:
        resp = youtube.channels().list(part="statistics,snippet", mine=True).execute()
    except HttpError:
        return None
    items = resp.get("items") or []
    if not items:
        return None
    item = items[0]
    stats = item.get("statistics") or {}
    snippet = item.get("snippet") or {}
    return {
        "channel_id": item.get("id"),
        "title": snippet.get("title"),
        "subscriber_count": (
            int(stats["subscriberCount"]) if "subscriberCount" in stats else None
        ),
        "view_count": int(stats["viewCount"]) if "viewCount" in stats else None,
        "video_count": int(stats["videoCount"]) if "videoCount" in stats else None,
        "hidden_subscribers": bool(stats.get("hiddenSubscriberCount")),
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def fetch_all(*, quiet: bool = False) -> dict[str, int]:
    """Refresh analytics/<slug>.json for every uploaded video.

    Returns a small summary dict (counts) for CLI/log output.
    """
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    uploads = _enumerate_uploads()
    if not uploads:
        if not quiet:
            print("[stats] no uploads to refresh")
        return {"uploads": 0, "fetched": 0, "missing_auth": 0}

    # Group video_ids by account so we can batch and re-use one auth.
    by_account: dict[str, list[tuple[str, str]]] = {}
    for account, slug, vid, _ in uploads:
        by_account.setdefault(account, []).append((slug, vid))

    fetched = 0
    missing_auth = 0
    now = datetime.now(tz=timezone.utc).isoformat()

    for account, items in sorted(by_account.items()):
        ids = [vid for _, vid in items]
        stats_by_id = _fetch_for_account(account, ids)
        if not stats_by_id and ids:
            missing_auth += len(ids)
            if not quiet:
                print(f"[stats] {account}: skipped ({len(ids)} videos) — no auth or API error")
            continue

        # Subscriber + total-channel stats, written alongside per-video files.
        channel_stats = _fetch_channel_for_account(account)
        if channel_stats:
            (ANALYTICS_DIR / f"_channel_{account}.json").write_text(
                json.dumps(channel_stats, ensure_ascii=False, indent=2)
            )

        for slug, vid in items:
            raw = stats_by_id.get(vid) or {}
            row = VideoStats(
                slug=slug,
                video_id=vid,
                account=account,
                view_count=int(raw["viewCount"]) if "viewCount" in raw else None,
                like_count=int(raw["likeCount"]) if "likeCount" in raw else None,
                comment_count=int(raw["commentCount"]) if "commentCount" in raw else None,
                favorite_count=int(raw["favoriteCount"]) if "favoriteCount" in raw else None,
                fetched_at=now,
            )
            (ANALYTICS_DIR / f"{slug}.json").write_text(
                json.dumps(row.to_json(), ensure_ascii=False, indent=2)
            )
            fetched += 1
        if not quiet:
            print(f"[stats] {account}: refreshed {len(items)} videos")

    return {"uploads": len(uploads), "fetched": fetched, "missing_auth": missing_auth}


def load_for_slug(slug: str) -> dict | None:
    p = ANALYTICS_DIR / f"{slug}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def load_channel_for_account(account: str) -> dict | None:
    p = ANALYTICS_DIR / f"_channel_{account}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# ---- CLI ----------------------------------------------------------------


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="pipeline.youtube_stats")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    summary = fetch_all(quiet=args.quiet)
    if args.quiet:
        print(json.dumps(summary))


if __name__ == "__main__":
    main()
