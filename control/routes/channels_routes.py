"""Channel + customization endpoints — drives the /app/create wizard.

GET  /api/channels                              all 8 channels (gallery)
GET  /api/channels/{ch}                         one channel
GET  /api/channels/{ch}/customization_schema    fields for the form
PATCH /api/channels/{ch}/defaults               sidecar JSON, NEVER YAML

GET  /api/channels/{ch}/avatar.jpg              mirrored YT avatar
GET  /api/channels/{ch}/banner.jpg              mirrored YT banner
GET  /api/channels/{ch}/inspiration             top recent shorts
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pipeline.schemas import customization
from pipeline.paths import PROJECT_ROOT
from pipeline.research import channel_assets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels")

# Cache the brand image responses on the browser. The mirrored asset only
# changes when the user re-runs `python -m pipeline.research.youtube` —
# safe to cache aggressively (immutable for the file's URL lifetime; the
# stable URL is keyed on channel slug, not on a content hash, so we keep
# 1h to balance freshness vs. CDN-class caching).
_ASSET_CACHE_HEADERS = {"Cache-Control": "public, max-age=3600"}


def _account_for_channel(channel_key: str) -> str | None:
    """Resolve the OAuth/upload account for a registry channel key.

    Mirrors :func:`pipeline.schemas.customization._account_for` but reads from
    the channel YAML directly so this module doesn't widen the
    customization import surface.
    """
    entry = next(
        (e for e in customization.CHANNEL_REGISTRY if e["key"] == channel_key),
        None,
    )
    if entry is None:
        return None
    yaml_path = PROJECT_ROOT / entry["yaml"]
    if not yaml_path.exists():
        return entry["key"]
    try:
        ydoc = yaml.safe_load(yaml_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        ydoc = {}
    upload = ydoc.get("upload") or {}
    return upload.get("account") or entry["key"]


@router.get("")
async def list_channels() -> dict:
    chans = customization.list_channels()
    return {"channels": [c.model_dump() for c in chans]}


@router.get("/{channel}")
async def get_channel(channel: str) -> dict:
    c = customization.get_channel(channel)
    if c is None:
        raise HTTPException(status_code=404, detail=f"channel '{channel}' not found")
    return c.model_dump()


@router.get("/{channel}/customization_schema")
async def get_schema(channel: str) -> dict:
    s = customization.get_customization_schema(channel)
    if s is None:
        raise HTTPException(status_code=404, detail=f"channel '{channel}' not found")
    return s.model_dump(by_alias=True)


class DefaultsPatch(BaseModel):
    defaults: dict


@router.patch("/{channel}/defaults")
async def patch_defaults(channel: str, body: DefaultsPatch) -> dict:
    if customization.get_channel(channel) is None:
        raise HTTPException(status_code=404, detail=f"channel '{channel}' not found")
    customization.save_user_defaults(channel, body.defaults)
    return {"ok": True, "channel": channel, "defaults": body.defaults}


# ---------------------------------------------------------------------------
# Brand-asset endpoints — serve mirrored YT avatar + banner so the studio
# UI never hot-links yt3.ggpht.com (CORS / link rot). 404 lets the
# frontend fall back to a monogram + niche-themed gradient.
# ---------------------------------------------------------------------------


def _serve_asset(channel: str, kind: str) -> FileResponse:
    if customization.get_channel(channel) is None:
        raise HTTPException(status_code=404, detail=f"channel '{channel}' not found")
    account = _account_for_channel(channel)
    if not account:
        raise HTTPException(status_code=404, detail=f"channel '{channel}' has no account")
    path: Path | None = channel_assets.asset_path(account, kind)
    if path is None:
        # Surfaced as 404 deliberately — the FE uses this signal to render
        # the monogram fallback, and DevTools 404 noise is the price of
        # honest reporting (better than serving a 1×1 placeholder).
        raise HTTPException(
            status_code=404,
            detail=f"no {kind} mirrored for '{channel}' yet — run "
                   "`python -m pipeline.research.youtube`",
        )
    return FileResponse(str(path), media_type="image/jpeg", headers=_ASSET_CACHE_HEADERS)


@router.get("/{channel}/avatar.jpg")
async def get_avatar(channel: str) -> FileResponse:
    return _serve_asset(channel, "avatar")


@router.get("/{channel}/banner.jpg")
async def get_banner(channel: str) -> FileResponse:
    return _serve_asset(channel, "banner")


@router.get("/{channel}/inspiration")
async def get_inspiration(channel: str, limit: int | None = None) -> dict:
    """Recent shorts for this channel — powers the InspirationDrawer on
    the customize page (3-tile preview) and the channel page's Latest
    videos tab (full scrollable grid).

    Reads videos straight from the YT cache so the result is NOT capped
    by the channel hero card's 3-video personality preview. Optional
    ``?limit=N`` clips the response if the caller wants a smaller list.
    """
    from pipeline.research import youtube as _yt

    c = customization.get_channel(channel)
    if c is None:
        raise HTTPException(status_code=404, detail=f"channel '{channel}' not found")
    account = _account_for_channel(channel) or channel
    payload = _yt.load_account(account) or {}
    raw = payload.get("videos") or []
    if isinstance(limit, int) and limit > 0:
        raw = raw[:limit]
    videos: list[dict] = []
    for v in raw:
        vid = v.get("video_id")
        if not vid:
            continue
        videos.append(
            {
                "video_id": vid,
                "title": v.get("title") or vid,
                "thumbnail": (
                    v.get("thumbnail_url")
                    or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                ),
                "views": v.get("view_count"),
                "watch_url": v.get("url") or f"https://youtu.be/{vid}",
            }
        )
    return {
        "channel": channel,
        "label": c.label,
        "youtube_url": c.youtube_url,
        "videos": videos,
    }
