"""YouTube-driven enumeration + stats fetcher for the research dashboard.

Source of truth is the live YouTube channel, not local mp4s. For each
authenticated upload account (one per ``<channel>/config.yaml``):

  - ``channels.list(mine=True)`` → channel meta + uploads playlist ID
    + subscriber/view/video count
  - paginate ``playlistItems.list(playlistId=<uploads>)`` → every
    video on that channel
  - batch ``videos.list(id=…)`` 50 at a time → per-video snippet,
    statistics, contentDetails, status

Cache shape:

    data/research/youtube/<account>.json
        {
          "fetched_at": "<iso>",
          "channel": { id, title, subscriber_count, view_count,
                       video_count, hidden_subscribers, uploads_playlist },
          "videos":  [ { video_id, title, description, published_at,
                         privacy, duration_s, thumbnail_url, url,
                         view_count, like_count, comment_count,
                         favorite_count }, ... ]
        }

CTR + retention require the youtubeAnalytics API and a separate
``yt-analytics.readonly`` scope — not wired here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESEARCH_DIR = PROJECT_ROOT / "data" / "research"
YOUTUBE_DIR = RESEARCH_DIR / "youtube"


# ---- channel discovery --------------------------------------------------


# Top-level dirs that are not channel dirs (and so must be skipped when
# enumerating <channel>/config.yaml).
_NON_CHANNEL_DIRS = {
    "pipeline", "web", "data", "docs", "tests", "scripts",
    "workers", "control", "node_modules", "venv", ".venv", ".git",
}


def iter_channel_configs() -> list[tuple[str, str]]:
    """Yield (account, channel_dir_name) for every <channel>/config.yaml.

    ``account`` is taken from the YAML's ``upload.account`` (the OAuth
    keying), falling back to the directory name. Channels without a
    ``config.yaml`` are skipped silently.
    """
    import yaml

    out: list[tuple[str, str]] = []
    for d in sorted(PROJECT_ROOT.iterdir()):
        if not d.is_dir() or d.name in _NON_CHANNEL_DIRS or d.name.startswith("."):
            continue
        cfg_path = d / "config.yaml"
        if not cfg_path.exists():
            continue
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            cfg = {}
        account = ((cfg.get("upload") or {}).get("account")) or d.name
        out.append((account, d.name))
    return out


# ---- API helpers --------------------------------------------------------


def _build_youtube(account: str):
    """Returns an authenticated youtube v3 client, or None on auth fail."""
    from googleapiclient.discovery import build

    from pipeline.upload import authenticate

    try:
        creds = authenticate(account=account, interactive=False)
    except Exception:
        return None
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _fetch_channel(youtube) -> dict | None:
    """``channels.list?mine=true`` — channel meta + uploads playlist."""
    from googleapiclient.errors import HttpError

    try:
        resp = youtube.channels().list(
            part="snippet,statistics,contentDetails", mine=True,
        ).execute()
    except HttpError:
        return None
    items = resp.get("items") or []
    if not items:
        return None
    item = items[0]
    snippet = item.get("snippet") or {}
    stats = item.get("statistics") or {}
    related = (item.get("contentDetails") or {}).get("relatedPlaylists") or {}
    return {
        "id": item.get("id"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "subscriber_count": (
            int(stats["subscriberCount"]) if "subscriberCount" in stats else None
        ),
        "view_count": int(stats["viewCount"]) if "viewCount" in stats else None,
        "video_count": int(stats["videoCount"]) if "videoCount" in stats else None,
        "hidden_subscribers": bool(stats.get("hiddenSubscriberCount")),
        "uploads_playlist": related.get("uploads"),
    }


def _fetch_uploads_playlist(youtube, playlist_id: str) -> list[str]:
    """Paginate playlistItems.list, return every video_id on the channel."""
    from googleapiclient.errors import HttpError

    if not playlist_id:
        return []
    ids: list[str] = []
    page_token: str | None = None
    while True:
        try:
            resp = youtube.playlistItems().list(
                part="contentDetails",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=page_token,
            ).execute()
        except HttpError:
            break
        for it in resp.get("items") or []:
            vid = (it.get("contentDetails") or {}).get("videoId")
            if vid:
                ids.append(vid)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _parse_iso8601_duration(s: str | None) -> int | None:
    """Convert ISO-8601 PT#H#M#S to seconds. Returns None on bad input."""
    if not s or not s.startswith("PT"):
        return None
    rest = s[2:]
    total = 0
    num = ""
    for ch in rest:
        if ch.isdigit():
            num += ch
            continue
        if not num:
            return None
        n = int(num)
        if ch == "H":
            total += n * 3600
        elif ch == "M":
            total += n * 60
        elif ch == "S":
            total += n
        else:
            return None
        num = ""
    return total if not num else None


def _fetch_videos_batch(youtube, video_ids: list[str]) -> dict[str, dict]:
    """Returns {video_id: full row}. Batches at 50 ids per call."""
    from googleapiclient.errors import HttpError

    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        try:
            resp = youtube.videos().list(
                part="snippet,statistics,contentDetails,status",
                id=",".join(chunk),
            ).execute()
        except HttpError:
            continue
        for item in resp.get("items") or []:
            snippet = item.get("snippet") or {}
            stats = item.get("statistics") or {}
            details = item.get("contentDetails") or {}
            status = item.get("status") or {}
            thumbs = snippet.get("thumbnails") or {}
            best = (
                thumbs.get("maxres") or thumbs.get("standard")
                or thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}
            )
            vid = item["id"]
            out[vid] = {
                "video_id": vid,
                "title": snippet.get("title"),
                "description": snippet.get("description"),
                "published_at": snippet.get("publishedAt"),
                "channel_id": snippet.get("channelId"),
                "channel_title": snippet.get("channelTitle"),
                "tags": snippet.get("tags") or [],
                "category_id": snippet.get("categoryId"),
                "thumbnail_url": best.get("url"),
                "duration_s": _parse_iso8601_duration(details.get("duration")),
                "privacy": status.get("privacyStatus"),
                "made_for_kids": status.get("madeForKids"),
                "url": f"https://youtu.be/{vid}",
                "view_count": int(stats["viewCount"]) if "viewCount" in stats else None,
                "like_count": int(stats["likeCount"]) if "likeCount" in stats else None,
                "comment_count": int(stats["commentCount"]) if "commentCount" in stats else None,
                "favorite_count": (
                    int(stats["favoriteCount"]) if "favoriteCount" in stats else None
                ),
            }
    return out


# ---- per-account fetch + cache -----------------------------------------


def fetch_account(account: str, *, quiet: bool = False) -> dict | None:
    """Fetch channel meta + every uploaded video for one account.

    Writes ``data/research/youtube/<account>.json`` and returns the
    payload. Returns None on auth/API failure (caller treats as
    "channel not refreshable yet").
    """
    youtube = _build_youtube(account)
    if youtube is None:
        if not quiet:
            print(f"[stats] {account}: auth failed — skipping")
        return None

    channel = _fetch_channel(youtube)
    if not channel:
        if not quiet:
            print(f"[stats] {account}: channels.list returned nothing — skipping")
        return None

    video_ids = _fetch_uploads_playlist(youtube, channel.get("uploads_playlist") or "")
    videos_by_id = _fetch_videos_batch(youtube, video_ids) if video_ids else {}

    # Preserve playlist order (most-recent first per YouTube convention).
    videos = [videos_by_id[v] for v in video_ids if v in videos_by_id]

    payload = {
        "account": account,
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "channel": channel,
        "videos": videos,
    }
    YOUTUBE_DIR.mkdir(parents=True, exist_ok=True)
    (YOUTUBE_DIR / f"{account}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2)
    )
    if not quiet:
        print(f"[stats] {account}: {len(videos)} videos · {channel.get('subscriber_count')} subs")
    return payload


def fetch_all(*, quiet: bool = False) -> dict[str, Any]:
    """Refresh every channel discoverable via <channel>/config.yaml.

    Returns a small summary dict for CLI/log output.
    """
    accounts = iter_channel_configs()
    if not accounts:
        if not quiet:
            print("[stats] no channels found — nothing to refresh")
        return {"channels": 0, "fetched": 0, "missing_auth": 0, "videos": 0}

    fetched = 0
    missing_auth = 0
    total_videos = 0
    # One fetch per distinct account (a few channel dirs may share an account).
    seen: set[str] = set()
    for account, _chan_dir in accounts:
        if account in seen:
            continue
        seen.add(account)
        result = fetch_account(account, quiet=quiet)
        if result is None:
            missing_auth += 1
            continue
        fetched += 1
        total_videos += len(result.get("videos") or [])

    return {
        "channels": len(seen),
        "fetched": fetched,
        "missing_auth": missing_auth,
        "videos": total_videos,
    }


# ---- offline readers (cheap; no network) -------------------------------


def load_account(account: str) -> dict | None:
    p = YOUTUBE_DIR / f"{account}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def load_all_cached() -> list[dict]:
    """Every cached account payload, in account-name order."""
    if not YOUTUBE_DIR.exists():
        return []
    out: list[dict] = []
    for p in sorted(YOUTUBE_DIR.glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


# ---- CLI ----------------------------------------------------------------


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="pipeline.youtube_stats")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--account",
        help="Refresh only this account (default: every <channel>/config.yaml).",
    )
    args = ap.parse_args()

    if args.account:
        result = fetch_account(args.account, quiet=args.quiet)
        summary = {
            "account": args.account,
            "fetched": 1 if result else 0,
            "videos": len(result.get("videos") or []) if result else 0,
        }
    else:
        summary = fetch_all(quiet=args.quiet)

    if args.quiet:
        print(json.dumps(summary))


if __name__ == "__main__":
    main()
