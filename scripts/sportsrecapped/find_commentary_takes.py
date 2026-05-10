#!/usr/bin/env python3
"""find_commentary_takes — score commentator/YouTuber soundbites.

Sibling of find_match_clips.py — same search pattern but tuned for podcast /
YouTube-essay clips where pundits, podcasters, or other YouTubers give
SPICY takes on the subject. These are the talking-head clips that supply
the "spicy why" in /make-sports-doc.

Differences from match-clip search:
  * `--takes` instead of `--moments` — phrases like "Argentina deserved it",
    "Mbappé carried France", "Lloris should have saved the third".
  * `--speakers` lets you preferentially pick clips where the speaker name
    appears in the source title or description (logged in candidates JSON
    for the caller to filter on).
  * Window default: -1.0s buildup / +0.8s tail (talking-heads start mid-
    sentence on cuts; less pre-roll needed than match cuts).
  * Score boosts windows that contain emotion-loaded tokens (`should`,
    `never`, `disgrace`, `genius`, `criminal`, `disrespect`, `robbed`,
    `disgusting`, `incredible`, `magic`) — the spice signal.

Output JSON:
    {
      "subject": "...",
      "candidates": [
        {
          "take": "Mbappé carried France",
          "matches": [
            {
              "url": "...",
              "in_s": 78.4, "out_s": 91.2,
              "speaker_hint": "Tifo Football",        # parsed from URL/title
              "spice_score": 0.21,                     # how many emotion tokens hit
              "transcript_window": "...",
              "score": 0.88
            }
          ]
        }
      ]
    }

Usage:
    .venv/bin/python sportstoriesanimated/scripts/find_commentary_takes.py \\
        --subject "Mbappé 2022 World Cup Final" \\
        --urls "https://www.youtube.com/watch?v=...,..." \\
        --takes "Mbappé carried France,Argentina deserved it,Lloris errors" \\
        --speakers "Tifo Football,Mark Goldbridge,Sky Sports,Peter Drury" \\
        --out sportstoriesanimated/footage_plan/_candidates_commentary.json
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


_SPICE_TOKENS = {
    "should", "shouldnt", "never", "always", "disgrace", "disgraceful",
    "genius", "criminal", "disrespect", "robbed", "disgusting", "incredible",
    "magic", "monumental", "legendary", "joke", "embarrassing", "deserved",
    "undeserved", "scandal", "myth", "overrated", "underrated", "outrageous",
    "shameful", "phenomenal", "ridiculous", "absurd", "carried", "bottled",
}


def _slug_from_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _yt_metadata(url: str) -> dict[str, str]:
    """Best-effort parse of yt-dlp metadata for speaker hint matching."""
    try:
        out = subprocess.check_output([
            "yt-dlp", "--print-json", "--skip-download", "--no-warnings", url,
        ], stderr=subprocess.DEVNULL).decode("utf-8")
        meta = json.loads(out.splitlines()[0])
        return {
            "title": str(meta.get("title", "")),
            "channel": str(meta.get("channel", "") or meta.get("uploader", "")),
            "description": (meta.get("description", "") or "")[:500],
        }
    except Exception:
        return {"title": "", "channel": "", "description": ""}


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
    from pipeline.audio import beats as _beats
    return _beats.transcribe_words(audio_path)


def _score_take(
    take_tokens: list[str],
    word_tokens: list[str],
    word_starts: list[float],
    word_ends: list[float],
    buildup_s: float,
    tail_s: float,
) -> tuple[float, float, float, str, float] | None:
    n = len(take_tokens)
    if n == 0 or len(word_tokens) < n:
        return None
    best: tuple[float, int, int] | None = None
    for i in range(len(word_tokens) - n + 1):
        window = word_tokens[i:i + n]
        misses = sum(1 for a, w in zip(take_tokens, window) if a != w)
        score = 1.0 - misses / n
        if best is None or score > best[0]:
            best = (score, i, i + n - 1)
    if best is None or best[0] < 0.45:
        return None
    score, si, ei = best
    # Wide window for spice scoring (pundits load emotion words around the take)
    spice_window_start = max(0, si - 8)
    spice_window_end = min(len(word_tokens), ei + 9)
    spice_window = word_tokens[spice_window_start:spice_window_end]
    spice_hits = sum(1 for t in spice_window if t in _SPICE_TOKENS)
    spice_score = min(1.0, spice_hits / 6.0)
    in_s = max(0.0, word_starts[si] - buildup_s)
    out_s = word_ends[ei] + tail_s
    transcript = " ".join(spice_window)
    return (score, in_s, out_s, transcript, spice_score)


def _speaker_hint(meta: dict, speakers: list[str]) -> str:
    """Match channel/title against the user's preferred speakers list."""
    if not speakers:
        return meta.get("channel") or ""
    haystack = (meta.get("channel", "") + " " + meta.get("title", "")).lower()
    for sp in speakers:
        if sp.lower() in haystack:
            return sp
    return meta.get("channel") or ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True)
    ap.add_argument("--urls", required=True, help="Comma-separated podcast/YouTube URLs")
    ap.add_argument("--takes", required=True,
                    help="Comma-separated take phrases (the spicy claim you want a soundbite of)")
    ap.add_argument("--speakers", default="",
                    help="Comma-separated preferred-speaker names (Tifo Football, Mark Goldbridge, etc.)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--buildup-s", type=float, default=1.0)
    ap.add_argument("--tail-s", type=float, default=0.8)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--sources-dir", default=None)
    args = ap.parse_args()

    urls = [u.strip() for u in args.urls.split(",") if u.strip()]
    takes = [t.strip() for t in args.takes.split(",") if t.strip()]
    speakers = [s.strip() for s in args.speakers.split(",") if s.strip()]
    if not urls or not takes:
        raise SystemExit("--urls and --takes must each have at least one entry")

    sources_dir = Path(args.sources_dir) if args.sources_dir else (
        REPO_ROOT / "sportstoriesanimated" / "footage" / "sources"
    )

    # 1. Download + parse metadata
    sources: list[tuple[str, Path, dict[str, str]]] = []
    for url in urls:
        meta = _yt_metadata(url)
        try:
            p = _download(url, sources_dir)
        except subprocess.CalledProcessError as e:
            print(f"[dl  ] FAIL {url}: {e}")
            continue
        sources.append((url, p, meta))

    if not sources:
        raise SystemExit("no sources downloaded")

    # 2. Whisper each
    per_source_words: dict[str, tuple[list[str], list[float], list[float]]] = {}
    for url, path, _ in sources:
        print(f"[asr ] whisper-aligning {path.name}…")
        try:
            words = _whisper_words(path)
        except Exception as e:
            print(f"[asr ] FAIL {path.name}: {e}")
            continue
        wt: list[str] = []
        ws: list[float] = []
        we: list[float] = []
        for w in words:
            tok = _norm_tokens(w.text)
            wt.append(tok[0] if tok else "")
            ws.append(float(w.start))
            we.append(float(w.end))
        per_source_words[url] = (wt, ws, we)

    # 3. Score
    candidates: list[dict[str, Any]] = []
    for take in takes:
        take_tokens = _norm_tokens(take)
        all_matches: list[dict[str, Any]] = []
        for url, _path, meta in sources:
            wb = per_source_words.get(url)
            if wb is None:
                continue
            wt, ws, we = wb
            res = _score_take(take_tokens, wt, ws, we, args.buildup_s, args.tail_s)
            if res is None:
                continue
            score, in_s, out_s, transcript, spice = res
            speaker_hint = _speaker_hint(meta, speakers)
            all_matches.append({
                "url": url,
                "in_s": round(in_s, 2),
                "out_s": round(out_s, 2),
                "speaker_hint": speaker_hint,
                "transcript_window": transcript,
                "score": round(score, 3),
                "spice_score": round(spice, 3),
                "buildup_secs": args.buildup_s,
                "tail_secs": args.tail_s,
            })
        # Rank by combined score: phrase match × 0.7 + spice × 0.3
        all_matches.sort(key=lambda m: m["score"] * 0.7 + m["spice_score"] * 0.3, reverse=True)
        candidates.append({"take": take, "matches": all_matches[:args.top_k]})

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "subject": args.subject,
        "speakers_preferred": speakers,
        "sources_searched": [u for u, _, _ in sources],
        "candidates": candidates,
    }, indent=2))

    matched = sum(1 for c in candidates if c["matches"])
    print(f"[done] {out_path} — {matched}/{len(takes)} takes matched")
    if matched < len(takes):
        unmatched = [c["take"] for c in candidates if not c["matches"]]
        print(f"[warn] no matches for: {unmatched}")
        print("       hint: pundit phrasings vary; try shorter substrings "
              "('carried France' vs 'Mbappé carried France'), or feed more podcast URLs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
