"""Cross-channel video catalog.

Single source of truth for "every video we've ever shipped to YouTube
across the production stable." Used by:

* `pipeline.cross_engage.burner_engage` — burner channels need the
  full catalog to cross-engage with.
* (future) Cross-promotion analytics, "next-up" recommendations in
  the dashboard, etc.

The catalog is derived live from each channel's ``<channel>/uploads/*.json``
records — never stored as a separate persisted index, because uploads
are the source of truth and adding/removing one shouldn't require an
explicit re-index step.

**Cloud / laptop split (post 2026-05-09 cutover).** Production upload
records live in ``gs://$YTFACTORY_STATE_BUCKET/<channel>/uploads/*.json``.
Laptop dev uses on-disk records under ``<repo>/<channel>/uploads/``.
This module reads from GCS first when ``YTFACTORY_STATE_BUCKET`` is
set (cloud control plane AND the laptop worker invoked via the agent
queue both inherit this env), then falls back to disk for purely-local
workflows. A small in-process TTL cache absorbs the 5s dashboard
poll without hammering GCS.

Per `pipeline.schemas.customization.CHANNEL_REGISTRY`, "production channel" =
the 7 channels currently shipping. Burner channels (anything outside
that registry) are explicitly excluded — they're consumers of the
catalog, not producers.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

from pipeline.schemas.customization import CHANNEL_REGISTRY

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Cloud (GCS) backing for upload records — see module docstring.
# ---------------------------------------------------------------------------
_STATE_BUCKET_ENV = "YTFACTORY_STATE_BUCKET"
_GCS_CACHE_TTL_S = 5.0
_GCS_CLIENT = None  # lazy module-global storage client
_GCS_ENTRIES_CACHE: dict[str, tuple[float, list]] = {}

# `catalog_count()` is hit on every poll of /api/burner_channels (the
# dashboard refreshes every 5s) and would otherwise fan out to one
# `list_blobs` per production channel — ~1k blobs per call across the
# 7-channel stable, several seconds round-trip on Cloud Run. Uploads
# happen at most a few times per day, so a 60s TTL is more than fine
# and turns the steady-state cost into ~0ms.
_GCS_COUNT_CACHE: tuple[float, int] | None = None
_COUNT_CACHE_TTL_S = 60.0


def _state_bucket() -> str | None:
    return os.environ.get(_STATE_BUCKET_ENV) or None


def _gcs_client():
    """Lazy module-global storage client; None when the GCP extras
    aren't installed (laptop dev without google.cloud.storage)."""
    global _GCS_CLIENT  # noqa: PLW0603
    if _GCS_CLIENT is not None:
        return _GCS_CLIENT
    try:
        from google.cloud import storage  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        _GCS_CLIENT = storage.Client(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2"),
        )
    except Exception:  # noqa: BLE001
        return None
    return _GCS_CLIENT


def _entries_for_channel_gcs(entry: dict, bucket_name: str) -> list:
    """GCS variant of ``_entries_for_channel`` — reads upload records
    from ``gs://<bucket>/<channel>/uploads/*.json`` (and the
    niche-nested ``<channel>/<niche>/uploads/*.json`` paths since
    mystoriesanimated and other niched layouts ship there too)."""
    cache_key = f"{bucket_name}|{entry['key']}"
    now = time.time()
    cached = _GCS_ENTRIES_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    out: list[CatalogEntry] = []
    cli = _gcs_client()
    if cli is None:
        _GCS_ENTRIES_CACHE[cache_key] = (now + _GCS_CACHE_TTL_S, out)
        return out

    try:
        bucket = cli.bucket(bucket_name)
        # Match both <channel>/uploads/<slug>.json AND
        # <channel>/<niche>/uploads/<slug>.json by listing under the
        # channel prefix and filtering on the path shape.
        for blob in cli.list_blobs(bucket_name, prefix=f"{entry['key']}/"):
            name = blob.name
            parts = name.split("/")
            if len(parts) < 3 or parts[-2] != "uploads" or not name.endswith(".json"):
                continue
            stem = Path(parts[-1]).stem
            # Skip the convention "_pending.md" / non-record files /
            # X-poster sidecars (matching laptop semantics in
            # _entries_for_channel + _load_upload_record).
            if parts[-1].startswith("_") or name.endswith(".x.json"):
                continue
            try:
                rec = json.loads(blob.download_as_text())
            except Exception as e:  # noqa: BLE001
                logger.warning("skipping malformed GCS upload record %s: %s",
                               name, e)
                continue
            vid = rec.get("video_id")
            if not vid:
                continue
            url = rec.get("url") or f"https://youtube.com/watch?v={vid}"
            title = (rec.get("title") or "").strip()
            if not title:
                title = rec.get("slug") or stem
            out.append(
                CatalogEntry(
                    video_id=vid,
                    channel=entry["key"],
                    channel_label=entry["label"],
                    slug=rec.get("slug") or stem,
                    title=title,
                    url=url,
                    uploaded_at=rec.get("uploaded_at") or "",
                ),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("catalog: GCS read for %s failed: %s", entry["key"], e)

    _GCS_ENTRIES_CACHE[cache_key] = (now + _GCS_CACHE_TTL_S, out)
    return out


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
    """Walk one channel's uploads/*.json and yield catalog entries.

    Cloud-aware: when ``YTFACTORY_STATE_BUCKET`` is set, reads from
    GCS first (post 2026-05-09 cutover layout). On disk-only laptop
    runs, falls back to ``<repo>/<channel>/uploads/``.
    """
    bucket = _state_bucket()
    if bucket:
        gcs = _entries_for_channel_gcs(entry, bucket)
        if gcs:
            return gcs
        # Fall through to disk only when GCS is empty for this channel
        # — handles a brand-new channel before its first cloud upload.

    out: list[CatalogEntry] = []
    uploads_dir = _channel_uploads_dir(entry["key"])
    if not uploads_dir.is_dir():
        return out
    for f in sorted(uploads_dir.glob("*.json")):
        # Skip the convention "_pending.md" / non-record files just in case
        # a stray .json sneaks in (e.g. cron sidecar).
        if f.name.startswith("_") or f.name.endswith(".x.json"):
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
    """Cheap count without materialising every record's body.

    Cloud-aware: when ``YTFACTORY_STATE_BUCKET`` is set, counts blobs
    in GCS (avoiding the per-blob download). Falls back to disk.

    Memoised for ``_COUNT_CACHE_TTL_S`` seconds (60s) so the dashboard's
    5s polling loop doesn't hammer GCS — see module-level
    ``_GCS_COUNT_CACHE`` for the rationale.
    """
    global _GCS_COUNT_CACHE  # noqa: PLW0603
    now = time.time()
    if _GCS_COUNT_CACHE is not None and _GCS_COUNT_CACHE[0] > now:
        return _GCS_COUNT_CACHE[1]

    bucket = _state_bucket()
    if bucket:
        cli = _gcs_client()
        if cli is not None:
            n = 0
            try:
                for entry in CHANNEL_REGISTRY:
                    for blob in cli.list_blobs(bucket, prefix=f"{entry['key']}/"):
                        parts = blob.name.split("/")
                        if (len(parts) >= 3 and parts[-2] == "uploads"
                                and blob.name.endswith(".json")
                                and not parts[-1].startswith("_")
                                and not blob.name.endswith(".x.json")):
                            n += 1
                _GCS_COUNT_CACHE = (now + _COUNT_CACHE_TTL_S, n)
                return n
            except Exception as e:  # noqa: BLE001
                logger.warning("catalog_count: GCS list failed, falling back to disk: %s", e)

    n = 0
    for entry in CHANNEL_REGISTRY:
        d = _channel_uploads_dir(entry["key"])
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            if f.name.startswith("_") or f.name.endswith(".x.json"):
                continue
            n += 1
    _GCS_COUNT_CACHE = (now + _COUNT_CACHE_TTL_S, n)
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
