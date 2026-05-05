"""Research index builder.

YouTube is the source of truth. Walks the per-account caches written by
``pipeline.youtube_stats`` and LEFT-JOINs local artefacts (upload
records, narration scripts, critique scores, rendered mp4s) by
``video_id``. Channels are discovered via ``<channel>/config.yaml`` —
nothing here depends on the legacy ``data/shorts/`` or ``channels/``
layout.

Outputs at ``data/research/``:

    videos.jsonl    — one row per YouTube video (slug + local fields
                      may be None when no local artefact matches)
    channels.jsonl  — one row per <channel>/config.yaml, joined with
                      its YouTube channel meta + per-video rollup
    learnings.jsonl — promoted from memory feedback_*.md + critique
                      class-of-bug findings

The index is a derived view; nothing here owns truth. Re-run any time
to refresh the offline rollup. Pass ``refresh_analytics=True`` to also
hit the YouTube API first.

CLI::

    python -m pipeline.research              # rebuild all three
    python -m pipeline.research videos       # one slice
    python -m pipeline.research --refresh-analytics
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

# Cross-channel state under data/. Note RESEARCH_DIR is the cross-channel
# research aggregate (not per-channel); CRITIQUES_DIR has been migrated
# to per-channel ``<channel>/[<niche>/]/critiques/<slug>/`` since
# 2026-05-05 — the legacy aggregate dir is kept for backward-compat
# reads of pre-migration critiques.
from pipeline.paths import (  # noqa: E402
    PROJECT_ROOT,
    DATA_ROOT as DATA_DIR,
    RESEARCH_DIR,
)

CRITIQUES_DIR = DATA_DIR / "critiques"  # legacy; new critiques write per-channel
YOUTUBE_DIR = RESEARCH_DIR / "youtube"

MEMORY_DIR = Path(
    os.environ.get(
        "YTFACTORY_MEMORY_DIR",
        str(Path.home() / ".claude" / "projects" / "-Users-rohit-ytFactory" / "memory"),
    )
)

# Locked by `project_two_production_channels.md` — anything else is a variant.
PRODUCTION_CHANNELS = {"mystoriesanimated", "sportstoriesanimated"}

# Top-level dirs that are NOT channel dirs.
_NON_CHANNEL_DIRS = {
    "pipeline", "web", "data", "docs", "tests", "scripts",
    "workers", "control", "node_modules", "venv", ".venv", ".git",
}


# ---- helpers ------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _read_yaml(path: Path) -> dict | None:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return None


def _mtime_iso(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _iter_channel_dirs() -> list[Path]:
    """Top-level dirs that contain a ``config.yaml``."""
    out: list[Path] = []
    if not PROJECT_ROOT.exists():
        return out
    for d in sorted(PROJECT_ROOT.iterdir()):
        if not d.is_dir() or d.name in _NON_CHANNEL_DIRS or d.name.startswith("."):
            continue
        if (d / "config.yaml").exists():
            out.append(d)
    return out


def _channel_account(chan_dir: Path) -> str:
    cfg = _read_yaml(chan_dir / "config.yaml") or {}
    return ((cfg.get("upload") or {}).get("account")) or chan_dir.name


def _index_local_uploads() -> dict[str, dict]:
    """Scan every ``<channel>/[<niche>/]/uploads/<slug>.json`` once and
    return a ``{video_id: {channel, slug, path, record}}`` index. Missing
    or malformed records are skipped; the YouTube row stays without a
    local join.

    Globs both flat (``<channel>/uploads/<slug>.json``) and niched
    (``<channel>/<niche>/uploads/<slug>.json``) layouts via the
    recursive ``<channel>/**/uploads/*.json`` pattern. Skips the X
    sidecar (``*.x.json``) — different upload track.
    """
    out: dict[str, dict] = {}
    for chan_dir in _iter_channel_dirs():
        record_files = sorted(
            f for f in chan_dir.rglob("uploads/*.json")
            if not f.name.endswith(".x.json")
        )
        for f in record_files:
            rec = _read_json(f)
            if not rec:
                continue
            vid = rec.get("video_id")
            if not vid:
                continue
            out[vid] = {
                "channel": chan_dir.name,
                "slug": rec.get("slug") or f.stem,
                "path": f,
                "record": rec,
            }
    return out


def _local_script(chan_dir: Path, slug: str) -> tuple[Path | None, dict | None]:
    """Per-channel narration is the canonical script for the dashboard.

    Falls back to ``<chan>/scripts/<slug>.json`` if narrations don't
    have one (some channels keep the script there).
    """
    for sub in ("narrations", "scripts"):
        p = chan_dir / sub / f"{slug}.json"
        if p.exists():
            data = _read_json(p)
            if data:
                return p, data
    return None, None


def _local_mp4(chan_dir: Path, slug: str) -> Path | None:
    p = chan_dir / "shorts" / f"{slug}.mp4"
    return p if p.exists() else None


def _critique_for_slug(slug: str) -> tuple[Path | None, dict | None]:
    score = CRITIQUES_DIR / slug / f"{slug}.score.json"
    if score.exists():
        return score, _read_json(score)
    return None, None


def _critique_md_for_slug(slug: str) -> Path | None:
    p = CRITIQUES_DIR / f"{slug}.md"
    return p if p.exists() else None


def _classify_issues(issues: list[str] | None) -> tuple[int, int]:
    """Count CLASS-OF-BUG vs ONE-OFF tags in a critique's top_issues."""
    if not issues:
        return 0, 0
    cob = sum(1 for t in issues if "class-of-bug" in t.lower())
    one = sum(1 for t in issues if "one-off" in t.lower())
    return cob, one


# ---- videos.jsonl -------------------------------------------------------


def build_videos() -> list[dict]:
    """One row per YouTube video, LEFT JOINed with local artefacts.

    Source-of-truth = ``data/research/youtube/<account>.json`` written
    by :func:`pipeline.youtube_stats.fetch_account`. Videos with no
    local upload record still appear (just with ``slug``, ``script``,
    ``critique`` and ``local`` set to None).
    """
    rows: list[dict] = []
    if not YOUTUBE_DIR.exists():
        return rows

    upload_index = _index_local_uploads()
    chan_lookup = {d.name: d for d in _iter_channel_dirs()}

    for cache_path in sorted(YOUTUBE_DIR.glob("*.json")):
        cache = _read_json(cache_path)
        if not cache:
            continue
        account = cache.get("account") or cache_path.stem
        for yt in cache.get("videos") or []:
            vid = yt.get("video_id")
            if not vid:
                continue
            local = upload_index.get(vid)
            slug: str | None = None
            channel: str | None = None
            script_block: dict | None = None
            crit_block: dict | None = None
            crit_md_path: str | None = None
            local_block: dict | None = None

            if local:
                slug = local["slug"]
                channel = local["channel"]
                chan_dir = chan_lookup.get(channel)
                rec: dict = local["record"]

                # Render artefact (may be missing — old slug, file deleted).
                mp4 = _local_mp4(chan_dir, slug) if chan_dir else None
                script_path, script = (
                    _local_script(chan_dir, slug) if chan_dir else (None, None)
                )
                crit_path, crit = _critique_for_slug(slug)
                crit_md = _critique_md_for_slug(slug)

                local_block = {
                    "channel": channel,
                    "upload_record_path": str(local["path"].relative_to(PROJECT_ROOT)),
                    "uploaded_at": rec.get("uploaded_at"),
                    "title": rec.get("title"),
                    "thumbnail_set": rec.get("thumbnail_set"),
                    "thumbnail_error": rec.get("thumbnail_error"),
                    "mp4_path": (
                        str(mp4.relative_to(PROJECT_ROOT)) if mp4 else None
                    ),
                    "mp4_size_bytes": (mp4.stat().st_size if mp4 else None),
                    "rendered_at": _mtime_iso(mp4) if mp4 else None,
                }

                if script and script_path:
                    script_block = {
                        "hook": script.get("hook"),
                        "source": script.get("source"),
                        "source_url": script.get("source_url"),
                        "narration_chars": len(script.get("narration") or ""),
                        "title_options": script.get("title_options") or [],
                        "footage_count": len(script.get("footage") or []),
                        "path": str(script_path.relative_to(PROJECT_ROOT)),
                    }

                if crit and crit_path:
                    cob, one_off = _classify_issues(crit.get("top_issues"))
                    crit_block = {
                        "score": crit.get("score"),
                        "one_line_take": crit.get("one_line_take"),
                        "top_issue_count": len(crit.get("top_issues") or []),
                        "class_of_bug_count": cob,
                        "one_off_count": one_off,
                        "highest_leverage_change": crit.get("highest_leverage_change"),
                        "system_correction_count": len(crit.get("system_corrections") or []),
                        "path": str(crit_path.relative_to(PROJECT_ROOT)),
                    }

                if crit_md:
                    crit_md_path = str(crit_md.relative_to(PROJECT_ROOT))

            rows.append({
                "video_id": vid,
                "account": account,
                "slug": slug,
                "channel": channel,
                "title": yt.get("title"),
                "description": yt.get("description"),
                "published_at": yt.get("published_at"),
                "privacy": yt.get("privacy"),
                "duration_s": yt.get("duration_s"),
                "thumbnail_url": yt.get("thumbnail_url"),
                "url": yt.get("url"),
                "youtube_channel_id": yt.get("channel_id"),
                "youtube_channel_title": yt.get("channel_title"),
                "stats": {
                    "view_count": yt.get("view_count"),
                    "like_count": yt.get("like_count"),
                    "comment_count": yt.get("comment_count"),
                    "favorite_count": yt.get("favorite_count"),
                    "fetched_at": cache.get("fetched_at"),
                },
                "local": local_block,
                "script": script_block,
                "critique": crit_block,
                "critique_md_path": crit_md_path,
            })

    return rows


# ---- channels.jsonl -----------------------------------------------------


def _aggregate(videos_for: list[dict]) -> dict:
    scores = [
        v["critique"]["score"]
        for v in videos_for
        if v.get("critique") and v["critique"].get("score") is not None
    ]
    views = [
        (v.get("stats") or {}).get("view_count") or 0
        for v in videos_for
        if (v.get("stats") or {}).get("view_count") is not None
    ]
    published = [v.get("published_at") for v in videos_for if v.get("published_at")]
    locally_rendered = [v for v in videos_for if v.get("local") and v["local"].get("mp4_path")]
    return {
        "video_count_local": len(videos_for),  # videos joined to this channel via upload record
        "locally_rendered_count": len(locally_rendered),
        "avg_score": (round(sum(scores) / len(scores), 2) if scores else None),
        "score_count": len(scores),
        "total_views": sum(views) if views else 0,
        "last_published_at": (max(published) if published else None),
        "slugs": [v["slug"] for v in videos_for if v.get("slug")],
    }


def build_channels(videos: list[dict]) -> list[dict]:
    """One row per ``<channel>/config.yaml``, joined with YouTube cache.

    The row carries authoritative numbers from YouTube
    (subscriber_count, total view_count, total video_count) plus a
    rollup over the videos this index already joined to the channel
    (which may be a subset if some videos were posted manually and
    have no local upload record).
    """
    rows: list[dict] = []

    by_channel: dict[str, list[dict]] = {}
    for v in videos:
        ch = v.get("channel")
        if ch:
            by_channel.setdefault(ch, []).append(v)

    for chan_dir in _iter_channel_dirs():
        cfg = _read_yaml(chan_dir / "config.yaml") or {}
        account = ((cfg.get("upload") or {}).get("account")) or chan_dir.name

        cache = _read_json(YOUTUBE_DIR / f"{account}.json")
        yt_channel = (cache or {}).get("channel") or {}
        yt_videos = (cache or {}).get("videos") or []

        videos_for = by_channel.get(chan_dir.name, [])
        rollup = _aggregate(videos_for)

        rows.append({
            "kind": "channel",
            "channel": chan_dir.name,
            "name": cfg.get("name") or chan_dir.name,
            "production": chan_dir.name in PRODUCTION_CHANNELS,
            "account": account,
            "config_path": str((chan_dir / "config.yaml").relative_to(PROJECT_ROOT)),
            "source_adapter": cfg.get("source_adapter"),
            # Live YouTube numbers — None when cache is missing/auth failed.
            "youtube_channel_id": yt_channel.get("id"),
            "youtube_title": yt_channel.get("title"),
            "subscriber_count": yt_channel.get("subscriber_count"),
            "youtube_view_count": yt_channel.get("view_count"),
            "youtube_video_count": yt_channel.get("video_count"),
            "hidden_subscribers": yt_channel.get("hidden_subscribers"),
            "youtube_fetched_at": (cache or {}).get("fetched_at"),
            "youtube_uploads_seen": len(yt_videos),
            **rollup,
        })

    return rows


# ---- learnings.jsonl ----------------------------------------------------


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def _parse_frontmatter_md(path: Path) -> tuple[dict, str]:
    txt = path.read_text()
    m = _FRONTMATTER_RE.match(txt)
    if not m:
        return {}, txt
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, m.group(2)


def _excerpt(body: str, max_chars: int = 320) -> str:
    body = body.strip()
    if len(body) <= max_chars:
        return body
    return body[: max_chars - 1].rstrip() + "…"


def _learnings_from_memory() -> list[dict]:
    rows: list[dict] = []
    if not MEMORY_DIR.exists():
        return rows
    for md in sorted(MEMORY_DIR.glob("*.md")):
        if md.name == "MEMORY.md":
            continue
        meta, body = _parse_frontmatter_md(md)
        rows.append({
            "id": f"memory:{md.stem}",
            "source": "memory",
            "source_path": str(md.relative_to(MEMORY_DIR.parent)),
            "type": meta.get("type") or "memory",
            "title": meta.get("name") or md.stem,
            "description": meta.get("description") or "",
            "channel": None,
            "body_excerpt": _excerpt(body),
            "related_slugs": [],
        })
    return rows


def _learnings_from_critiques(videos: list[dict]) -> list[dict]:
    """Promote each system_correction in a critique to its own learning row."""
    rows: list[dict] = []
    for v in videos:
        crit_meta = v.get("critique")
        if not crit_meta or not crit_meta.get("path"):
            continue
        full = _read_json(PROJECT_ROOT / crit_meta["path"])
        if not full:
            continue
        slug = v.get("slug")
        for sc in full.get("system_corrections") or []:
            issue_class = (sc.get("issue_class") or "").strip()
            if not issue_class:
                continue
            rows.append({
                "id": f"critique:{slug or v.get('video_id')}:{issue_class}",
                "source": "critique",
                "source_path": crit_meta["path"],
                "type": "class-of-bug",
                "title": issue_class,
                "description": (sc.get("principle") or "")[:200],
                "channel": v.get("channel"),
                "body_excerpt": _excerpt(
                    (sc.get("fix") or "") + "\n\nWHERE: " + (sc.get("where") or "")
                ),
                "related_slugs": [slug] if slug else [],
            })
    return rows


def build_learnings(videos: list[dict]) -> list[dict]:
    rows = _learnings_from_memory() + _learnings_from_critiques(videos)
    rows.sort(key=lambda r: (r["source"] != "memory", r.get("title") or ""))
    return rows


# ---- writers ------------------------------------------------------------


def _write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def rebuild(
    slices: list[str] | None = None,
    *,
    quiet: bool = False,
    refresh_analytics: bool = False,
) -> dict:
    """Rebuild the requested slices (or all). Returns counts.

    When ``refresh_analytics`` is True, hits the YouTube API first via
    :func:`pipeline.youtube_stats.fetch_all` so the cache reflects
    current view/like/comment counts and any newly-published videos.
    Otherwise the rebuild is purely offline (re-reads the cached JSON).
    """
    targets = set(slices or ["videos", "channels", "learnings"])
    out: dict[str, Any] = {"built_at": datetime.now(tz=timezone.utc).isoformat()}

    if refresh_analytics:
        from pipeline import youtube_stats

        out["analytics"] = youtube_stats.fetch_all(quiet=quiet)

    videos = build_videos() if {"videos", "channels", "learnings"} & targets else []

    if "videos" in targets:
        n = _write_jsonl(RESEARCH_DIR / "videos.jsonl", videos)
        out["videos"] = n
        if not quiet:
            print(f"[research] videos.jsonl: {n} rows")

    if "channels" in targets:
        rows = build_channels(videos)
        n = _write_jsonl(RESEARCH_DIR / "channels.jsonl", rows)
        out["channels"] = n
        if not quiet:
            print(f"[research] channels.jsonl: {n} rows")

    if "learnings" in targets:
        rows = build_learnings(videos)
        n = _write_jsonl(RESEARCH_DIR / "learnings.jsonl", rows)
        out["learnings"] = n
        if not quiet:
            print(f"[research] learnings.jsonl: {n} rows")

    return out


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ---- CLI ----------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(prog="pipeline.research")
    ap.add_argument(
        "slice",
        nargs="*",
        choices=["videos", "channels", "learnings"],
        help="Which slice(s) to rebuild (default: all).",
    )
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--refresh-analytics",
        action="store_true",
        help="Hit the YouTube API to refresh per-channel caches before rebuilding.",
    )
    args = ap.parse_args()

    result = rebuild(
        args.slice or None,
        quiet=args.quiet,
        refresh_analytics=args.refresh_analytics,
    )
    if args.quiet:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
