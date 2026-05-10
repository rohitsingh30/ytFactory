"""Cross-channel video catalog.

Single source of truth for "every video we've ever shipped to YouTube
across the production stable." Used by:

* `pipeline.burner_engage` — burner channels need the full catalog to
  cross-engage with.
* (future) Cross-promotion analytics, "next-up" recommendations in
  the dashboard, etc.

The catalog is derived live from each channel's ``<channel>/uploads/*.json``
records — never stored as a separate persisted index, because uploads
are the source of truth and adding/removing one shouldn't require an
explicit re-index step.

Per `pipeline.schemas.customization.CHANNEL_REGISTRY`, "production channel" =
the 7 channels currently shipping. Burner channels (anything outside
that registry) are explicitly excluded — they're consumers of the
catalog, not producers.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

from pipeline.schemas.customization import CHANNEL_REGISTRY

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class CatalogEntry:
    """One shipped video — flat, JSON-friendly, dashboard-ready."""

    video_id: str
    channel: str       # production channel slug, e.g. 'scrollpulse'
    channel_label: str # human-readable channel label
    slug: str          # the per-render slug (file basename, without .json)
    title: str         # may be empty if title wasn't set at upload time
    url: str           # canonical youtube.com link
    uploaded_at: str   # ISO timestamp from the upload record


def _channel_uploads_dir(channel_key: str) -> Path:
    """Per-channel uploads dir lives directly under the channel root.

    Niched channels (e.g. mystoriesanimated) ALSO ship uploads at the
    channel-wide root because the cron upload writes there — niche
    nesting is for narrations/raw, not uploads. Verified by the
    layout doc.
    """
    return PROJECT_ROOT / channel_key / "uploads"


def _load_upload_record(path: Path) -> dict | None:
    try:
        with path.open() as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError) as e:  # pragma: no cover - defensive
        logger.warning("skipping malformed upload record %s: %s", path, e)
        return None


def _entries_for_channel(entry: dict) -> list[CatalogEntry]:
    """Walk one channel's uploads/*.json and yield catalog entries."""
    out: list[CatalogEntry] = []
    uploads_dir = _channel_uploads_dir(entry["key"])
    if not uploads_dir.is_dir():
        return out
    for f in sorted(uploads_dir.glob("*.json")):
        # Skip the convention "_pending.md" / non-record files just in case
        # a stray .json sneaks in (e.g. cron sidecar).
        if f.name.startswith("_"):
            continue
        rec = _load_upload_record(f)
        if not rec:
            continue
        vid = rec.get("video_id")
        if not vid:
            # Some upload records are placeholder/error-state — no video
            # id means nothing to engage with. Skip silently.
            continue
        url = rec.get("url") or f"https://youtube.com/watch?v={vid}"
        title = (rec.get("title") or "").strip()
        if not title:
            # Fall back to the slug — better signal than empty string in
            # the dashboard.
            title = rec.get("slug") or f.stem
        out.append(
            CatalogEntry(
                video_id=vid,
                channel=entry["key"],
                channel_label=entry["label"],
                slug=rec.get("slug") or f.stem,
                title=title,
                url=url,
                uploaded_at=rec.get("uploaded_at") or "",
            )
        )
    return out


def list_catalog(*, channels: Iterable[str] | None = None) -> list[CatalogEntry]:
    """Return every shipped video across the production stable.

    Args:
        channels: optional whitelist of channel slugs. ``None`` means
            "all production channels in CHANNEL_REGISTRY". An invalid
            slug is silently skipped.

    Sorted newest-first by ``uploaded_at`` (lex sort works because
    timestamps are ISO-8601). Videos missing an ``uploaded_at`` sink
    to the bottom.
    """
    allow = set(channels) if channels is not None else None
    out: list[CatalogEntry] = []
    for entry in CHANNEL_REGISTRY:
        if allow is not None and entry["key"] not in allow:
            continue
        out.extend(_entries_for_channel(entry))
    out.sort(key=lambda e: e.uploaded_at or "", reverse=True)
    return out


def list_catalog_dicts(**kwargs) -> list[dict]:
    """JSON-friendly variant for FastAPI / frontend consumers."""
    return [asdict(e) for e in list_catalog(**kwargs)]


def catalog_count() -> int:
    """Cheap count without materialising every record's body."""
    n = 0
    for entry in CHANNEL_REGISTRY:
        d = _channel_uploads_dir(entry["key"])
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            if f.name.startswith("_"):
                continue
            n += 1
    return n


# ---------------------------------------------------------------------------
# CLI for ad-hoc inspection
# ---------------------------------------------------------------------------


def _cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="pipeline.utils.catalog")
    ap.add_argument("--channel", action="append", help="Filter to this channel (may repeat).")
    ap.add_argument("--json", action="store_true", help="Dump as JSON.")
    args = ap.parse_args()

    rows = list_catalog(channels=args.channel)
    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=2))
        return 0

    if not rows:
        print("(catalog empty)")
        return 0
    print(f"{len(rows)} videos:")
    for r in rows:
        print(f"  {r.video_id:14s}  {r.channel:22s}  {r.title[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
