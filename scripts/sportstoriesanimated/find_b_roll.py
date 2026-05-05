#!/usr/bin/env python3
"""find_b_roll — collect stadium / fan / training / city b-roll for /make-sports-doc.

B-roll is the breathing room between match cuts and talking-head clips —
stadium exteriors, fan reactions, training drills, tunnel walks, city
skylines. Unlike match clips and commentary, b-roll has no narration_anchor
text to whisper-align against; it's selected by VISUAL kind + duration.

Given URLs and per-URL kind tags, this script:
    1. Downloads each URL.
    2. Probes duration.
    3. Suggests N short candidate windows (5-12s each, evenly spaced
       across the source) per kind, so the skill / user can quickly
       pick fillers that aren't all from the same 30s of the same clip.
    4. Optionally runs scene-cut detection (ffmpeg `scdet` filter) and
       prefers windows that begin within ~1s after a scene boundary —
       cuts that start mid-pan look choppy.

Output:
    {
      "subject": "...",
      "candidates": [
        {
          "kind": "stadium_ext",
          "windows": [
            {"url": "...", "in_s": 0.0, "out_s": 6.0, "scene_starts": [0.0, 8.4, 14.2]}
          ]
        }
      ]
    }

Usage:
    .venv/bin/python scripts/sportstoriesanimated/find_b_roll.py \\
        --subject "Lusail Stadium Doha" \\
        --kinds stadium_ext,fans,city_skyline \\
        --urls "https://...stadium_walkthrough,https://...fans_reaction,https://...doha_skyline" \\
        --out sportstoriesanimated/footage_plan/_candidates_broll.json

Note: --urls and --kinds must be the same length and aligned by index
(URL 1 is kind[0], URL 2 is kind[1], …). For multiple URLs of the same
kind, repeat the kind in --kinds.
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

VALID_KINDS = {
    "stadium_ext", "stadium_int", "fans", "training", "celebration",
    "city_skyline", "tunnel_walk", "press_conf", "matchday_walkup",
    "warmup", "trophy", "fan_protest",
}


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


def _probe_duration(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]).decode().strip()
    return float(out)


def _scene_starts(path: Path, threshold: float = 0.4, max_secs: float = 600.0) -> list[float]:
    """Run ffmpeg scdet to get scene-cut timestamps. Capped at max_secs scan."""
    try:
        proc = subprocess.run([
            "ffmpeg", "-hide_banner", "-i", str(path),
            "-t", f"{max_secs}",
            "-filter:v", f"select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-",
        ], capture_output=True, check=False)
        text = proc.stderr.decode(errors="ignore")
    except Exception:
        return []
    starts = []
    for m in re.finditer(r"pts_time:([\d\.]+)", text):
        try:
            starts.append(float(m.group(1)))
        except ValueError:
            continue
    return starts


def _evenly_spaced_windows(
    duration: float,
    n: int,
    win_dur: float,
    edge_margin: float,
    scene_starts: list[float],
) -> list[tuple[float, float]]:
    """Return n (in_s, out_s) tuples evenly spaced across [edge_margin, duration-edge_margin].

    If scene_starts is non-empty, snap each candidate's in_s to the nearest
    scene boundary within ±2s — cuts feel cleaner from a fresh shot.
    """
    if duration <= edge_margin * 2 + win_dur:
        # Source is too short — return one centered window.
        return [(max(0.0, (duration - win_dur) / 2.0), min(duration, (duration + win_dur) / 2.0))]
    span_lo = edge_margin
    span_hi = duration - edge_margin - win_dur
    if n == 1:
        positions = [(span_lo + span_hi) / 2]
    else:
        step = (span_hi - span_lo) / (n - 1)
        positions = [span_lo + i * step for i in range(n)]

    windows: list[tuple[float, float]] = []
    for pos in positions:
        if scene_starts:
            best = min(scene_starts, key=lambda s: abs(s - pos))
            if abs(best - pos) <= 2.0:
                pos = best
        in_s = max(0.0, pos)
        out_s = min(duration, in_s + win_dur)
        windows.append((round(in_s, 2), round(out_s, 2)))
    return windows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True)
    ap.add_argument("--urls", required=True, help="Comma-separated source URLs")
    ap.add_argument("--kinds", required=True,
                    help=f"Comma-separated kind tags, one per URL. Valid: {sorted(VALID_KINDS)}")
    ap.add_argument("--out", required=True)
    ap.add_argument("--win-dur-s", type=float, default=6.0,
                    help="Default window duration per b-roll cut")
    ap.add_argument("--per-source", type=int, default=4,
                    help="Number of evenly-spaced candidate windows per source URL")
    ap.add_argument("--edge-margin-s", type=float, default=2.0,
                    help="Skip the first/last N seconds of each source (intro/outro graphics)")
    ap.add_argument("--no-scene-snap", action="store_true",
                    help="Disable scdet scene-cut snapping (faster, slightly worse cut points)")
    ap.add_argument("--sources-dir", default=None)
    args = ap.parse_args()

    urls = [u.strip() for u in args.urls.split(",") if u.strip()]
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    if len(urls) != len(kinds):
        raise SystemExit(f"--urls ({len(urls)}) and --kinds ({len(kinds)}) must be the same length")
    bad = [k for k in kinds if k not in VALID_KINDS]
    if bad:
        raise SystemExit(f"unknown kinds: {bad}. Valid: {sorted(VALID_KINDS)}")

    sources_dir = Path(args.sources_dir) if args.sources_dir else (
        REPO_ROOT / "sportstoriesanimated" / "footage" / "sources"
    )

    # Group by kind
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for url, kind in zip(urls, kinds):
        try:
            path = _download(url, sources_dir)
        except subprocess.CalledProcessError as e:
            print(f"[dl  ] FAIL {url}: {e}")
            continue
        try:
            dur = _probe_duration(path)
        except Exception as e:
            print(f"[probe] FAIL {path.name}: {e}")
            continue
        scenes = [] if args.no_scene_snap else _scene_starts(path)
        wins = _evenly_spaced_windows(
            duration=dur,
            n=args.per_source,
            win_dur=args.win_dur_s,
            edge_margin=args.edge_margin_s,
            scene_starts=scenes,
        )
        for in_s, out_s in wins:
            by_kind.setdefault(kind, []).append({
                "url": url,
                "in_s": in_s,
                "out_s": out_s,
                "scene_starts_nearby": [s for s in scenes if abs(s - in_s) <= 5.0][:5],
                "source_duration_s": round(dur, 2),
            })

    candidates = [{"kind": kind, "windows": wins} for kind, wins in by_kind.items()]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "subject": args.subject,
        "candidates": candidates,
    }, indent=2))

    total_wins = sum(len(c["windows"]) for c in candidates)
    print(f"[done] {out_path} — {total_wins} windows across {len(candidates)} kinds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
