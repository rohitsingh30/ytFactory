"""Reddit JSON API source adapter (use case 2.3 in DESIGN.md).

Pulls top posts from a subreddit's public ``.json`` endpoint. No auth,
no PRAW dependency — Reddit serves anonymous JSON to clients with a
non-default User-Agent.

Filters that actually matter for AITA-style narrated shorts:
- Self-posts only (we need text in ``selftext``, not external links)
- Min/max body length (too short = no story; too long = won't condense well)
- Optional NSFW filter
"""

from __future__ import annotations

import argparse
from pathlib import Path

import requests

from .base import RawStory, save_raw, slugify


USER_AGENT = "ytFactory/0.1 (+https://github.com/local; story aggregator)"
TIMEOUT = 20


def fetch(
    subreddit: str,
    listing: str = "top",
    timeframe: str = "day",
    limit: int = 25,
    min_chars: int = 400,
    max_chars: int = 6000,
    skip_nsfw: bool = True,
) -> list[RawStory]:
    """Pull stories from one subreddit.

    ``listing`` is one of ``top|hot|new|rising``; ``timeframe`` is
    ``hour|day|week|month|year|all`` (only meaningful for ``top``).
    """
    url = f"https://www.reddit.com/r/{subreddit}/{listing}.json"
    params = {"limit": str(min(limit * 3, 100))}  # over-fetch, we filter
    if listing == "top":
        params["t"] = timeframe

    print(f"[reddit_api] GET {url}  ({listing}/{timeframe}, limit={limit})")
    r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()

    out: list[RawStory] = []
    for child in payload.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("stickied") or not d.get("is_self"):
            continue
        if skip_nsfw and d.get("over_18"):
            continue
        body = (d.get("selftext") or "").strip()
        if not (min_chars <= len(body) <= max_chars):
            continue

        title = d.get("title", "").strip()
        permalink = d.get("permalink", "")
        out.append(
            RawStory(
                slug=slugify(f"{subreddit}-{title}"),
                title=title,
                body=body,
                source=f"reddit:{subreddit}",
                url=f"https://reddit.com{permalink}",
                metadata={
                    "score": d.get("score", 0),
                    "num_comments": d.get("num_comments", 0),
                    "author": d.get("author"),
                    "created_utc": d.get("created_utc"),
                    "post_id": d.get("id"),
                },
            )
        )
        if len(out) >= limit:
            break

    print(f"[reddit_api] kept {len(out)} stories after filtering")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subreddit", required=True, help="e.g. AmItheAsshole, tifu, MaliciousCompliance")
    ap.add_argument("--listing", default="top", choices=["top", "hot", "new", "rising"])
    ap.add_argument("--timeframe", default="day", choices=["hour", "day", "week", "month", "year", "all"])
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--min-chars", type=int, default=400)
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--out", default="data/intermediate")
    ap.add_argument("--channel", default="aita_text")
    args = ap.parse_args()

    stories = fetch(
        subreddit=args.subreddit,
        listing=args.listing,
        timeframe=args.timeframe,
        limit=args.limit,
        min_chars=args.min_chars,
        max_chars=args.max_chars,
    )
    dest = Path(args.out) / args.channel / "raw"
    for s in stories:
        path = save_raw(s, dest)
        print(f"  -> {path}")


if __name__ == "__main__":
    main()
