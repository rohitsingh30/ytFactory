#!/usr/bin/env python3
"""find_match_clips — score broadcast match clips for /make-sports-doc.

Given a list of candidate YouTube URLs (full matches, highlights, condensed
broadcasts) and a list of moment phrases the narration will hit, this
script:

    1. yt-dlp's each URL into sportstoriesanimated/footage/sources/<hash>.mp4
       (cached — re-runs are cheap).
    2. Whisper-aligns the source audio to get word-level timestamps.
    3. For each moment phrase, finds the best (in_s, out_s) window in each
       source via fuzzy phrase match against the commentator track.
    4. Writes a candidates JSON sorted by match-quality score so the skill
       (or the user) can pick one window per moment.

This is the KNOWN-DETERMINISTIC step. The DISCOVERY step (figuring out
which YouTube videos to download in the first place) belongs to the
caller — typically Claude using vidlens MCP `findVideos` /
`exploreYouTube` queries with the subject + "full match" /
"highlights" / "extended highlights" / "condensed game".

The output candidates JSON shape:

    {
      "subject": "Argentina France 2022 final",
      "candidates": [
        {
          "moment": "Messi opener from the spot",
          "matches": [
            {
              "url": "...",
              "in_s": 412.3, "out_s": 422.1,
              "transcript_window": "...goal goal goal Lionel Messi has scored...",
              "score": 0.92,                 # 0-1, higher is closer fuzzy match
              "buildup_secs": 1.4,           # how much pre-phrase the window includes
              "tail_secs": 1.2               # how much post-phrase the window includes
            }
          ]
        }
      ]
    }

Per feedback_sports_footage_window_must_include_buildup.md, the scorer
penalizes windows that start AT the climax word — buildup matters.

Usage:
    .venv/bin/python scripts/sportstoriesanimated/find_match_clips.py \\
        --subject "Argentina France 2022 final" \\
        --urls "https://www.youtube.com/watch?v=...,https://..." \\
        --moments "Messi opener,Di Maria second,Mbappé first,Mbappé volley,Messi extra-time,Mbappé hat-trick penalty" \\
        --out sportstoriesanimated/footage_plan/_candidates_match.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _slug_from_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _download(url: str, sources_dir: Path) -> Path:
    sources_dir.mkdir(parents=True, exist_ok=True)
    target = sources_dir / f"{_slug_from_url(url)}.mp4"
    if target.exists() and target.stat().st_size > 1024 * 100:
        return target
    print(f"[dl  ] yt-dlp {url}")
    subprocess.run([
        "yt-dlp",
        "-f", "best[ext=mp4][height<=1080]/best[ext=mp4]/best",
        "-o", str(target),
        "--no-progress", "--no-warnings",
        url,
    ], check=True)
    return target


def _norm_tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if t]


def _whisper_words(audio_path: Path) -> list[Any]:
    """Word-level transcript via the project's whisper wrapper."""
    from pipeline import beats as _beats
    return _beats.transcribe_words(audio_path)


def _score_window(
    moment_tokens: list[str],
    word_tokens: list[str],
    word_starts: list[float],
    word_ends: list[float],
    buildup_s: float = 1.5,
    tail_s: float = 1.2,
) -> tuple[float, float, float, str] | None:
    """Slide the moment phrase across word_tokens, return best (score, in_s, out_s, transcript_window).

    Score = 1 - (token misses / phrase length), penalized if the match starts
    at index 0 of the source (no buildup possible) or with < buildup_s of
    pre-roll.
    """
    n = len(moment_tokens)
    if n == 0 or len(word_tokens) < n:
        return None
    best: tuple[float, int, int] | None = None
    for i in range(len(word_tokens) - n + 1):
        window = word_tokens[i:i + n]
        misses = sum(1 for a, w in zip(moment_tokens, window) if a != w)
        score = 1.0 - misses / n
        if best is None or score > best[0]:
            best = (score, i, i + n - 1)
    if best is None or best[0] < 0.5:
        return None
    score, si, ei = best

    # Compute window with buildup + tail
    in_s = max(0.0, word_starts[si] - buildup_s)
    out_s = word_ends[ei] + tail_s

    # Transcript window for human review
    snippet_start = max(0, si - 5)
    snippet_end = min(len(word_tokens), ei + 6)
    transcript_window = " ".join(word_tokens[snippet_start:snippet_end])

    # Penalize matches with no breathing room before the phrase (cuts feel jumpy).
    actual_buildup = word_starts[si] - in_s
    if actual_buildup < buildup_s * 0.5:
        score *= 0.85

    return (score, in_s, out_s, transcript_window)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True, help="Free-text subject (used only in output JSON)")
    ap.add_argument("--urls", required=True,
                    help="Comma-separated YouTube URLs (full matches / highlights / extended highlights)")
    ap.add_argument("--moments", required=True,
                    help="Comma-separated moment phrases the narration will hit. "
                         "Use commentator-style phrasing for best whisper match.")
    ap.add_argument("--out", required=True, help="Output candidates JSON path")
    ap.add_argument("--buildup-s", type=float, default=1.5,
                    help="Seconds of pre-phrase buildup to include in each window")
    ap.add_argument("--tail-s", type=float, default=1.2,
                    help="Seconds of post-phrase tail to include in each window")
    ap.add_argument("--top-k", type=int, default=3,
                    help="Per moment, keep the top-K windows across all sources")
    ap.add_argument("--sources-dir", default=None,
                    help="Override download dir (default: sportstoriesanimated/footage/sources)")
    args = ap.parse_args()

    urls = [u.strip() for u in args.urls.split(",") if u.strip()]
    moments = [m.strip() for m in args.moments.split(",") if m.strip()]
    if not urls or not moments:
        raise SystemExit("--urls and --moments must each have at least one entry")

    sources_dir = Path(args.sources_dir) if args.sources_dir else (
        REPO_ROOT / "sportstoriesanimated" / "footage" / "sources"
    )

    # 1. Download every source
    sources: list[tuple[str, Path]] = []
    for url in urls:
        try:
            p = _download(url, sources_dir)
            sources.append((url, p))
        except subprocess.CalledProcessError as e:
            print(f"[dl  ] FAIL {url}: {e}")
            continue

    if not sources:
        raise SystemExit("no sources downloaded successfully")

    # 2. Whisper each source once (cached on disk by transcribe_words via pipeline.beats)
    per_source_words: dict[str, tuple[list[str], list[float], list[float]]] = {}
    for url, path in sources:
        print(f"[asr ] whisper-aligning {path.name}…")
        try:
            words = _whisper_words(path)
        except Exception as e:
            print(f"[asr ] FAIL {path.name}: {e}")
            continue
        word_tokens: list[str] = []
        word_starts: list[float] = []
        word_ends: list[float] = []
        for w in words:
            tok = _norm_tokens(w.text)
            word_tokens.append(tok[0] if tok else "")
            word_starts.append(float(w.start))
            word_ends.append(float(w.end))
        per_source_words[url] = (word_tokens, word_starts, word_ends)

    # 3. Score each moment against each source, collect top-K
    candidates: list[dict[str, Any]] = []
    for moment in moments:
        moment_tokens = _norm_tokens(moment)
        all_matches: list[dict[str, Any]] = []
        for url, (wt, ws, we) in per_source_words.items():
            res = _score_window(moment_tokens, wt, ws, we,
                                buildup_s=args.buildup_s, tail_s=args.tail_s)
            if res is None:
                continue
            score, in_s, out_s, transcript = res
            all_matches.append({
                "url": url,
                "in_s": round(in_s, 2),
                "out_s": round(out_s, 2),
                "transcript_window": transcript,
                "score": round(score, 3),
                "buildup_secs": round(args.buildup_s, 2),
                "tail_secs": round(args.tail_s, 2),
            })
        all_matches.sort(key=lambda m: m["score"], reverse=True)
        candidates.append({
            "moment": moment,
            "matches": all_matches[:args.top_k],
        })

    # 4. Write
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "subject": args.subject,
        "sources_searched": [u for u, _ in sources],
        "candidates": candidates,
    }
    out_path.write_text(json.dumps(out, indent=2))

    matched = sum(1 for c in candidates if c["matches"])
    print(f"[done] {out_path} — {matched}/{len(moments)} moments matched")
    if matched < len(moments):
        unmatched = [c["moment"] for c in candidates if not c["matches"]]
        print(f"[warn] no matches for: {unmatched}")
        print(f"       hint: try alternate phrasings (commentator-style verbs), or "
              f"add more source URLs (extended highlights / multi-cam reuploads).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
