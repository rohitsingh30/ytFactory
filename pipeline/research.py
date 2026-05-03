"""Research index builder.

Walks the pipeline's filesystem state and emits a flat, regenerable
index at ``data/research/``:

    videos.jsonl    — one row per rendered short
    channels.jsonl  — one row per channel YAML
    learnings.jsonl — promoted from memory feedback_*.md + critique class-of-bug

The index is a derived view; nothing here owns truth. Re-run any time
to refresh. Single user, single host — JSONL beats SQLite for now
(grep-able, diff-able, no schema migrations).

CLI::

    python -m pipeline.research              # rebuild all three
    python -m pipeline.research videos       # one slice
    python -m pipeline.research --quiet
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHANNELS_DIR = PROJECT_ROOT / "channels"
DATA_DIR = PROJECT_ROOT / "data"
SHORTS_DIR = DATA_DIR / "shorts"
INTERMEDIATE_DIR = DATA_DIR / "intermediate"
UPLOADS_DIR = DATA_DIR / "uploads"
CRITIQUES_DIR = DATA_DIR / "critiques"
RESEARCH_DIR = DATA_DIR / "research"
ANALYTICS_DIR = RESEARCH_DIR / "analytics"

MEMORY_DIR = Path(
    os.environ.get(
        "YTFACTORY_MEMORY_DIR",
        str(Path.home() / ".claude" / "projects" / "-Users-rohit-ytFactory" / "memory"),
    )
)

# Locked by `project_two_production_channels.md` — anything else is a variant.
PRODUCTION_CHANNELS = {"mystoriesanimated", "sportstoriesanimated"}


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


def _channel_for_slug(slug: str) -> str | None:
    """Find which channel owns a slug by checking intermediate/<channel>/scripts."""
    if not INTERMEDIATE_DIR.exists():
        return None
    for ch in INTERMEDIATE_DIR.iterdir():
        if not ch.is_dir():
            continue
        if (ch / "scripts" / f"{slug}.json").exists():
            return ch.name
    return None


def _upload_for_slug(slug: str) -> tuple[Path | None, dict | None]:
    """Return (upload_path, upload_dict) for the first channel-dir match."""
    if not UPLOADS_DIR.exists():
        return None, None
    for ch in UPLOADS_DIR.iterdir():
        if not ch.is_dir():
            continue
        p = ch / f"{slug}.json"
        if p.exists():
            return p, _read_json(p)
    return None, None


def _critique_for_slug(slug: str) -> tuple[Path | None, dict | None]:
    """Match either <slug>/<slug>.score.json or top-level <slug>.md."""
    if not CRITIQUES_DIR.exists():
        return None, None
    score = CRITIQUES_DIR / slug / f"{slug}.score.json"
    if score.exists():
        return score, _read_json(score)
    return None, None


def _critique_md_for_slug(slug: str) -> Path | None:
    if not CRITIQUES_DIR.exists():
        return None
    p = CRITIQUES_DIR / f"{slug}.md"
    return p if p.exists() else None


def _analytics_for_slug(slug: str) -> dict | None:
    p = ANALYTICS_DIR / f"{slug}.json"
    if not p.exists():
        return None
    return _read_json(p)


def _classify_issues(issues: list[str] | None) -> tuple[int, int]:
    """Count CLASS-OF-BUG vs ONE-OFF tags in a critique's top_issues."""
    if not issues:
        return 0, 0
    cob = sum(1 for t in issues if "class-of-bug" in t.lower())
    one = sum(1 for t in issues if "one-off" in t.lower())
    return cob, one


# ---- videos.jsonl -------------------------------------------------------


def build_videos() -> list[dict]:
    """One row per .mp4 in data/shorts/, joined with script + upload + critique."""
    rows: list[dict] = []
    if not SHORTS_DIR.exists():
        return rows

    for mp4 in sorted(SHORTS_DIR.glob("*.mp4")):
        slug = mp4.stem
        # Skip "copy" duplicates that macOS finder leaves behind.
        if " copy" in slug:
            continue

        channel = _channel_for_slug(slug)
        script_path = (
            INTERMEDIATE_DIR / channel / "scripts" / f"{slug}.json"
            if channel
            else None
        )
        script = _read_json(script_path) if script_path else None

        upload_path, upload = _upload_for_slug(slug)
        crit_path, crit = _critique_for_slug(slug)
        crit_md = _critique_md_for_slug(slug)
        analytics = _analytics_for_slug(slug)

        cob, one_off = _classify_issues(crit.get("top_issues") if crit else None)

        row = {
            "slug": slug,
            "channel": channel,
            "mp4_path": str(mp4.relative_to(PROJECT_ROOT)),
            "mp4_size_bytes": mp4.stat().st_size,
            "rendered_at": _mtime_iso(mp4),
            "script": (
                {
                    "hook": script.get("hook"),
                    "source": script.get("source"),
                    "source_url": script.get("source_url"),
                    "narration_chars": len(script.get("narration") or ""),
                    "title_options": script.get("title_options") or [],
                    "footage_count": len(script.get("footage") or []),
                    "path": str(script_path.relative_to(PROJECT_ROOT)),
                }
                if script and script_path
                else None
            ),
            "upload": (
                {
                    "video_id": upload.get("video_id"),
                    "url": upload.get("url"),
                    "uploaded_at": upload.get("uploaded_at"),
                    "title": upload.get("title"),
                    "privacy": upload.get("privacy"),
                    "account": upload.get("account"),
                    "thumbnail_set": upload.get("thumbnail_set"),
                    "thumbnail_error": upload.get("thumbnail_error"),
                    "path": str(upload_path.relative_to(PROJECT_ROOT)),
                }
                if upload and upload_path
                else None
            ),
            "critique": (
                {
                    "score": crit.get("score"),
                    "one_line_take": crit.get("one_line_take"),
                    "top_issue_count": len(crit.get("top_issues") or []),
                    "class_of_bug_count": cob,
                    "one_off_count": one_off,
                    "highest_leverage_change": crit.get("highest_leverage_change"),
                    "system_correction_count": len(crit.get("system_corrections") or []),
                    "path": str(crit_path.relative_to(PROJECT_ROOT)),
                }
                if crit and crit_path
                else None
            ),
            "critique_md_path": (
                str(crit_md.relative_to(PROJECT_ROOT)) if crit_md else None
            ),
            "analytics": (
                {
                    "view_count": analytics.get("view_count"),
                    "like_count": analytics.get("like_count"),
                    "comment_count": analytics.get("comment_count"),
                    "fetched_at": analytics.get("fetched_at"),
                }
                if analytics
                else None
            ),
        }
        rows.append(row)

    return rows


# ---- channels.jsonl -----------------------------------------------------


def _aggregate(videos_for: list[dict]) -> dict:
    """Numeric rollup shared between the YAML and account aggregations."""
    scores = [
        v["critique"]["score"]
        for v in videos_for
        if v.get("critique") and v["critique"].get("score") is not None
    ]
    uploaded = [v for v in videos_for if v.get("upload")]
    rendered_dates = [v.get("rendered_at") for v in videos_for if v.get("rendered_at")]
    return {
        "video_count": len(videos_for),
        "uploaded_count": len(uploaded),
        "avg_score": (round(sum(scores) / len(scores), 2) if scores else None),
        "score_count": len(scores),
        "last_render_at": (max(rendered_dates) if rendered_dates else None),
        "slugs": [v["slug"] for v in videos_for],
    }


def build_channels(videos: list[dict]) -> list[dict]:
    """Two complementary views, both written into channels.jsonl:

      kind="yaml"     → one row per channels/*.yaml (recipe definition)
      kind="account"  → one row per distinct upload.account (where it ships)

    The mismatch matters: e.g. ``mystoriesanimated.yaml`` and
    ``aita_animated.yaml`` both ship to the YouTube account
    ``mystoriesanimated``, but the rendered intermediate dir is
    ``reddit_amitheasshole``. The two-row-kinds layout makes that
    explicit instead of forcing a single join key.
    """
    rows: list[dict] = []

    # Group videos two ways.
    by_intermediate: dict[str, list[dict]] = {}
    by_account: dict[str, list[dict]] = {}
    for v in videos:
        ch = v.get("channel")
        if ch:
            by_intermediate.setdefault(ch, []).append(v)
        acct = (v.get("upload") or {}).get("account")
        if acct:
            by_account.setdefault(acct, []).append(v)

    # ---- kind=yaml ------------------------------------------------------
    # "production" on a yaml row means this YAML *is* the canonical
    # production recipe — i.e. its filename matches a production channel.
    # Sibling YAMLs that happen to share an upload account are variants,
    # not production recipes (per project_two_production_channels.md).
    if CHANNELS_DIR.exists():
        for yml in sorted(CHANNELS_DIR.glob("*.yaml")):
            cfg = _read_yaml(yml) or {}
            channel_id = cfg.get("channel_dir") or cfg.get("name") or yml.stem
            upload_account = ((cfg.get("upload") or {}).get("account")) or None

            videos_for = by_intermediate.get(channel_id, [])
            row = {
                "kind": "yaml",
                "channel": channel_id,
                "yaml_path": str(yml.relative_to(PROJECT_ROOT)),
                "yaml_filename": yml.name,
                "name": cfg.get("name"),
                "production": yml.stem in PRODUCTION_CHANNELS,
                "source_adapter": cfg.get("source_adapter"),
                "upload_account": upload_account,
                **_aggregate(videos_for),
            }
            rows.append(row)

    # ---- kind=intermediate-orphan --------------------------------------
    # Render dirs that no YAML claims (e.g. legacy `reddit_amitheasshole`
    # backing `aita_animated.yaml`). Surface them so their videos aren't
    # invisible just because the YAML uses a different channel_id.
    seen_intermediate = {
        v.get("channel")
        for r in rows
        for v in by_intermediate.get(r["channel"], [])
    }
    if INTERMEDIATE_DIR.exists():
        for ch_dir in sorted(INTERMEDIATE_DIR.iterdir()):
            if not ch_dir.is_dir() or ch_dir.name in seen_intermediate:
                continue
            videos_for = by_intermediate.get(ch_dir.name, [])
            if not videos_for:
                continue  # empty dir, nothing to surface
            rows.append(
                {
                    "kind": "intermediate",
                    "channel": ch_dir.name,
                    "yaml_path": None,
                    "yaml_filename": None,
                    "name": None,
                    "production": False,
                    "source_adapter": None,
                    "upload_account": None,
                    "note": "no YAML claims this intermediate dir",
                    **_aggregate(videos_for),
                }
            )

    # ---- kind=account ---------------------------------------------------
    for acct, videos_for in sorted(by_account.items()):
        rows.append(
            {
                "kind": "account",
                "channel": acct,
                "yaml_path": None,
                "yaml_filename": None,
                "name": acct,
                "production": acct in PRODUCTION_CHANNELS,
                "source_adapter": None,
                "upload_account": acct,
                **_aggregate(videos_for),
            }
        )

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
        rows.append(
            {
                "id": f"memory:{md.stem}",
                "source": "memory",
                "source_path": str(md.relative_to(MEMORY_DIR.parent)),
                "type": meta.get("type") or "memory",
                "title": meta.get("name") or md.stem,
                "description": meta.get("description") or "",
                "channel": None,
                "body_excerpt": _excerpt(body),
                "related_slugs": [],
            }
        )
    return rows


def _learnings_from_critiques(videos: list[dict]) -> list[dict]:
    """Promote each system_correction in a critique to its own learning row.

    Class-of-bug fixes from the critic are the closest thing we have to
    durable findings; one-offs are filtered out because they don't
    generalise across future renders.
    """
    rows: list[dict] = []
    for v in videos:
        crit_meta = v.get("critique")
        if not crit_meta or not crit_meta.get("path"):
            continue
        full = _read_json(PROJECT_ROOT / crit_meta["path"])
        if not full:
            continue
        for sc in full.get("system_corrections") or []:
            issue_class = (sc.get("issue_class") or "").strip()
            if not issue_class:
                continue
            rows.append(
                {
                    "id": f"critique:{v['slug']}:{issue_class}",
                    "source": "critique",
                    "source_path": crit_meta["path"],
                    "type": "class-of-bug",
                    "title": issue_class,
                    "description": (sc.get("principle") or "")[:200],
                    "channel": v.get("channel"),
                    "body_excerpt": _excerpt(
                        (sc.get("fix") or "") + "\n\nWHERE: " + (sc.get("where") or "")
                    ),
                    "related_slugs": [v["slug"]],
                }
            )
    return rows


def build_learnings(videos: list[dict]) -> list[dict]:
    rows = _learnings_from_memory() + _learnings_from_critiques(videos)
    # Stable order: memory first (durable), then critique findings sorted by
    # issue_class so the same issue across multiple slugs sits adjacent.
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

    When ``refresh_analytics`` is True, also calls
    ``pipeline.youtube_stats.fetch_all`` first so videos.jsonl picks up
    the latest viewCount/likeCount/commentCount. Network call — only
    pass True when the user explicitly asks (CLI flag, "Refresh stats"
    button).
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
        help="Pull fresh YouTube view/like/comment counts before rebuilding (network).",
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
