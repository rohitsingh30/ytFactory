"""Live YouTube analytics dashboard for every uploaded Short.

Cloud-friendly: uses a YouTube Data API v3 key (set ``YOUTUBE_API_KEY``
env var) to fetch public stats — no per-channel OAuth refresh tokens
needed in the deployed control plane.

The list of videos to fetch stats for comes from
``gs://<bucket>/upload-records/<channel>/[<niche>/]<slug>.json``. The
laptop's ``pipeline.upload.write_upload_record`` mirrors every record
to that bucket on every successful upload, so the cloud dashboard
stays fresh without redeploying the image. If GCS is unavailable
(local dev with no creds) or empty, falls back to walking
``<channel>/uploads/**/*.json`` on disk.

Routes:
    GET  /api/dashboard/videos              — cached payload (no API hit)
    GET  /api/dashboard/videos?refresh=true — refresh from YouTube first
    GET  /dashboard                          — serves dashboard.html

The cache is in-memory per process. Cloud Run scales to zero so a cold
start re-fetches; a warm container reuses cached stats.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "web" / "static"

# ---- in-memory stats cache --------------------------------------------------
# {video_id: {"viewCount": int, "likeCount": int, "commentCount": int,
#             "fetched_at": float (epoch seconds)}}
_STATS_CACHE: dict[str, dict[str, Any]] = {}
_STATS_TTL_S = 600  # 10 min — refresh button bypasses this

# {channel_id: {"subscriber_count": int, "view_count": int,
#               "video_count": int, "title": str, "fetched_at": float}}
# Subscriber counts barely move between refreshes, so we keep this
# parallel cache and refresh it together with video stats.
_CHANNEL_STATS_CACHE: dict[str, dict[str, Any]] = {}
# Maps OAuth-account slug → channelId, populated lazily from videos.list
# snippet.channelId. Without OAuth the cloud has no other way to learn
# the channel id for `<account>/uploads/`.
_ACCOUNT_TO_CHANNEL_ID: dict[str, str] = {}


_LAST_ENUMERATE_SOURCE: str | None = None  # surfaced in the API payload


def _enumerate_uploads() -> list[tuple[str, str, str, dict]]:
    """Yield (channel, slug, video_id, full_record) for every upload.

    Reads from GCS first (``gs://<bucket>/upload-records/``) so new
    uploads appear without redeploying the image. Falls back to walking
    ``<channel>/uploads/**/*.json`` on disk if GCS is empty or
    unreachable. Records present in BOTH places dedupe by (channel, slug).
    Handles both flat (``uploads/<slug>.json``) and nested
    (``uploads/<niche>/<slug>.json``) on-disk layouts.
    """
    global _LAST_ENUMERATE_SOURCE
    out: list[tuple[str, str, str, dict]] = []
    seen: set[tuple[str, str]] = set()  # (channel, slug)
    sources: list[str] = []

    # 1. GCS — laptop pushes records here on every upload.
    try:
        from control import storage as _gcs
        gcs_count = 0
        for channel, slug, rec in _gcs.list_upload_records():
            vid = rec.get("video_id")
            if not vid:
                continue
            key = (channel, slug)
            if key in seen:
                continue
            seen.add(key)
            out.append((channel, slug, vid, rec))
            gcs_count += 1
        if gcs_count:
            sources.append(f"gcs:{gcs_count}")
    except Exception as e:
        sources.append(f"gcs:err({type(e).__name__})")

    # 2. Disk fallback — picks up anything baked into the image but not
    # yet mirrored, plus everything on the laptop when running locally.
    disk_count = 0
    for chan_dir in sorted(PROJECT_ROOT.iterdir()):
        if not chan_dir.is_dir() or not (chan_dir / "config.yaml").exists():
            continue
        # Glob both shapes:
        #   <channel>/uploads/<slug>.json                 (flat channels)
        #   <channel>/<niche>/uploads/<slug>.json         (niched channels — post-2026-05-05 layout)
        # Single recursive pattern catches both. Skip the *.x.json X-platform
        # sidecars (different upload track).
        record_files = sorted(
            f for f in chan_dir.rglob("uploads/*.json")
            if not f.name.endswith(".x.json")
        )
        for f in record_files:
            try:
                rec = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            vid = rec.get("video_id")
            slug = rec.get("slug") or f.stem
            if not vid:
                continue
            key = (chan_dir.name, slug)
            if key in seen:
                continue
            seen.add(key)
            out.append((chan_dir.name, slug, vid, rec))
            disk_count += 1
    if disk_count:
        sources.append(f"disk:{disk_count}")

    _LAST_ENUMERATE_SOURCE = " ".join(sources) if sources else "empty"
    return out


_LAST_FETCH_ERROR: str | None = None


def _fetch_stats(video_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Hit YouTube Data API v3 with API key. Returns {video_id: {...}}.

    Records last-error to ``_LAST_FETCH_ERROR`` (surfaced in the dashboard
    response) so silent auth/quota/restriction failures aren't invisible.

    Videos missing from the response are marked ``api_visible=False`` —
    typically means the video is private/unlisted/deleted (API-key auth
    only sees public videos). The caller can then render a "private on
    YouTube" chip instead of an empty stats row.
    """
    global _LAST_FETCH_ERROR
    _LAST_FETCH_ERROR = None
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        _LAST_FETCH_ERROR = "YOUTUBE_API_KEY not set"
        return {}
    if not video_ids:
        return {}

    import urllib.parse
    import urllib.request
    import urllib.error

    out: dict[str, dict[str, Any]] = {}
    # Pre-mark every requested id as not-yet-seen; we'll flip api_visible
    # when the response includes the id.
    for vid in video_ids:
        out[vid] = {
            "viewCount": None, "likeCount": None, "commentCount": None,
            "api_visible": False, "privacy_live": None,
        }

    seen_channel_ids: set[str] = set()
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        params = urllib.parse.urlencode({
            "part": "statistics,status,snippet",
            "id": ",".join(chunk),
            "key": api_key,
        })
        url = f"https://www.googleapis.com/youtube/v3/videos?{params}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
                msg = (body.get("error") or {}).get("message") or str(e)
            except Exception:
                msg = str(e)
            _LAST_FETCH_ERROR = f"HTTP {e.code}: {msg[:200]}"
            continue
        except Exception as e:
            _LAST_FETCH_ERROR = f"{type(e).__name__}: {str(e)[:200]}"
            continue

        for item in data.get("items") or []:
            stats = item.get("statistics") or {}
            status = item.get("status") or {}
            snippet = item.get("snippet") or {}
            cid = snippet.get("channelId")
            out[item["id"]] = {
                "viewCount":    int(stats["viewCount"])    if "viewCount"    in stats else None,
                "likeCount":    int(stats["likeCount"])    if "likeCount"    in stats else None,
                "commentCount": int(stats["commentCount"]) if "commentCount" in stats else None,
                "api_visible":  True,
                "privacy_live": status.get("privacyStatus"),
                "channel_id":   cid,
            }
            if cid:
                seen_channel_ids.add(cid)

    # Fold any newly-seen channelIds into channel stats so subscribers
    # land in the same payload as the videos.
    if seen_channel_ids:
        _refresh_channel_stats(sorted(seen_channel_ids), api_key)
    return out


def _refresh_channel_stats(channel_ids: list[str], api_key: str) -> None:
    """Populate _CHANNEL_STATS_CACHE for the given channel ids.

    Best-effort — silent on errors (the per-video stats path already
    surfaces API errors via _LAST_FETCH_ERROR).
    """
    if not channel_ids:
        return
    import urllib.parse
    import urllib.request
    import urllib.error

    now = time.time()
    for i in range(0, len(channel_ids), 50):
        chunk = channel_ids[i : i + 50]
        params = urllib.parse.urlencode({
            "part": "statistics,snippet",
            "id": ",".join(chunk),
            "key": api_key,
        })
        url = f"https://www.googleapis.com/youtube/v3/channels?{params}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            continue
        except Exception:
            continue
        for item in data.get("items") or []:
            stats = item.get("statistics") or {}
            snippet = item.get("snippet") or {}
            cid = item.get("id")
            if not cid:
                continue
            _CHANNEL_STATS_CACHE[cid] = {
                "subscriber_count": (
                    int(stats["subscriberCount"]) if "subscriberCount" in stats else None
                ),
                "view_count":  int(stats["viewCount"])  if "viewCount"  in stats else None,
                "video_count": int(stats["videoCount"]) if "videoCount" in stats else None,
                "hidden_subscribers": bool(stats.get("hiddenSubscriberCount")),
                "title": snippet.get("title"),
                "fetched_at": now,
            }


def _refresh(video_ids: list[str]) -> None:
    """Refresh cache for the given ids. Best-effort — failures don't raise."""
    fetched = _fetch_stats(video_ids)
    now = time.time()
    for vid, stats in fetched.items():
        _STATS_CACHE[vid] = {**stats, "fetched_at": now}


def _ensure_fresh(video_ids: list[str]) -> None:
    """Refresh any cached entry older than TTL (or never fetched)."""
    now = time.time()
    stale = [
        v for v in video_ids
        if v not in _STATS_CACHE
        or (now - _STATS_CACHE[v].get("fetched_at", 0)) > _STATS_TTL_S
    ]
    if stale:
        _refresh(stale)


# ---- routes -----------------------------------------------------------------


@router.get("/dashboard")
async def dashboard_page() -> FileResponse:
    p = STATIC_DIR / "dashboard.html"
    if not p.exists():
        raise HTTPException(404, "dashboard.html missing from static dir")
    # no-cache forces the browser to revalidate every load via ETag/304 —
    # without this, a fresh Cloud Run deploy doesn't reach users until
    # they hard-reload (⌘⇧R). The 304 path is bandwidth-cheap.
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


# ---- 404-silencers for legacy index.html JS ----------------------------
# index.html (the operator UI) was written against web/server.py and
# fetches /api/telemetry/* + /api/research/* + /favicon.ico on load.
# Those routes don't exist on the cloud control plane (the data lives on
# the laptop). Return empty payloads so the page renders clean instead
# of spamming the console with 404s. The matching telemetry/research
# UI buttons in index.html are already `class="hidden"`.

@router.get("/favicon.ico")
async def favicon() -> Any:
    from fastapi.responses import Response
    # 204 No Content — browsers stop asking. Cheap, no static file needed.
    return Response(status_code=204)


# Only telemetry endpoints WITHOUT a real handler in
# control.routes.telemetry_routes stay stubbed here. The full set
# (overview / stages / timeline / errors / services / init_status /
# links) is served by that router — declaring them here too would
# shadow the real handlers because this router can be mounted before
# telemetry_router (first-match wins in Starlette).
@router.get("/api/telemetry/llm")
@router.get("/api/telemetry/latency")
async def telemetry_stub() -> dict:
    """Empty telemetry — pipeline runs on the laptop, not the cloud."""
    return {"events": [], "rows": [], "stages": [], "warning": "telemetry only available on the laptop UI"}


@router.get("/api/research/videos")
@router.get("/api/research/channels")
@router.get("/api/research/learnings")
async def research_stub() -> dict:
    return {"items": [], "warning": "research dashboard data lives on the laptop"}


@router.post("/api/research/rebuild")
async def research_rebuild_stub() -> dict:
    return {"ok": False, "warning": "rebuild runs on the laptop pipeline"}


@router.get("/api/dashboard/videos")
async def dashboard_videos(refresh: bool = Query(False)) -> dict:
    """Aggregate every uploaded video with live YouTube stats.

    Without ``?refresh=true``, returns whatever's in the in-memory cache,
    refreshing only entries older than 10 minutes. With ``refresh=true``,
    re-fetches every video's stats from the YouTube Data API.
    """
    uploads = _enumerate_uploads()
    if not uploads:
        return {
            "channels": [],
            "totals": {"videos": 0, "views": 0, "likes": 0, "comments": 0, "subscribers": 0},
            "latest_fetch": None,
            "warning": "No upload records on disk. Did you bake them into the image?",
        }

    video_ids = [vid for _, _, vid, _ in uploads]
    if refresh:
        _refresh(video_ids)
    else:
        _ensure_fresh(video_ids)

    api_key_set = bool(os.environ.get("YOUTUBE_API_KEY"))

    by_channel: dict[str, list[dict]] = {}
    account_channel_id: dict[str, str] = {}
    totals = {"videos": 0, "views": 0, "likes": 0, "comments": 0, "subscribers": 0}
    latest_fetch_epoch: float | None = None

    for account, slug, vid, rec in uploads:
        cached = _STATS_CACHE.get(vid, {})
        view = cached.get("viewCount")
        like = cached.get("likeCount")
        comment = cached.get("commentCount")
        fetched_at = cached.get("fetched_at")
        api_visible = cached.get("api_visible")
        privacy_live = cached.get("privacy_live")
        cid = cached.get("channel_id")
        if cid and account not in account_channel_id:
            account_channel_id[account] = cid
        if fetched_at and (latest_fetch_epoch is None or fetched_at > latest_fetch_epoch):
            latest_fetch_epoch = fetched_at

        # Decide watch URL — Shorts get the /shorts/ form (mobile-friendly).
        is_short = bool(rec.get("mp4_path", "").endswith(".mp4"))
        watch_url = (
            f"https://youtube.com/shorts/{vid}" if is_short
            else rec.get("url") or f"https://youtu.be/{vid}"
        )

        # Privacy resolution: prefer YouTube's live status. If the API
        # didn't return the video at all (api_visible=False), it's
        # private/unlisted/deleted as far as the public API can tell —
        # mark it explicitly so the upload-record's stale "public" doesn't
        # mislead the viewer.
        if privacy_live:
            privacy = privacy_live
        elif api_visible is False and api_key_set:
            privacy = "private/unlisted"
        else:
            privacy = rec.get("privacy")

        by_channel.setdefault(account, []).append({
            "slug": slug,
            "video_id": vid,
            "title": rec.get("title") or slug,
            "uploaded_at": rec.get("uploaded_at"),
            "privacy": privacy,
            "privacy_record": rec.get("privacy"),  # original upload-time value, for context
            "api_visible": api_visible,
            "watch_url": watch_url,
            "studio_url": rec.get("studio_url") or f"https://studio.youtube.com/video/{vid}/edit",
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "stats": {
                "views": view,
                "likes": like,
                "comments": comment,
                "fetched_at": (
                    None if fetched_at is None
                    else __import__("datetime").datetime.fromtimestamp(fetched_at).isoformat()
                ),
            },
        })
        totals["videos"] += 1
        if view is not None: totals["views"] += view
        if like is not None: totals["likes"] += like
        if comment is not None: totals["comments"] += comment

    channels = []
    for account, vids in sorted(by_channel.items()):
        vids.sort(key=lambda v: v.get("uploaded_at") or "", reverse=True)
        c_views = sum(v["stats"]["views"] or 0 for v in vids)
        c_likes = sum(v["stats"]["likes"] or 0 for v in vids)
        c_comments = sum(v["stats"]["comments"] or 0 for v in vids)
        cid = account_channel_id.get(account)
        chan_stats = _CHANNEL_STATS_CACHE.get(cid, {}) if cid else {}
        subs = chan_stats.get("subscriber_count")
        if subs is not None:
            totals["subscribers"] += subs
        channels.append({
            "account": account,
            "video_count": len(vids),
            "subscribers": subs,
            "subscribers_hidden": chan_stats.get("hidden_subscribers", False),
            "totals": {"views": c_views, "likes": c_likes, "comments": c_comments},
            "videos": vids,
        })

    payload: dict[str, Any] = {
        "channels": channels,
        "totals": totals,
        "latest_fetch": (
            None if latest_fetch_epoch is None
            else __import__("datetime").datetime.fromtimestamp(latest_fetch_epoch).isoformat()
        ),
        "record_source": _LAST_ENUMERATE_SOURCE,
    }
    if not api_key_set:
        payload["warning"] = (
            "YOUTUBE_API_KEY not set in this environment — stats will be "
            "empty until configured. Set it in Cloud Run env vars."
        )
    elif _LAST_FETCH_ERROR:
        payload["warning"] = f"YouTube API error: {_LAST_FETCH_ERROR}"
    return payload
