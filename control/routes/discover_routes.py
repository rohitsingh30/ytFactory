"""Auto-pull a topic for a channel — drives the create-flow "Auto-pull" button.

POST /api/discover/{channel}     → one suggested topic with full source attribution
GET  /api/discover/{channel}/feed → up to 10 candidates the user can pick from

Routes the discovery to the right adapter based on the channel:
  mystoriesanimated → r/AmItheAsshole top-of-day
  scrollpulse       → round-robin across AskReddit/AITA/TIFU/etc
  historyrecapped   → Wikipedia "On this day" today
  cosmosdecoded     → Wikipedia "On this day" filtered to physics/astronomy
  scrollpulse       → ai_news.fetch() (HN top AI stories) — for the ai_recap variant
  others            → 422 "no auto-pull adapter for this channel"

The endpoint is read-only — pulling never enqueues a job. The user
clicks "Use this" in the UI which fills the Topic field.
"""
from __future__ import annotations

import logging
import random
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/discover")


# Per-channel discovery config
SCROLLPULSE_SUBREDDITS = [
    "AmItheAsshole",
    "AskReddit",
    "tifu",
    "relationship_advice",
    "MaliciousCompliance",
    "Showerthoughts",
    "TrueOffMyChest",
]


class DiscoverItem(BaseModel):
    topic: str
    source_kind: str  # reddit_url | wikipedia_topic | user_text | youtube_video | auto
    source_ref: str | None = None  # canonical URL
    source_label: str  # "r/AmItheAsshole · top of day" etc — display string
    source_excerpt: str | None = None  # short preview of the body
    metadata: dict[str, Any] = {}


class DiscoverFeed(BaseModel):
    channel: str
    adapter: str
    items: list[DiscoverItem]


def _excerpt(text: str, n: int = 280) -> str:
    text = (text or "").strip().replace("\r", "").replace("\n\n", " ¶ ")
    return text[: n - 1] + "…" if len(text) > n else text


def _reddit_items(subreddit: str, listing: str = "top",
                  timeframe: str = "day", limit: int = 8) -> list[DiscoverItem]:
    from pipeline.sources import reddit_api  # noqa: PLC0415

    stories = reddit_api.fetch(
        subreddit=subreddit,
        listing=listing,
        timeframe=timeframe,
        limit=limit,
        min_chars=300,
    )
    out: list[DiscoverItem] = []
    for s in stories:
        out.append(DiscoverItem(
            topic=s.title,
            source_kind="reddit_url",
            source_ref=s.url,
            source_label=f"r/{subreddit} · {listing} of {timeframe}",
            source_excerpt=_excerpt(s.body),
            metadata=s.metadata,
        ))
    return out


def _today_in_history_items(limit: int = 8, keyword: str | None = None) -> list[DiscoverItem]:
    from pipeline.sources import today_in_history  # noqa: PLC0415

    stories = today_in_history.fetch(limit=20)
    out: list[DiscoverItem] = []
    for s in stories:
        if keyword and keyword.lower() not in (s.title + " " + s.body).lower():
            continue
        out.append(DiscoverItem(
            topic=s.title,
            source_kind="wikipedia_topic",
            source_ref=s.url,
            source_label=f"Wikipedia · on this day{' · ' + keyword if keyword else ''}",
            source_excerpt=_excerpt(s.body),
            metadata={"year": s.metadata.get("year"), "license": "CC-BY-SA"},
        ))
        if len(out) >= limit:
            break
    return out


def _ai_news_items(limit: int = 8) -> list[DiscoverItem]:
    from pipeline.sources import ai_news  # noqa: PLC0415

    stories = ai_news.fetch(limit=limit)
    out: list[DiscoverItem] = []
    for s in stories:
        out.append(DiscoverItem(
            topic=s.title,
            source_kind="user_text",  # external article but we render as topic
            source_ref=s.url,
            source_label="HN · top AI today",
            source_excerpt=_excerpt(s.body),
            metadata=s.metadata,
        ))
    return out


def _build_feed(channel: str, *, limit: int = 8) -> DiscoverFeed:
    if channel == "mystoriesanimated":
        return DiscoverFeed(channel=channel, adapter="reddit:AmItheAsshole",
                            items=_reddit_items("AmItheAsshole", "top", "day", limit))
    if channel == "scrollpulse":
        # round-robin across subreddits — pick one at random per call
        sub = random.choice(SCROLLPULSE_SUBREDDITS)
        return DiscoverFeed(channel=channel, adapter=f"reddit:{sub} (round-robin)",
                            items=_reddit_items(sub, "top", "day", limit))
    if channel == "historyrecapped":
        return DiscoverFeed(channel=channel, adapter="wikipedia:onthisday",
                            items=_today_in_history_items(limit))
    if channel == "cosmosdecoded":
        # Filter to topics with a physics / astronomy keyword
        items: list[DiscoverItem] = []
        for kw in ("physics", "astronomy", "telescope", "rocket", "satellite",
                   "space", "discovered", "particle", "black hole"):
            items += _today_in_history_items(limit=4, keyword=kw)
            if len(items) >= limit:
                break
        # dedupe
        seen: set[str] = set()
        deduped: list[DiscoverItem] = []
        for it in items:
            key = it.source_ref or it.topic
            if key in seen:
                continue
            seen.add(key)
            deduped.append(it)
        return DiscoverFeed(channel=channel, adapter="wikipedia:onthisday (physics)",
                            items=deduped[:limit])
    if channel == "scrollpulse":  # pragma: no cover
        # AI Recap variant — daily HN AI-news pull. The split-screen
        # default has its own thread-discovery path elsewhere; this
        # surfaces AI-recap candidates for the variant picker.
        return DiscoverFeed(channel=channel, adapter="hn:ai-top",
                            items=_ai_news_items(limit))
    raise HTTPException(
        status_code=422,
        detail=f"auto-pull not available for '{channel}' — write a topic manually or "
               "use one of: mystoriesanimated, scrollpulse, historyrecapped, "
               "cosmosdecoded",
    )


@router.get("/{channel}/feed", response_model=DiscoverFeed)
async def feed(channel: str) -> DiscoverFeed:
    """Up to 10 candidate topics. UI shows them as a strip."""
    try:
        return _build_feed(channel, limit=10)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("discover feed failed for %s", channel, exc_info=True)
        raise HTTPException(status_code=502, detail=f"upstream source failed: {e}")


@router.post("/{channel}", response_model=DiscoverItem)
async def pick_one(channel: str) -> DiscoverItem:
    """One suggested topic, picked from the feed at random."""
    feed_obj = await feed(channel)
    if not feed_obj.items:
        raise HTTPException(status_code=502, detail="upstream returned no candidates")
    return random.choice(feed_obj.items[: min(5, len(feed_obj.items))])
