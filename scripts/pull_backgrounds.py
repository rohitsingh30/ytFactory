"""Pull silent vertical 1080x1920 cooking-style background loops.

Used by channels whose visual format is "Reddit story text overlay on
cooking footage" — the background is a face-free, talking-free,
satisfying-cooking loop.

Two entry points:

    .venv/bin/python scripts/pull_backgrounds.py --query "asmr cooking no talking no face"
    .venv/bin/python scripts/pull_backgrounds.py --url <youtube-url>

Both download a long-form YouTube video to ``data/cache/backgrounds/``
(cached, so re-runs don't re-download), then ffmpeg-slice it into
``--num-clips`` x ``--clip-len`` second 9:16 clips dropped into
``mystoriesanimated/cooking_loops/``. With ``--check-faces`` each candidate clip
is sampled at a few frames and rejected if OpenCV detects a face — a
safety net against editor cutaways to a host's face.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from yt_dlp import YoutubeDL


# ---------------- yt-dlp helpers ----------------

def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:60] or "video"


def search_candidates(query: str, n: int, min_duration_s: int) -> list[dict]:
    """Return ranked list of YouTube search hits (flat metadata)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    entries = [e for e in (info.get("entries") or []) if e]
    # flat search returns duration on most entries; some may be None
    keep = []
    for e in entries:
        dur = e.get("duration") or 0
        if dur >= min_duration_s:
            keep.append(e)
    keep.sort(key=lambda e: (e.get("view_count") or 0), reverse=True)
    return keep


def download_video(url_or_id: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    # cache by video id; yt-dlp resolves id from url
    opts_probe = {"quiet": True, "no_warnings": True, "skip_download": True}
    with YoutubeDL(opts_probe) as ydl:
        meta = ydl.extract_info(url_or_id, download=False)
    vid = meta["id"]
    out_path = cache_dir / f"{vid}.mp4"
    if out_path.exists():
        print(f"[cache] {out_path} (skip download)")
        return out_path
    opts_dl = {
        "quiet": True,
        "no_warnings": True,
        "format": "bv*[height<=1920][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(cache_dir / "%(id)s.%(ext)s"),
    }
    print(f"[download] {meta.get('title','?')} ({vid}) -> {out_path}")
    with YoutubeDL(opts_dl) as ydl:
        ydl.download([url_or_id])
    if not out_path.exists():
        # yt-dlp may have produced .mkv/.webm if mp4 wasn't available
        for p in cache_dir.glob(f"{vid}.*"):
            return p
        raise RuntimeError(f"download produced no file for {vid}")
    return out_path


# ---------------- ffmpeg helpers ----------------

def probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "default=nw=1:nk=1", str(path),
        ],
        text=True,
    )
    return float(out.strip())


def slice_to_vertical(src: Path, start_s: float, length_s: float, dest: Path) -> None:
    # scale to fill 9:16, then center-crop, drop audio.
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start_s:.3f}",
        "-i", str(src),
        "-t", f"{length_s:.3f}",
        "-vf", vf,
        "-an",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(dest),
    ]
    subprocess.check_call(cmd)


# ---------------- face detection (optional) ----------------

def has_face(clip_path: Path, sample_frames: int = 6) -> bool:
    import cv2

    cap = cv2.VideoCapture(str(clip_path))
    if not cap.isOpened():
        return False
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total <= 0:
        cap.release()
        return False
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(cascade_path)
    indices = [int(total * (i + 1) / (sample_frames + 1)) for i in range(sample_frames)]
    found = False
    for ix in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, ix)
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5, minSize=(80, 80))
        if len(faces) > 0:
            found = True
            break
    cap.release()
    return found


# ---------------- orchestration ----------------

def pick_clip_starts(duration: float, num: int, clip_len: float, head_skip: float, tail_skip: float) -> list[float]:
    usable = duration - head_skip - tail_skip - clip_len
    if usable <= 0:
        return [head_skip] if duration > head_skip + clip_len else []
    if num <= 1:
        return [head_skip + usable / 2]
    step = usable / (num - 1)
    return [head_skip + i * step for i in range(num)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--query", help="YouTube search query")
    src.add_argument("--url", help="Specific YouTube video URL or id")

    ap.add_argument("--out", default="mystoriesanimated/cooking_loops", help="Where to drop the 1080x1920 mp4 loops")
    ap.add_argument("--cache", default="data/cache/backgrounds", help="Where to cache downloaded source videos")
    ap.add_argument("--clip-len", type=float, default=25.0, help="Seconds per clip")
    ap.add_argument("--num-clips", type=int, default=5, help="How many clips to extract from the source")
    ap.add_argument("--head-skip", type=float, default=15.0, help="Seconds to skip at the start (intro)")
    ap.add_argument("--tail-skip", type=float, default=20.0, help="Seconds to skip at the end (outro)")
    ap.add_argument("--min-duration", type=int, default=600, help="Minimum source-video length in seconds (search only)")
    ap.add_argument("--search-n", type=int, default=15, help="How many search candidates to consider")
    ap.add_argument("--check-faces", action="store_true", help="Reject clips where OpenCV detects a face")
    ap.add_argument("--max-face-rejects", type=int, default=10, help="Give up after this many face-rejects when sliding through the source")

    args = ap.parse_args()

    cache_dir = Path(args.cache)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.url:
        url = args.url
        print(f"[source] {url}")
    else:
        print(f"[search] ytsearch{args.search_n}:{args.query}  (min duration {args.min_duration}s)")
        cands = search_candidates(args.query, args.search_n, args.min_duration)
        if not cands:
            print("no candidates met the minimum duration; widen --search-n or lower --min-duration", file=sys.stderr)
            sys.exit(2)
        for i, c in enumerate(cands[:5]):
            print(f"  [{i}] {c.get('view_count','?'):>10}  {c.get('duration','?')}s  {c.get('title','?')[:80]}")
        pick = cands[0]
        url = pick.get("url") or f"https://www.youtube.com/watch?v={pick['id']}"
        print(f"[pick] {pick.get('title','?')}  ({pick.get('view_count','?')} views, {pick.get('duration')}s)")

    src_path = download_video(url, cache_dir)
    duration = probe_duration(src_path)
    print(f"[source duration] {duration:.1f}s")

    base_slug = _slugify(src_path.stem)
    starts = pick_clip_starts(duration, args.num_clips, args.clip_len, args.head_skip, args.tail_skip)
    if not starts:
        print("source too short for any clip; adjust head/tail skip", file=sys.stderr)
        sys.exit(3)

    written: list[Path] = []
    rejects = 0
    for ix, start in enumerate(starts):
        attempt = 0
        cur_start = start
        while True:
            dest = out_dir / f"{base_slug}_{ix:02d}.mp4"
            print(f"[clip {ix}] start={cur_start:.1f}s len={args.clip_len:.1f}s -> {dest}")
            slice_to_vertical(src_path, cur_start, args.clip_len, dest)
            if args.check_faces and has_face(dest):
                rejects += 1
                attempt += 1
                if rejects >= args.max_face_rejects:
                    print(f"[reject] face detected; abandoned after {rejects} total rejects")
                    dest.unlink(missing_ok=True)
                    break
                # nudge forward by clip_len and retry
                cur_start = cur_start + args.clip_len
                if cur_start + args.clip_len > duration - args.tail_skip:
                    print(f"[reject] face detected; ran out of room in source")
                    dest.unlink(missing_ok=True)
                    break
                print(f"[reject] face detected; sliding to {cur_start:.1f}s and retrying")
                continue
            written.append(dest)
            break

    print()
    print(f"wrote {len(written)} clip(s) to {out_dir}/")
    for p in written:
        print(f"  - {p.name}")
    manifest = out_dir / "_manifest.json"
    existing = json.loads(manifest.read_text()) if manifest.exists() else []
    existing.append(
        {
            "source_url": url,
            "source_file": str(src_path),
            "clips": [str(p) for p in written],
            "clip_len_s": args.clip_len,
            "face_check": bool(args.check_faces),
        }
    )
    manifest.write_text(json.dumps(existing, indent=2))
    print(f"updated {manifest}")


if __name__ == "__main__":
    main()
