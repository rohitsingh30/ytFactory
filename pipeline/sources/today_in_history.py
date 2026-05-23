"""Today-in-history source adapter (use case 2.5 in DESIGN.md).

Hits Wikipedia's REST "On this day" feed. Returns one ``RawStory`` per
event for the chosen month/day. Default: today, UTC.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import requests

from pipeline.observability.event_helpers import safe_track as _track

from .base import RawStory, save_raw, slugify


USER_AGENT = "ytFactory/0.1 (https://github.com/local; story aggregator)"
TIMEOUT = 20


def _source_attempt(kind: str, ref: str, backend: str) -> None:
    _track(
        "source.fetch_attempt",
        category="http",
        metadata={"kind": kind, "ref": ref, "backend": backend},
    )


def _source_ok(*, status_code: int, body_chars: int) -> None:
    _track(
        "source.fetch_ok",
        category="http",
        success=True,
        metadata={"status_code": status_code, "body_chars": body_chars},
    )


def _source_fallback(*, reason: str, original_status: int | None = None) -> None:
    _track(
        "source.fetch_fallback",
        category="http",
        success=False,
        metadata={"fallback_reason": reason, "original_status": original_status},
    )


# Feed types: events, births, deaths, holidays, selected.
# "selected" is the human-curated set Wikipedia uses for its main page.
FEED_TYPE_DEFAULT = "selected"


def fetch(
    month: int | None = None,
    day: int | None = None,
    feed_type: str = FEED_TYPE_DEFAULT,
    limit: int = 15,
    min_chars: int = 80,
) -> list[RawStory]:
    if month is None or day is None:
        now = datetime.now(timezone.utc)
        month = month or now.month
        day = day or now.day

    url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/{feed_type}/{month:02d}/{day:02d}"
    print(f"[today_in_history] GET {url}")
    _source_attempt("today_in_history", f"{feed_type}/{month:02d}/{day:02d}", "wikipedia_rest")
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        _source_ok(status_code=getattr(r, "status_code", 200), body_chars=len(str(getattr(r, "text", "") or "")))
    except requests.HTTPError as exc:
        _source_fallback(
            reason=f"http_{getattr(exc.response, 'status_code', 'unknown')}",
            original_status=getattr(exc.response, "status_code", None),
        )
        raise
    except requests.RequestException as exc:
        _source_fallback(reason=type(exc).__name__, original_status=None)
        raise
    payload = r.json()

    out: list[RawStory] = []
    items = payload.get(feed_type, []) or payload.get("events", [])
    for ev in items:
        text = (ev.get("text") or "").strip()
        year = ev.get("year")
        if len(text) < min_chars:
            continue
        # Pull the most relevant linked page for context, if available.
        pages = ev.get("pages") or []
        primary_page = pages[0] if pages else {}
        page_title = primary_page.get("normalizedtitle") or primary_page.get("title")
        page_extract = (primary_page.get("extract") or "").strip()

        # Compose a longer body so the rewriter has material to work with.
        body_parts = [f"In {year}: {text}" if year else text]
        if page_extract and page_extract not in text:
            body_parts.append(page_extract)
        body = "\n\n".join(body_parts)

        title = f"On this day in {year}: {text[:90]}" if year else text[:120]
        url_canonical = (
            primary_page.get("content_urls", {}).get("desktop", {}).get("page")
            or f"https://en.wikipedia.org/wiki/Wikipedia:Selected_anniversaries/{month:02d}_{day:02d}"
        )

        out.append(
            RawStory(
                slug=slugify(f"tih-{month:02d}-{day:02d}-{year or 'x'}-{page_title or text[:30]}"),
                title=title,
                body=body,
                source=f"wikipedia:onthisday/{feed_type}",
                url=url_canonical,
                metadata={
                    "year": year,
                    "month": month,
                    "day": day,
                    "feed_type": feed_type,
                    "primary_page": page_title,
                    "license": "CC-BY-SA",
                },
            )
        )
        if len(out) >= limit:
            break

    print(f"[today_in_history] kept {len(out)} events for {month:02d}/{day:02d}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", type=int, default=None)
    ap.add_argument("--day", type=int, default=None)
    ap.add_argument(
        "--feed",
        default=FEED_TYPE_DEFAULT,
        choices=["selected", "events", "births", "deaths", "holidays"],
    )
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default="data/intermediate")
    ap.add_argument("--channel", default="today_in_history")
    args = ap.parse_args()

    stories = fetch(
        month=args.month,
        day=args.day,
        feed_type=args.feed,
        limit=args.limit,
    )
    dest = Path(args.out) / args.channel / "raw"
    for s in stories:
        path = save_raw(s, dest)
        print(f"  -> {path}")


if __name__ == "__main__":
    main()
