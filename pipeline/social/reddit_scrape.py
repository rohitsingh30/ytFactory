"""Reddit thread scraper for ScrollPulse — public JSON API, no auth.

Reddit returns full post + comment trees as JSON when you append ``.json`` to
any thread URL. No PRAW dependency, no OAuth. Just a sane User-Agent header.

Usage:
    python -m pipeline.social.reddit_scrape --subreddit AmItheAsshole --top day --out raw/aita-<slug>.json
    python -m pipeline.social.reddit_scrape --thread-url https://reddit.com/r/.../comments/<id>/<slug> --out raw/<slug>.json

Output schema:
    {
      "subreddit": "AmItheAsshole",
      "scraped_at": "2026-05-08T...Z",
      "post": {
        "id", "url", "title", "selftext", "author", "score", "num_comments",
        "upvote_ratio", "created_utc", "is_nsfw", "is_locked"
      },
      "top_comments": [
        {"id", "author", "body", "score", "depth", "controversiality"},
        ...
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = "ytFactory/ScrollPulse 1.0 (by u/ytfactory)"


def _get(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_top_post(
    subreddit: str,
    *,
    sort: str = "top",
    time_window: str = "day",
    min_score: int = 5_000,
    min_comments: int = 200,
    skip_ids: set[str] | None = None,
) -> dict | None:
    """Pick the highest-engagement post in a subreddit that passes quality gates.

    ``sort`` is one of ``top``/``hot``/``rising``. ``time_window`` only matters
    for ``top`` (``day``/``week``/``month``).
    """
    skip_ids = skip_ids or set()
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit=25"
    if sort == "top":
        url += f"&t={time_window}"
    listing = _get(url)
    for child in listing.get("data", {}).get("children", []):
        d = child["data"]
        if d.get("stickied") or d.get("over_18") or d.get("locked"):
            continue
        if d["id"] in skip_ids:
            continue
        if (d.get("score") or 0) < min_score:
            continue
        if (d.get("num_comments") or 0) < min_comments:
            continue
        # Only self-posts (text). Link posts and image posts don't render as
        # readable cards in this format.
        if not d.get("is_self"):
            continue
        return d
    return None


def fetch_post_with_comments(
    subreddit: str, post_id: str, *, top_n_comments: int = 12
) -> dict:
    url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json?limit=200&depth=2"
    listing = _get(url)
    # listing[0] = post, listing[1] = comments
    post = listing[0]["data"]["children"][0]["data"]
    comments_raw = listing[1]["data"]["children"]
    comments: list[dict] = []
    for c in comments_raw:
        if c.get("kind") != "t1":
            continue
        cd = c["data"]
        if cd.get("stickied") or not cd.get("body"):
            continue
        if cd.get("body") in ("[deleted]", "[removed]"):
            continue
        comments.append(
            {
                "id": cd["id"],
                "author": cd.get("author") or "[unknown]",
                "body": cd["body"],
                "score": cd.get("score") or 0,
                "depth": cd.get("depth") or 0,
                "controversiality": cd.get("controversiality") or 0,
                "permalink": f"https://reddit.com{cd.get('permalink', '')}",
            }
        )
    comments.sort(key=lambda c: c["score"], reverse=True)
    return {
        "post": {
            "id": post["id"],
            "url": f"https://reddit.com{post['permalink']}",
            "subreddit": post["subreddit"],
            "title": post["title"],
            "selftext": post.get("selftext") or "",
            "author": post.get("author") or "[unknown]",
            "score": post["score"],
            "num_comments": post["num_comments"],
            "upvote_ratio": post.get("upvote_ratio"),
            "created_utc": post.get("created_utc"),
            "is_nsfw": post.get("over_18", False),
            "is_locked": post.get("locked", False),
        },
        "top_comments": comments[:top_n_comments],
    }


def _parse_thread_url(url: str) -> tuple[str, str]:
    """``https://www.reddit.com/r/<sub>/comments/<id>/<slug>/`` → (sub, id)."""
    parts = urllib.parse.urlparse(url).path.strip("/").split("/")
    # ['r', '<sub>', 'comments', '<id>', '<slug>?']
    if len(parts) < 4 or parts[0] != "r" or parts[2] != "comments":
        raise SystemExit(f"Not a thread URL: {url}")
    return parts[1], parts[3]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--subreddit", help="Subreddit name (no r/ prefix)")
    src.add_argument("--thread-url", help="Direct thread URL")
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--sort", default="top", choices=["top", "hot", "rising"])
    ap.add_argument("--time-window", default="day", choices=["day", "week", "month"])
    ap.add_argument("--min-score", type=int, default=5000)
    ap.add_argument("--min-comments", type=int, default=200)
    ap.add_argument("--top-n-comments", type=int, default=12)
    ap.add_argument(
        "--skip-ids-file",
        type=pathlib.Path,
        default=None,
        help="JSON list of post IDs to skip (e.g. previously-shipped threads)",
    )
    args = ap.parse_args()

    skip = set()
    if args.skip_ids_file and args.skip_ids_file.exists():
        try:
            skip = set(json.loads(args.skip_ids_file.read_text()))
        except Exception:
            pass

    if args.thread_url:
        subreddit, post_id = _parse_thread_url(args.thread_url)
        bundle = fetch_post_with_comments(subreddit, post_id, top_n_comments=args.top_n_comments)
    else:
        post = fetch_top_post(
            args.subreddit,
            sort=args.sort,
            time_window=args.time_window,
            min_score=args.min_score,
            min_comments=args.min_comments,
            skip_ids=skip,
        )
        if not post:
            print(f"[reddit_scrape] no post passes gates in r/{args.subreddit}", file=sys.stderr)
            sys.exit(1)
        bundle = fetch_post_with_comments(
            args.subreddit, post["id"], top_n_comments=args.top_n_comments
        )

    bundle["scraped_at"] = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(bundle, indent=2, ensure_ascii=False))
    p = bundle["post"]
    print(
        f"[reddit_scrape] r/{p['subreddit']}  {p['title'][:60]!r}  "
        f"score={p['score']}  comments={p['num_comments']}  "
        f"top_comments={len(bundle['top_comments'])}  -> {args.out}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
