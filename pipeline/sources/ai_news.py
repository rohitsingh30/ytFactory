"""AI news source adapter for the airecap channel.

Pulls today's top AI/tech stories from Hacker News (and optionally a
short list of AI-lab blogs) and turns each into a ``RawStory``. The
airecap rewriter (``pipeline/airecap_rewrite.py``) then condenses one
of these into a 60-second daily-recap narration.

Why HN as the primary source:

- It's the most reliable signal for "what AI news matters today" — the
  audience is the same tech audience we want on X
- Free, no API key, JSON endpoint, generous rate limit
- Stories that trend on HN have ~24h+ legs, which matches our daily
  cadence (no point chasing 2-hour Twitter trends with a 30-min render)

Filter: an AI-keyword whitelist applied to title + (optionally)
host. Misses are biased toward "too narrow" — we want the LLM rewriter
to throw out marginal items, not the scraper.

Body extraction: best-effort. We GET the linked URL with a short
timeout and pull the first chunk of paragraph text via stdlib regex.
If the link is a PDF, an arXiv abstract page, a paywalled site, or
just slow, we fall back to title-only and let the rewriter work from
the title.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from .base import RawStory, save_raw, slugify


USER_AGENT = "ytFactory/0.1 (https://github.com/local; AI news aggregator)"
HN_TIMEOUT = 15
URL_FETCH_TIMEOUT = 10
URL_FETCH_MAX_BYTES = 200_000  # cap to keep LLM context manageable

# Keywords that match the title to qualify as AI news. Lowercased
# substring match. Bias toward inclusivity — the rewriter throws out
# marginal items.
AI_KEYWORDS: tuple[str, ...] = (
    "gpt", "claude", "anthropic", "openai", "chatgpt", "gemini", "deepmind",
    "mistral", "llama", "meta ai", "grok", "xai", "perplexity", "cohere",
    "huggingface", "hugging face", "stability ai", "stable diffusion",
    "midjourney", "sora", "runway", "elevenlabs", "cartesia",
    " ai ", " ai,", " ai.", " ai:", " ai-", "(ai)", "ai/", "ai/ml",
    "llm ", " llm,", " llm.", " llms", "agentic", "agent ", "agents,",
    "rag ", "retrieval", "embedding", "transformer", "mixture of experts",
    "fine-tun", "fine tun", "rlhf", "dpo ", " moe ",
    "neural", "machine learning", "ml model", "foundation model",
    "diffusion", "generative", "multimodal", "vision-language",
    "robot", "autonomous", "self-driving", "waymo", "tesla fsd",
    "nvidia", "tpu", "h100", "h200", "b200", "gpu cluster",
    "open source ai", "opensource ai", "open-source ai",
    "ai safety", "ai alignment", "ai regulation", "ai act",
)

# Domains that are "AI-blog" sources — even if the title doesn't match
# the keyword whitelist, accept the story. Conservative list:
# DEDICATED AI labs / AI product blogs only. Excluded: arxiv.org
# (hosts every field's papers, not just AI), blog.google (mixed
# product/policy content), github.com (anything goes).
AI_BLOG_DOMAINS: tuple[str, ...] = (
    "anthropic.com",
    "openai.com",
    "deepmind.google",
    "ai.meta.com",
    "huggingface.co",
    "mistral.ai",
    "stability.ai",
    "x.ai",
)


# ---- Hacker News -------------------------------------------------------


HN_API = "https://hacker-news.firebaseio.com/v0"


def _hn_fetch_top_ids(limit: int = 200) -> list[int]:
    r = requests.get(
        f"{HN_API}/topstories.json",
        headers={"User-Agent": USER_AGENT},
        timeout=HN_TIMEOUT,
    )
    r.raise_for_status()
    return list(r.json())[:limit]


def _hn_fetch_item(item_id: int) -> dict | None:
    """One HN item. Returns dict with id/title/url/by/score/time, or None on error."""
    try:
        r = requests.get(
            f"{HN_API}/item/{item_id}.json",
            headers={"User-Agent": USER_AGENT},
            timeout=HN_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return None
        if data.get("dead") or data.get("deleted") or data.get("type") != "story":
            return None
        return data
    except requests.RequestException:
        return None


def _qualifies_as_ai(title: str, url: str | None) -> bool:
    t = " " + (title or "").lower() + " "
    if any(kw in t for kw in AI_KEYWORDS):
        return True
    if url:
        try:
            host = (urlparse(url).hostname or "").lower()
            if any(host == d or host.endswith("." + d) for d in AI_BLOG_DOMAINS):
                return True
        except ValueError:
            pass
    return False


# ---- URL body extraction (best-effort) ---------------------------------


_RE_TAG = re.compile(r"<[^>]+>")
_RE_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_RE_WS = re.compile(r"\s+")
_RE_TITLE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)


def _extract_text_from_html(html: str, *, max_chars: int = 4000) -> str:
    """Crude HTML→plaintext for an article snippet. Strips scripts/styles
    and tags, collapses whitespace. Skips obvious nav/cookie boilerplate
    by trimming leading short lines."""
    html = _RE_SCRIPT.sub(" ", html)
    text = _RE_TAG.sub(" ", html)
    text = _RE_WS.sub(" ", text).strip()
    return text[:max_chars]


def _fetch_url_body(url: str) -> str:
    """Best-effort article snippet. Returns "" on any failure."""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"},
            timeout=URL_FETCH_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "text/plain" not in ctype:
            return ""  # PDFs, images, JSON APIs etc.
        chunks: list[bytes] = []
        total = 0
        for chunk in r.iter_content(chunk_size=8192):
            chunks.append(chunk)
            total += len(chunk)
            if total >= URL_FETCH_MAX_BYTES:
                break
        body = b"".join(chunks).decode("utf-8", errors="replace")
    except requests.RequestException:
        return ""

    title_m = _RE_TITLE.search(body)
    title = (title_m.group(1).strip() if title_m else "")
    text = _extract_text_from_html(body)
    if title and not text.startswith(title[:30]):
        text = f"{title}\n\n{text}"
    return text.strip()


# ---- public API --------------------------------------------------------


def fetch(
    *,
    limit: int = 5,
    hn_top_n: int = 200,
    fetch_bodies: bool = True,
    skip_slugs: set[str] | None = None,
) -> list[RawStory]:
    """Return the top N AI/tech stories from Hacker News, newest first.

    ``limit`` is the cap on returned stories AFTER filtering.
    ``hn_top_n`` is the over-fetch from HN (we filter then cut).
    ``fetch_bodies`` toggles the per-URL article snippet (slower but
    gives the rewriter more material).
    ``skip_slugs`` — drop any story whose slug is in this set
    (dedup against already-saved raw files).
    """
    skip_slugs = set(skip_slugs or ())
    print(f"[ai_news] HN topstories.json (top {hn_top_n})…")
    try:
        ids = _hn_fetch_top_ids(hn_top_n)
    except requests.RequestException as e:
        print(f"[ai_news] HN top fetch failed: {e}", file=sys.stderr)
        return []

    out: list[RawStory] = []
    seen_urls: set[str] = set()
    fetched = 0

    for hn_id in ids:
        if len(out) >= limit:
            break
        item = _hn_fetch_item(hn_id)
        fetched += 1
        if not item:
            continue
        title = (item.get("title") or "").strip()
        url = item.get("url") or ""
        if not title:
            continue
        if not _qualifies_as_ai(title, url):
            continue
        # HN stories occasionally re-link the same URL across days.
        # Drop dupes within this run.
        canon_url = url or f"https://news.ycombinator.com/item?id={hn_id}"
        if canon_url in seen_urls:
            continue
        seen_urls.add(canon_url)

        slug = slugify(f"{datetime.now(timezone.utc).strftime('%Y%m%d')}-{title[:60]}")
        if slug in skip_slugs:
            continue

        body_parts: list[str] = [title]
        if fetch_bodies and url:
            snippet = _fetch_url_body(url)
            if snippet:
                body_parts.append(snippet)

        # HN discussion link as a backup context source.
        hn_discussion = f"https://news.ycombinator.com/item?id={hn_id}"
        body_parts.append(f"\nHN discussion: {hn_discussion} (score={item.get('score') or 0})")
        body = "\n\n".join(p for p in body_parts if p.strip())

        out.append(
            RawStory(
                slug=slug,
                title=title,
                body=body,
                source="hackernews:topstories",
                url=canon_url,
                metadata={
                    "hn_id": hn_id,
                    "hn_score": item.get("score") or 0,
                    "hn_descendants": item.get("descendants") or 0,
                    "hn_by": item.get("by"),
                    "hn_time_utc": (
                        datetime.fromtimestamp(int(item["time"]), tz=timezone.utc).isoformat()
                        if item.get("time") else None
                    ),
                    "host": (urlparse(canon_url).hostname or "").lower(),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        )

    print(f"[ai_news] kept {len(out)}/{fetched} HN items (filtered for AI relevance)")
    return out


def existing_slugs(raw_dir: Path) -> set[str]:
    """Read already-saved RawStory slugs to avoid re-picking yesterday's news."""
    if not raw_dir.exists():
        return set()
    out: set[str] = set()
    for p in raw_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text())
            if isinstance(data, dict) and data.get("slug"):
                out.add(data["slug"])
        except (OSError, json.JSONDecodeError):
            continue
    return out


# ---- CLI ---------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Pull today's AI news into airecap/raw/.")
    ap.add_argument("--limit", type=int, default=5, help="Max stories to keep")
    ap.add_argument("--hn-top-n", type=int, default=200, help="HN top-stories over-fetch")
    ap.add_argument("--no-bodies", action="store_true", help="Skip per-URL article extraction")
    ap.add_argument("--out", default="airecap/raw", help="Destination dir for RawStory JSON")
    ap.add_argument("--print-only", action="store_true", help="Print + skip writing files")
    args = ap.parse_args()

    out_dir = Path(args.out)
    skip = existing_slugs(out_dir)
    if skip:
        print(f"[ai_news] skipping {len(skip)} already-saved slugs in {out_dir}")

    stories = fetch(
        limit=args.limit,
        hn_top_n=args.hn_top_n,
        fetch_bodies=not args.no_bodies,
        skip_slugs=skip,
    )
    if not stories:
        print("[ai_news] no qualifying stories — try again later", file=sys.stderr)
        return 1

    if args.print_only:
        for s in stories:
            print(json.dumps(asdict(s), indent=2, ensure_ascii=False)[:800])
            print("---")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    for s in stories:
        path = save_raw(s, out_dir)
        print(f"  -> {path}  ({s.metadata.get('host')}, score={s.metadata.get('hn_score')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
