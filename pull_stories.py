"""Unified entry point for the source-mining stages.

Wraps the per-niche adapters in ``sources/`` so one command pulls
raw stories AND turns them into ready-to-render scripts (the
autonomous flow):

    .venv/bin/python pull_stories.py reddit --subreddit AmItheAsshole --limit 5
    # → data/intermediate/<channel>/{raw,scripts,cast}/<slug>.json

Pass ``--no-llm`` to keep only the raw pull (debug / pre-LLM).

Per story: pull → visualizability filter → rewrite (claude CLI)
→ author cast (claude CLI). Bad stories drop out at the
visualizability stage; bad rewrites surface at script_check time
when ``make_shorts.py`` runs.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from pathlib import Path

import yaml

from pipeline import cast as cast_mod, rewrite, visualizability
from sources import save_raw
from sources import drama, reddit_api, today_in_history, wikipedia, youtube_video


def _as_dict(story) -> dict:
    """RawStory dataclass → dict so rewrite/cast (which call .get) work."""
    if is_dataclass(story):
        return asdict(story)
    return dict(story)


# Channel-dir → channel YAML map, derived from the canonical
# pipeline.niches.NICHE_CHANNEL so adding a niche YAML is a one-line
# change in one file. Previously this dict was hand-curated and
# missing entries (reddit_tifu, reddit_maliciouscompliance,
# reddit_prorevenge) silently fell through to an empty channel_cfg —
# which dropped the rewriter's closer_format and visualizability
# threshold, so TIFU narrations ended up with AITA-style closers.
from pipeline.niches import NICHE_CHANNEL as _NICHE_CHANNEL

_DEFAULT_CHANNEL_YAML: dict[str, str] = {
    channel_dir: channel_yaml
    for channel_dir, channel_yaml in _NICHE_CHANNEL.values()
}


def _load_channel_cfg(channel: str) -> tuple[dict, float]:
    """Return (channel_cfg, visualizability_threshold)."""
    yaml_path = _DEFAULT_CHANNEL_YAML.get(channel)
    if yaml_path and Path(yaml_path).exists():
        cfg = yaml.safe_load(Path(yaml_path).read_text())
    else:
        cfg = {}
    threshold = float(cfg.get("visualizability_threshold", 0.4))
    return cfg, threshold


def _seen_post_ids(raw_dest: Path) -> set[str]:
    """Collect post_ids of stories already pulled into this channel.

    Used by the skip-seen guard so the same Reddit top-of-day doesn't
    get rendered repeatedly. Looks at every existing raw/<slug>.json
    and reads ``metadata.post_id``. Cheap — a directory scan plus a
    JSON parse per file.
    """
    if not raw_dest.exists():
        return set()
    seen: set[str] = set()
    for path in raw_dest.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        pid = (data.get("metadata") or {}).get("post_id")
        if pid:
            seen.add(str(pid))
    return seen


def _emit(
    stories: list[dict],
    channel: str,
    out_root: Path,
    *,
    run_llm: bool = True,
    pick: str = "all",
) -> None:
    if not stories:
        print(f"  (no stories — nothing written for channel '{channel}')")
        return

    raw_dest = out_root / channel / "raw"
    scripts_dest = out_root / channel / "scripts"
    cast_dest = out_root / channel / "cast"

    cfg, vt = _load_channel_cfg(channel)

    # Skip-seen guard: drop candidates whose post_id is already on disk
    # for this channel. Reddit's top-of-day rotates slowly, so without
    # this every click within a 24h window re-renders the same Short.
    # We keep both the ORIGINAL story object (RawStory dataclass — needed
    # by save_raw) and a dict view (used by filters and rewrite/cast)
    # as parallel lists; converting to dict-only would break save_raw,
    # which uses attribute access and dataclasses.asdict().
    seen = _seen_post_ids(raw_dest)
    n_skipped_seen = 0
    candidates: list[tuple[object, dict]] = []
    for raw_story in stories:
        s = _as_dict(raw_story)
        pid = str((s.get("metadata") or {}).get("post_id") or "")
        if pid and pid in seen:
            n_skipped_seen += 1
            print(f"  [skip-seen] {s['slug'][:60]} (post_id={pid})")
            continue
        candidates.append((raw_story, s))
    if n_skipped_seen:
        print(f"  (skipped {n_skipped_seen} already-rendered stories)")
    if not candidates:
        print(f"  (all candidates already rendered for channel '{channel}')")
        return

    # Visualizability filter — gate on whether the story is illustratable
    # at all, BEFORE drama-score (cheap regex score doesn't help an
    # unrenderable story). Run regardless of pick mode so the drama
    # selector picks from a pool that's already known to be drawable.
    survivors: list[tuple[object, dict]] = []
    for raw_story, s in candidates:
        if run_llm and cfg:
            score, reasons = visualizability.score_visualizability(
                s.get("body", "") or s.get("title", "")
            )
            if score < vt:
                print(
                    f"  [filter] DROP {score:.2f} {s['slug'][:60]}: {reasons}"
                )
                continue
        survivors.append((raw_story, s))
    if not survivors:
        print(f"  (no candidates survived visualizability filter)")
        return

    # Drama-score + pick: when pick=='best', rank by drama heuristic
    # and emit ONLY the top story. The point is to mine the spiciest
    # of N candidates rather than ship the first one off Reddit's
    # top-of-day. pick=='all' preserves the original behaviour for
    # batch CLI runs that want every candidate written.
    if pick == "best":
        survivor_dicts = [s for _, s in survivors]
        best_dict, ranked = drama.pick_best(survivor_dicts)
        print(f"  [drama] candidate leaderboard ({len(ranked)}):")
        for sd, sc, reasons in ranked[:8]:
            tag = "★" if sd is best_dict else " "
            why = ", ".join(reasons[:4])
            print(f"  [drama]  {tag} {sc:.2f}  {sd['slug'][:50]:50s} {why}")
        # Look up the matching (raw_story, dict) pair for the winner.
        emit_list = [
            (raw_story, s) for raw_story, s in survivors if s is best_dict
        ]
    else:
        emit_list = survivors

    n_raw = n_scripts = n_cast = 0
    for raw_story, s in emit_list:
        # --- raw write -------------------------------------------
        # save_raw takes the ORIGINAL dataclass (it uses .slug attribute
        # access and dataclasses.asdict for the JSON dump), not the dict.
        raw_path = save_raw(raw_story, raw_dest)
        print(f"  -> {raw_path}")
        print(f"[pull] raw written {s['slug']}")
        n_raw += 1

        if not run_llm:
            continue

        # --- rewrite + cast in parallel --------------------------
        # Both call `claude -p` and are independent of each other,
        # so we run them concurrently and roughly halve the wait.
        script_path = scripts_dest / f"{s['slug']}.json"
        cast_path = cast_dest / f"{s['slug']}.json"

        def _do_rewrite():
            script = rewrite.rewrite(s, channel_cfg=cfg)
            rewrite.save_script(script, script_path)
            print(f"     ↳ script {script_path.name}")
            print(f"[rewrite] done {s['slug']}")
            return True

        def _do_cast():
            cast_mod.author_cast(
                raw_story=s, channel_cfg=cfg, out_path=cast_path
            )
            print(f"[cast] done {s['slug']}")
            return True

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_rewrite = ex.submit(_do_rewrite)
            f_cast = ex.submit(_do_cast)
            try:
                f_rewrite.result()
                n_scripts += 1
            except Exception as e:
                print(f"     ↳ rewrite FAILED: {e}")
            try:
                f_cast.result()
                n_cast += 1
            except Exception as e:
                print(f"     ↳ cast FAILED: {e}")

    print(
        f"  wrote {n_raw} raw, {n_scripts} scripts, {n_cast} cast files "
        f"to {out_root / channel}/  "
        f"(pulled {len(stories)}, "
        f"skipped {n_skipped_seen} seen, "
        f"{len(candidates) - len(survivors)} dropped by visualizability"
        + (", picked top by drama" if pick == "best" else "")
        + ")"
    )


def cmd_reddit(args, out_root: Path) -> None:
    stories = reddit_api.fetch(
        subreddit=args.subreddit,
        listing=args.listing,
        timeframe=args.timeframe,
        limit=args.limit,
        min_chars=args.min_chars,
        max_chars=args.max_chars,
    )
    _emit(
        stories,
        args.channel or f"reddit_{args.subreddit.lower()}",
        out_root,
        run_llm=not args.no_llm,
        pick=args.pick,
    )


def cmd_wiki(args, out_root: Path) -> None:
    stories = wikipedia.fetch(
        page=args.page,
        limit=args.limit,
        min_chars=args.min_chars,
        max_chars=args.max_chars,
    )
    _emit(stories, args.channel or "wiki_oddities", out_root,
          run_llm=not args.no_llm, pick=getattr(args, "pick", "all"))


def cmd_tih(args, out_root: Path) -> None:
    stories = today_in_history.fetch(
        month=args.month,
        day=args.day,
        feed_type=args.feed,
        limit=args.limit,
    )
    _emit(stories, args.channel or "today_in_history", out_root,
          run_llm=not args.no_llm, pick=getattr(args, "pick", "all"))


def cmd_youtube(args, out_root: Path) -> None:
    stories = youtube_video.fetch(
        url_or_id=args.url,
        languages=tuple(args.languages.split(",")),
        use_whisper_fallback=args.whisper_fallback,
    )
    _emit(stories, args.channel or "reddit_video", out_root,
          run_llm=not args.no_llm, pick=getattr(args, "pick", "all"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="data/intermediate")
    ap.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip the rewrite + cast LLM stages (raw pull only).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("reddit", help="Pull stories from a subreddit's JSON API")
    p.add_argument("--subreddit", required=True)
    p.add_argument("--listing", default="top", choices=["top", "hot", "new", "rising"])
    p.add_argument("--timeframe", default="day", choices=["hour", "day", "week", "month", "year", "all"])
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--min-chars", type=int, default=400)
    p.add_argument("--max-chars", type=int, default=6000)
    p.add_argument("--channel", default=None)
    p.add_argument(
        "--pick",
        default="all",
        choices=["all", "best"],
        help="all = emit every candidate (batch CLI); best = drama-score the "
             "candidates and emit only the top-scoring story (web flow).",
    )

    p = sub.add_parser("wiki", help="Pull list-style entries from a Wikipedia page")
    p.add_argument("--page", default="unusual_deaths")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--min-chars", type=int, default=120)
    p.add_argument("--max-chars", type=int, default=1500)
    p.add_argument("--channel", default=None)

    p = sub.add_parser("tih", help="Pull today-in-history events from Wikipedia REST")
    p.add_argument("--month", type=int, default=None)
    p.add_argument("--day", type=int, default=None)
    p.add_argument("--feed", default="selected", choices=["selected", "events", "births", "deaths", "holidays"])
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--channel", default=None)

    p = sub.add_parser("youtube", help="Pull a transcript for a long-form YouTube video")
    p.add_argument("url", help="YouTube URL or 11-char video id")
    p.add_argument("--languages", default="en,en-US,en-GB")
    p.add_argument("--whisper-fallback", action="store_true")
    p.add_argument("--channel", default=None)

    args = ap.parse_args()
    out_root = Path(args.out)
    {
        "reddit": cmd_reddit,
        "wiki": cmd_wiki,
        "tih": cmd_tih,
        "youtube": cmd_youtube,
    }[args.cmd](args, out_root)


if __name__ == "__main__":
    main()
