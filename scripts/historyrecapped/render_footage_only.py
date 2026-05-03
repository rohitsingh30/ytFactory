#!/usr/bin/env python3
"""Footage-only render path for historyrecapped (and any other channel
where ``render_style: footage_only`` is set in config.yaml).

Inputs:
    <channel>/narrations/<slug>.json — pre-authored narration script
    <channel>/shotlist/<slug>.json   — windows + source URLs (see below)
    <channel>/config.yaml            — channel config (TTS voice, closer panel, upload account)

Output:
    <channel>/shorts/<slug>.mp4

Pipeline (NO image-gen):
    1. TTS via channel YAML's tts_provider/tts_voice → cache/<slug>/narration.wav
    2. Whisper word timestamps + beat split → cache/<slug>/beats.json
    3. Per-word caption PNGs → cache/<slug>/word_NNNN.png
    4. yt-dlp source clip(s) → channel/footage/sources/<video_id>.mp4 (cached)
    5. ffmpeg trim each window with blurred-letterbox 9:16 → scratch/clip_NN.mp4
    6. ffmpeg concat → scratch/video.mp4
    7. ffmpeg mux narration + tpad to match narration length → scratch/video_with_audio.mp4
    8. Composite inline national-flag emojis with relevant words (re-using
       the per-word PNGs + emoji PNGs)
    9. ffmpeg overlay word captions with cut-aware clamping → shorts/<slug>.mp4
   10. Optional Stage 8 upload via pipeline.upload (gated on min_score).

Shotlist JSON:
    {
      "slug": "pointe-du-hoc-1944",
      "source_url": "https://www.youtube.com/watch?v=<id>",
      "windows": [
        {"in_s": 240.0, "out_s": 247.0, "match_text": "Pointe du Hoc"},
        {"in_s": 1296.0, "out_s": 1303.0, "match_text": "..."},
        ...
      ]
    }
    Each window is one ffmpeg trim. They concat end-to-end. Total
    window duration should be ≥ narration length so the audio doesn't
    extend past frozen black; the final window auto-pads via tpad if
    narration runs longer.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml

from pipeline import audio, beats as beats_mod, align, compose, footage as footage_mod  # noqa: E402

LETTERBOX_FILTER = (
    "[0:v]split=2[bg][fg];"
    "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
    "crop=1080:1920,gblur=sigma=24,eq=brightness=-0.15[bg_blur];"
    "[fg]scale=1080:-2[fg_scaled];"
    "[bg_blur][fg_scaled]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]"
)


# --- regen audio + beats + word PNGs (call once per slug) ----------------

def _regen_audio_caps(channel: str, slug: str, cfg: dict) -> tuple[Path, Path, list]:
    """Synthesize TTS, run whisper, write word PNGs. Returns (narration_path,
    beats_path, beat_list). Re-runs if any of the three outputs is missing.
    """
    cache = REPO_ROOT / channel / "cache" / slug
    cache.mkdir(parents=True, exist_ok=True)
    narr_path = cache / "narration.wav"
    beats_path = cache / "beats.json"

    narrations_json = REPO_ROOT / channel / "narrations" / f"{slug}.json"
    if not narrations_json.exists():
        # Try niche-nested location
        for p in (REPO_ROOT / channel).rglob(f"narrations/{slug}.json"):
            narrations_json = p
            break
    if not narrations_json.exists():
        raise FileNotFoundError(f"no narration for {slug} under {channel}/")

    narration_text = json.loads(narrations_json.read_text())["narration"]
    narration_text = audio.normalize_for_tts(narration_text)

    # 1. TTS (skip if cached)
    if not narr_path.exists():
        print(f"[1/5] TTS via {cfg['tts_provider']} voice={cfg['tts_voice']}…")
        t0 = time.time()
        audio.synthesize(
            narration_text,
            voice=cfg["tts_voice"],
            out_path=narr_path,
            speed=cfg.get("tts_speed", 1.0),
            provider=cfg["tts_provider"],
            language=cfg.get("tts_language", "en"),
        )
        print(f"     wrote {narr_path.name} in {time.time() - t0:.1f}s")
    else:
        print(f"[1/5] TTS cached: {narr_path.name}")

    # 2. ASR + beats (skip if cached)
    if not beats_path.exists():
        print(f"[2/5] whisper_mlx + beat split…")
        t0 = time.time()
        whisper_words = beats_mod.transcribe_words(narr_path, provider=cfg.get("asr_provider", "whisper_mlx"))
        source_aligned = align.align_source_to_whisper(narration_text, whisper_words)
        beat_list = beats_mod.split_into_beats(
            source_aligned,
            target_s=cfg.get("beat_target_s", 2.0),
            max_s=cfg.get("beat_max_s", 3.0),
        )
        beats_mod.save_beats(beat_list, beats_path)
        print(f"     {len(beat_list)} beats in {time.time() - t0:.1f}s")
    else:
        beat_list = beats_mod.load_beats(beats_path)
        print(f"[2/5] beats cached: {len(beat_list)} beats")

    # 3. Word caption PNGs (idempotent on file presence inside)
    print(f"[3/5] prerendering word PNGs…")
    n = compose.prerender_word_captions(beat_list, cache)
    print(f"     wrote/reused {n} word PNGs")
    return narr_path, beats_path, beat_list


# --- footage build (download + trim + concat + mux) ----------------------

def _build_silent_video(channel: str, slug: str, shotlist: dict, scratch: Path) -> Path:
    """Trim each window from the source video with the blurred-letterbox
    filter, concat into one silent mp4. Returns its path."""
    scratch.mkdir(parents=True, exist_ok=True)

    src_url = shotlist["source_url"]
    cache_dir = REPO_ROOT / channel / "footage" / "sources"
    src_path = footage_mod._download_source(src_url, cache_dir)

    clip_paths: list[Path] = []
    for i, w in enumerate(shotlist["windows"]):
        in_s = float(w["in_s"])
        out_s = float(w["out_s"])
        clip_path = scratch / f"clip_{i:02d}.mp4"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{in_s:.3f}", "-t", f"{out_s - in_s:.3f}",
            "-i", str(src_path),
            "-filter_complex", LETTERBOX_FILTER,
            "-map", "[vout]", "-an",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
            str(clip_path),
        ]
        print(f"[footage] trim {i}: {in_s:.1f}-{out_s:.1f}s → {clip_path.name}")
        subprocess.run(cmd, check=True)
        clip_paths.append(clip_path)

    concat_list = scratch / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p.name}'" for p in clip_paths) + "\n")

    silent = scratch / "video.mp4"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-an",
        "-movflags", "+faststart",
        str(silent),
    ]
    print(f"[footage] concat → {silent.name}")
    subprocess.run(cmd, check=True)
    return silent


def _scene_cuts(shotlist: dict) -> list[float]:
    """Cumulative scene cut times — used to clamp captions across edits."""
    cuts: list[float] = []
    t = 0.0
    for w in shotlist["windows"]:
        t += float(w["out_s"]) - float(w["in_s"])
        cuts.append(t)
    return cuts


# --- caption + flag overlay (the final ffmpeg pass) ----------------------

EMOJI_DIR = REPO_ROOT / "historyrecapped" / "emoji"
TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72"
EMOJI_CODEPOINTS = {
    "fr":      "1f1eb-1f1f7",
    "uk":      "1f1ec-1f1e7",
    "de":      "1f1e9-1f1ea",
    "us":      "1f1fa-1f1f8",
    "medal":   "1f396",
    "thumbs":  "1f44d",
    "bell":    "1f514",
}
WORD_TAG = {
    "france": "fr", "british": "uk", "britain": "uk", "dunkirk": "uk",
    "royal": "uk", "england": "uk", "raf": "uk", "london": "uk",
    "churchill": "uk", "commons": "uk", "rangers": "us", "ranger": "us",
    "american": "us", "americans": "us", "us": "us",
    "hitler": "de", "hitler's": "de", "luftwaffe": "de", "german": "de",
    "germans": "de", "wehrmacht": "de", "messerschmitt": "de",
    "few": "medal", "served": "medal",
    "like": "thumbs", "subscribe": "bell",
}


def _ensure_emoji(tag: str) -> Path:
    cp = EMOJI_CODEPOINTS[tag]
    EMOJI_DIR.mkdir(parents=True, exist_ok=True)
    p = EMOJI_DIR / f"{cp}.png"
    if not p.exists():
        import urllib.request
        url = f"{TWEMOJI_BASE}/{cp}.png"
        urllib.request.urlretrieve(url, p)
    return p


def _composited_word_pngs(cache: Path, beat_list: list, channel: str, slug: str) -> Path:
    """Composite flag emoji onto specific word PNGs; return dir of new PNGs."""
    from PIL import Image
    out_dir = REPO_ROOT / channel / "cache" / slug / "_flagged_words"
    out_dir.mkdir(parents=True, exist_ok=True)
    gi = -1
    for b in beat_list:
        for w in b.words:
            gi += 1
            text = (w.text or "").strip().rstrip(".,!?:;\"'").lower()
            tag = WORD_TAG.get(text)
            src = cache / f"word_{gi:04d}.png"
            dst = out_dir / f"word_{gi:04d}.png"
            if not src.exists():
                continue
            if tag is None:
                # Just copy as-is so the burn step finds every PNG.
                if not dst.exists():
                    dst.write_bytes(src.read_bytes())
                continue
            flag_png = _ensure_emoji(tag)
            word_img = Image.open(src).convert("RGBA")
            f_img = Image.open(flag_png).convert("RGBA")
            target_h = word_img.height - 20
            f_img = f_img.resize((int(f_img.width * target_h / f_img.height), target_h), Image.LANCZOS)
            gap = 16
            cw = f_img.width + gap + word_img.width
            ch = max(f_img.height, word_img.height)
            canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            canvas.paste(f_img, (0, (ch - f_img.height) // 2), f_img)
            canvas.paste(word_img, (f_img.width + gap, (ch - word_img.height) // 2), word_img)
            canvas.save(dst)
    return out_dir


def _burn_video(
    silent: Path, narration_path: Path, words_dir: Path, beat_list: list,
    scene_cuts: list[float], out_path: Path,
) -> None:
    """Final pass: tpad silent video to narration length, mux audio, overlay
    each word PNG at its [start, end] (clamped to next scene cut)."""
    import bisect
    out_path.parent.mkdir(parents=True, exist_ok=True)
    scratch = silent.parent

    # 1. Probe lengths
    def _dur(p: Path) -> float:
        r = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(p),
        ]).strip()
        return float(r)

    silent_dur = _dur(silent)
    narr_dur = _dur(narration_path)
    pad = max(0.0, narr_dur - silent_dur)
    base_video = scratch / "video_with_audio.mp4"
    print(f"[mux] silent={silent_dur:.2f}s narration={narr_dur:.2f}s pad={pad:.2f}s")
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(silent), "-i", str(narration_path),
        "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={pad:.3f}[vout]",
        "-map", "[vout]", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        str(base_video),
    ], check=True)

    # 2. Build word overlay schedule with cut-aware clamping
    SAFETY = 0.10
    words: list[tuple[int, float, float]] = []
    gi = -1
    for b in beat_list:
        for w in b.words:
            gi += 1
            text = (w.text or "").strip()
            if not text:
                continue
            words.append((gi, float(w.start), float(w.end)))

    chains: list[str] = []
    inputs: list[str] = ["-i", str(base_video)]
    cur = "[0:v]"
    fi = 0
    for j, (idx, st, en) in enumerate(words):
        png = words_dir / f"word_{idx:04d}.png"
        if not png.exists():
            continue
        # Hold each word until the next visible word starts
        if j + 1 < len(words):
            en = max(en, words[j + 1][1])
        # Clamp to next scene cut to prevent caption bleeding across edits
        ci = bisect.bisect_right(scene_cuts, st)
        if ci < len(scene_cuts):
            en = min(en, scene_cuts[ci] - SAFETY)
        en = min(en, narr_dur)
        if en - st < 0.1:
            en = st + 0.1
        inputs += ["-i", str(png)]
        out_lbl = f"[v{fi}]"
        chains.append(
            f"{cur}[{len(inputs)//2}:v]overlay=x=(W-w)/2:y=(H-h)/2:"
            f"enable='between(t,{st:.3f},{en:.3f})'{out_lbl}"
        )
        cur = out_lbl
        fi += 1

    print(f"[burn] {fi} captions onto video → {out_path.name}")
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *inputs,
        "-filter_complex", ";".join(chains),
        "-map", cur, "-map", "0:a?",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ], check=True)
    print(f"[done] {out_path}")


# --- main entry ----------------------------------------------------------

def render(channel: str, slug: str, *, do_upload: bool = False) -> Path:
    chan_dir = REPO_ROOT / channel
    cfg = yaml.safe_load((chan_dir / "config.yaml").read_text())
    shotlist_path = chan_dir / "shotlist" / f"{slug}.json"
    if not shotlist_path.exists():
        raise FileNotFoundError(
            f"No shotlist for {slug}. Author "
            f"{shotlist_path.relative_to(REPO_ROOT)} with source_url + windows."
        )
    shotlist = json.loads(shotlist_path.read_text())

    cache = chan_dir / "cache" / slug
    scratch = chan_dir / "scratch" / slug

    narration_path, _, beat_list = _regen_audio_caps(channel, slug, cfg)
    silent = _build_silent_video(channel, slug, shotlist, scratch)
    words_dir = _composited_word_pngs(cache, beat_list, channel, slug)
    out = chan_dir / "shorts" / f"{slug}.mp4"
    _burn_video(silent, narration_path, words_dir, beat_list, _scene_cuts(shotlist), out)

    if do_upload:
        from pipeline import upload as up_mod
        raw_path = chan_dir / "raw" / f"{slug}.json"
        narr_json = chan_dir / "narrations" / f"{slug}.json"
        if not narr_json.exists():
            for p in chan_dir.rglob(f"narrations/{slug}.json"):
                narr_json = p
                break
        script = json.loads(narr_json.read_text())
        raw = json.loads(raw_path.read_text()) if raw_path.exists() else None
        record = up_mod.upload_short(
            project_root=REPO_ROOT,
            channel_yaml=cfg,
            channel_dir=channel,
            slug=slug,
            mp4_path=out,
            script=script,
            raw=raw,
            privacy_override=(cfg.get("upload") or {}).get("privacy", "public"),
            skip_critic=True,  # 100%-footage path doesn't run pipeline.critic
        )
        print(f"[upload] ✓ {record.get('url')}")

    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--channel", required=True, help="Channel slug (e.g. historyrecapped)")
    ap.add_argument("--slug", required=True, help="Story slug (e.g. pointe-du-hoc-1944)")
    ap.add_argument("--upload", action="store_true", help="Upload to YouTube on success")
    args = ap.parse_args()

    # Source .env for CARTESIA_API_KEY etc.
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    render(args.channel, args.slug, do_upload=args.upload)


if __name__ == "__main__":
    main()
