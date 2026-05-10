#!/usr/bin/env python3
"""Final v2 assembly:
- Re-mux the cached silent 100%-footage video with the NEW narration.wav.
- Composite national flags inline with relevant caption words.
- Burn captions onto the video, clamping each word's display so it does NOT
  bleed across a scene cut (fixes the cross-scene overlap the user flagged).
"""
import json, subprocess, bisect
from pathlib import Path
from PIL import Image

ROOT = Path("/Users/rohit/ytFactory")
CHAN = ROOT / "historyrecapped"
CACHE = CHAN / "cache/battle-of-britain-few"
EMOJI_DIR = CHAN / "emoji"
WORK = EMOJI_DIR / "inline_words_v2"
WORK.mkdir(parents=True, exist_ok=True)

SILENT = CHAN / "scratch/video.mp4"
NARR = CACHE / "narration.wav"
TMP_VIDEO = CHAN / "scratch/video_v2_audio.mp4"
VIDEO_OUT = CHAN / "shorts/battle-of-britain-few-100footage-v2.mp4"

# Scene cut times — set when historyrecapped/scripts/build_100footage.sh
# stitched the 7 trims at 7s + 7s + 8s + 8s + 8s + 6s + 6s. Cumulative cuts:
SCENE_CUTS = [7.0, 14.0, 22.0, 30.0, 38.0, 44.0, 57.0]

FLAG_FR = EMOJI_DIR / "1f1eb-1f1f7.png"
FLAG_UK = EMOJI_DIR / "1f1ec-1f1e7.png"
FLAG_DE = EMOJI_DIR / "1f1e9-1f1ea.png"
MEDAL = EMOJI_DIR / "1f396.png"
BELL = EMOJI_DIR / "1f514.png"
THUMBS = EMOJI_DIR / "1f44d.png"

WORD_FLAG = {
    "france":      FLAG_FR,
    "british":     FLAG_UK,
    "dunkirk":     FLAG_UK,
    "britain":     FLAG_UK,
    "royal":       FLAG_UK,
    "england":     FLAG_UK,
    "raf":         FLAG_UK,
    "london":      FLAG_UK,
    "churchill":   FLAG_UK,
    "commons":     FLAG_UK,
    "hitler":      FLAG_DE,
    "hitler's":    FLAG_DE,
    "luftwaffe":   FLAG_DE,
    "german":      FLAG_DE,
    "few":         MEDAL,
    "like":        THUMBS,
    "subscribe":   BELL,
}

def strip(text: str) -> str:
    return text.strip().rstrip(".,!?:;\"'").lower()

# ---- Step 1: re-mux narration onto the silent video --------------------
# Narration may be longer than the footage timeline (closing question + tail
# silence); tpad the video by freezing the last frame so the audio plays in
# full. Without this the closing question gets cut off.
print("[1] muxing new narration onto silent video (tpad if needed)…")
NARR_DUR = float(subprocess.check_output([
    "ffprobe", "-v", "error", "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1", str(NARR)
]).strip())
SILENT_DUR = float(subprocess.check_output([
    "ffprobe", "-v", "error", "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1", str(SILENT)
]).strip())
print(f"    silent={SILENT_DUR:.2f}s  narration={NARR_DUR:.2f}s")
pad_needed = max(0.0, NARR_DUR - SILENT_DUR)
subprocess.run([
    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
    "-i", str(SILENT), "-i", str(NARR),
    "-filter_complex",
    f"[0:v]tpad=stop_mode=clone:stop_duration={pad_needed:.3f}[vout]",
    "-map", "[vout]", "-map", "1:a",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
    "-c:a", "aac", "-b:a", "192k",
    "-shortest",
    "-movflags", "+faststart",
    str(TMP_VIDEO),
], check=True)
NARR_DUR = float(subprocess.check_output([
    "ffprobe", "-v", "error", "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1", str(TMP_VIDEO)
]).strip())
print(f"    final video duration={NARR_DUR:.2f}s")

# ---- Step 2: build flagged word PNGs ------------------------------------
print("[2] compositing inline flag PNGs…")
beats = json.loads((CACHE / "beats.json").read_text())

# Per-word: (global_idx, start, end, text, beat_idx)
words: list[tuple[int, float, float, str, int]] = []
for bi, b in enumerate(beats):
    for w in b.get("words") or []:
        text = (w.get("text") or "").strip()
        if not text:
            continue
        words.append((len(words), float(w["start"]), float(w["end"]), text, bi))
print(f"    {len(words)} words across {len(beats)} beats")

def emoji_at_height(src: Path, target_h: int) -> Image.Image:
    img = Image.open(src).convert("RGBA")
    w = int(img.width * target_h / img.height)
    return img.resize((w, target_h), Image.LANCZOS)

GAP_PX = 16
n_flagged = 0
for gi, _st, _en, text, _bi in words:
    src_png = CACHE / f"word_{gi:04d}.png"
    out_png = WORK / f"word_{gi:04d}.png"
    if not src_png.exists():
        continue
    flag = WORD_FLAG.get(strip(text))
    if flag is None:
        out_png.write_bytes(src_png.read_bytes())
        continue
    word_img = Image.open(src_png).convert("RGBA")
    flag_img = emoji_at_height(flag, word_img.height - 20)
    cw = flag_img.width + GAP_PX + word_img.width
    ch = max(flag_img.height, word_img.height)
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    canvas.paste(flag_img, (0, (ch - flag_img.height) // 2), flag_img)
    canvas.paste(word_img, (flag_img.width + GAP_PX, (ch - word_img.height) // 2), word_img)
    canvas.save(out_png)
    n_flagged += 1
print(f"    {n_flagged} words flagged")

# ---- Step 3: caption schedule with cut-aware clamping -------------------
# For each word, compute display window:
#   start = word.start
#   end_naive = next_word.start  (or word.end + 0.3s for the last word)
#   end_clamped = min(end_naive, NARR_DUR, next scene cut > word.start)
# The next-scene-cut clamp is what fixes the overlap.
SAFETY = 0.10  # 100ms gap before the cut so the caption fully disappears
print("[3] building caption overlay schedule (cut-aware)…")
schedule: list[tuple[int, float, float]] = []
for gi, st, en, _text, _bi in words:
    # Naive display end: hold until next word starts (smooth read)
    if gi + 1 < len(words):
        end_naive = words[gi + 1][1]
    else:
        end_naive = min(en + 0.30, NARR_DUR)
    # Find the first scene cut strictly AFTER this word's start
    next_cut_idx = bisect.bisect_right(SCENE_CUTS, st)
    if next_cut_idx < len(SCENE_CUTS):
        cut = SCENE_CUTS[next_cut_idx] - SAFETY
        end_naive = min(end_naive, cut)
    end_naive = min(end_naive, NARR_DUR)
    # Don't go negative or below display floor
    if end_naive - st < 0.10:
        end_naive = st + 0.10
    schedule.append((gi, st, end_naive))

# ---- Step 4: ffmpeg overlay chain ---------------------------------------
inputs: list[str] = ["-i", str(TMP_VIDEO)]
chains: list[str] = []
cur = "[0:v]"
filter_idx = 0
input_pos = 1
for gi, st, en in schedule:
    p = WORK / f"word_{gi:04d}.png"
    if not p.exists():
        continue
    inputs += ["-i", str(p)]
    out_label = f"[v{filter_idx}]"
    chains.append(
        f"{cur}[{input_pos}:v]overlay=x=(W-w)/2:y=(H-h)/2:"
        f"enable='between(t,{st:.3f},{en:.3f})'{out_label}"
    )
    cur = out_label
    filter_idx += 1
    input_pos += 1

filter_complex = ";".join(chains)
print(f"[4] burning {filter_idx} captions onto video…")
res = subprocess.run([
    "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
    *inputs,
    "-filter_complex", filter_complex,
    "-map", cur,
    "-map", "0:a?",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
    "-c:a", "copy",
    "-movflags", "+faststart",
    str(VIDEO_OUT),
], capture_output=True, text=True)
if res.returncode != 0:
    print("[ffmpeg STDERR (last 2000 chars)]")
    print(res.stderr[-2000:])
    raise SystemExit(res.returncode)
print(f"[done] {VIDEO_OUT}")
subprocess.run(["ls", "-lh", str(VIDEO_OUT)])
