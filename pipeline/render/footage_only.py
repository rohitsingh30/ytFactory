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
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Repo root = parent.parent of pipeline/render/footage_only.py.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

import yaml

from pipeline import audio, beats as beats_mod, align, compose, footage as footage_mod  # noqa: E402

# Two aspect-specific blurred-letterbox filters. Selection is driven by
# shotlist["aspect"] in _build_silent_video below — defaults to 9:16
# (Shorts) when the field is absent, preserving prior behaviour for
# every existing footage_only Shorts shotlist.
LETTERBOX_FILTER_9_16 = (
    "[0:v]split=2[bg][fg];"
    "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
    "crop=1080:1920,gblur=sigma=24,eq=brightness=-0.15[bg_blur];"
    "[fg]scale=1080:-2[fg_scaled];"
    "[bg_blur][fg_scaled]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]"
)
LETTERBOX_FILTER_16_9 = (
    "[0:v]split=2[bg][fg];"
    "[bg]scale=1920:1080:force_original_aspect_ratio=increase,"
    "crop=1920:1080,gblur=sigma=24,eq=brightness=-0.15[bg_blur];"
    "[fg]scale=-2:1080[fg_scaled];"
    "[bg_blur][fg_scaled]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]"
)
LETTERBOX_FILTERS = {"9:16": LETTERBOX_FILTER_9_16, "16:9": LETTERBOX_FILTER_16_9}
# Back-compat alias — older callers / tests imported LETTERBOX_FILTER directly.
LETTERBOX_FILTER = LETTERBOX_FILTER_9_16

ASPECT_DIMS: dict[str, tuple[int, int]] = {"9:16": (1080, 1920), "16:9": (1920, 1080)}


def _ken_burns_filter(aspect: str, duration_s: float, fps: int = 30) -> str:
    """Composite one still image onto a blurred-letterbox background of the
    same image. Used for image-only windows (Wikimedia stills / manuscript
    scans / museum open-access).

    Speed-tuned 2026-05-05: previously used a `zoompan` motion filter
    which is pathologically slow on looped stills (a single 10s
    portrait clip took 30+ minutes to encode at full HD on M2 Max).
    Replaced with a static composite — no on-clip zoom motion, but the
    cuts between beats provide enough motion for a ~5-15s/beat cadence,
    and the cosmosdecoded long-form prep pipeline assumed this anyway."""
    w, h = ASPECT_DIMS[aspect]
    return (
        "[0:v]split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},gblur=sigma=24,eq=brightness=-0.15[bg_blur];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fg_scaled];"
        "[bg_blur][fg_scaled]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]"
    )


# --- per-window asset resolution (Wikimedia / archive.org / direct media) ---

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".m4v")


_WIKIMEDIA_FILE_RE = re.compile(r"/wiki/(?:File|Image)(?::|%3A)(.+)$", re.IGNORECASE)


def _looks_like_image(url: str) -> bool:
    p = url.split("?")[0].lower()
    if p.endswith(_IMAGE_EXTS):
        return True
    m = _WIKIMEDIA_FILE_RE.search(url)
    if m:
        ext = "." + m.group(1).rsplit(".", 1)[-1].lower()
        return ext in _IMAGE_EXTS
    return False


def _looks_like_video(url: str) -> bool:
    return url.split("?")[0].lower().endswith(_VIDEO_EXTS)


def _is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _safe_filename(name: str, fallback_seed: str = "") -> str:
    name = urllib.parse.unquote(name)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name or name in ("_", "."):
        name = hashlib.sha1(fallback_seed.encode()).hexdigest()[:16]
    return name[:200]


def _wikimedia_filename(page_url: str) -> str:
    m = _WIKIMEDIA_FILE_RE.search(page_url)
    if not m:
        raise ValueError(f"not a Wikimedia File: URL: {page_url}")
    return urllib.parse.unquote(m.group(1))


def _resolve_wikimedia_file_url(page_url: str) -> str:
    fname = _wikimedia_filename(page_url)
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{urllib.parse.quote(fname)}"


def _resolve_archive_org_details(details_url: str) -> str:
    """Resolve archive.org/details/<id> → direct download URL of the largest
    .mp4 derivative via the public metadata API."""
    m = re.search(r"archive\.org/details/([^/?#]+)", details_url)
    if not m:
        raise ValueError(f"not an archive.org details URL: {details_url}")
    item = m.group(1)
    meta_url = f"https://archive.org/metadata/{item}"
    with urllib.request.urlopen(meta_url, timeout=30) as r:
        meta = json.loads(r.read())
    files = meta.get("files", [])
    candidates = [f for f in files if f.get("name", "").lower().endswith(".mp4")]
    if not candidates:
        raise RuntimeError(
            f"archive.org item '{item}' has no .mp4 derivative; pick a "
            f"different item or use a direct archive.org/download/<id>/<file> URL."
        )
    candidates.sort(key=lambda f: int(f.get("size", 0) or 0), reverse=True)
    return f"https://archive.org/download/{item}/{urllib.parse.quote(candidates[0]['name'])}"


def _urlretrieve(url: str, dest: Path) -> None:
    """urllib download with a UA header (Wikimedia rejects default UA)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "ytFactory-render/1.0 (+https://github.com/anthropics/claude-code)"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            out.write(chunk)


def _resolve_asset(url: str, cache_dir: Path) -> tuple[Path, str]:
    """Download `url` into cache_dir. Returns (path, kind ∈ {"image","video"}).

    Supported sources (no auth required):
      - YouTube                                → existing yt-dlp path
      - Wikimedia Commons File:/Image: pages   → Special:FilePath redirect
      - archive.org/details/<id>               → metadata API → largest mp4
      - archive.org/download/<id>/<file>       → direct
      - Direct media URL (.mp4/.jpg/.png/...)  → direct

    Pexels / Unsplash / Shutterstock page URLs are NOT resolvable here —
    swap to a direct asset URL in the shotlist before rendering.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    if _is_youtube(url):
        return footage_mod._download_source(url, cache_dir), "video"

    if "wikimedia.org/wiki/" in url and _WIKIMEDIA_FILE_RE.search(url):
        fname = _safe_filename(_wikimedia_filename(url), fallback_seed=url)
        dest = cache_dir / fname
        if dest.exists() and dest.stat().st_size > 1024:
            return dest, "image"
        resolved = _resolve_wikimedia_file_url(url)
        print(f"[footage] wikimedia → {fname}")
        _urlretrieve(resolved, dest)
        return dest, "image"

    if re.search(r"archive\.org/details/", url):
        resolved = _resolve_archive_org_details(url)
        fname = _safe_filename(Path(urllib.parse.urlparse(resolved).path).name, fallback_seed=url)
        dest = cache_dir / fname
        if dest.exists() and dest.stat().st_size > 1024:
            return dest, "video"
        print(f"[footage] archive.org → {fname}")
        _urlretrieve(resolved, dest)
        return dest, "video"

    if re.search(r"archive\.org/download/", url):
        fname = _safe_filename(Path(urllib.parse.urlparse(url).path).name, fallback_seed=url)
        dest = cache_dir / fname
        kind = "image" if _looks_like_image(url) else "video"
        if dest.exists() and dest.stat().st_size > 1024:
            return dest, kind
        print(f"[footage] archive.org direct → {fname}")
        _urlretrieve(url, dest)
        return dest, kind

    if _looks_like_image(url) or _looks_like_video(url):
        fname = _safe_filename(Path(urllib.parse.urlparse(url).path).name, fallback_seed=url)
        dest = cache_dir / fname
        kind = "image" if _looks_like_image(url) else "video"
        if dest.exists() and dest.stat().st_size > 1024:
            return dest, kind
        print(f"[footage] direct → {fname}")
        _urlretrieve(url, dest)
        return dest, kind

    raise RuntimeError(
        f"Unsupported source URL: {url!r}\n"
        f"Supported: YouTube, Wikimedia Commons File: pages, archive.org "
        f"details/ pages, archive.org/download/ direct URLs, direct "
        f".mp4/.jpg/.png/.webp/.webm URLs.\n"
        f"Pexels / Unsplash / etc. page URLs must be swapped to direct "
        f"asset URLs in the shotlist."
    )


# --- regen audio + beats + word PNGs (call once per slug) ----------------

def _regen_audio_caps(
    channel: str, slug: str, cfg: dict, *, caption_mode: str = "shorts",
) -> tuple[Path, Path | None, list]:
    """Synthesize TTS, optionally run whisper + word PNGs. Returns
    (narration_path, beats_path_or_None, beat_list).

    When ``caption_mode == "none"`` (long-form kathaa default) the Whisper
    + per-word PNG passes are skipped entirely — saves ~15 min on a
    70-min Hindi narration where Whisper undercounts dense Devanagari
    anyway (channel learning whisper_hindi_undercount.md)."""
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
            ref_audio_text=cfg.get("tts_ref_text"),
        )
        print(f"     wrote {narr_path.name} in {time.time() - t0:.1f}s")
    else:
        print(f"[1/5] TTS cached: {narr_path.name}")

    if caption_mode == "none":
        print(f"[2/5] caption_mode=none → skip whisper + beat split")
        print(f"[3/5] caption_mode=none → skip word PNG prerender")
        return narr_path, None, []

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

    aspect = shotlist.get("aspect", "9:16")
    if aspect not in LETTERBOX_FILTERS:
        raise ValueError(f"unsupported aspect {aspect!r}; expected one of {list(LETTERBOX_FILTERS)}")
    letterbox = LETTERBOX_FILTERS[aspect]

    # Asset resolution: top-level source_url is the legacy Shorts pattern
    # (one source video for every window). Per-window source_url is the
    # long-form kathaa pattern (each window from a distinct asset, possibly
    # mixing image stills + video clips). Both shapes are honoured here.
    cache_dir = REPO_ROOT / channel / "footage" / "sources"
    default_src_path: Path | None = None
    default_src_kind: str | None = None
    legacy_src_url = shotlist.get("source_url")
    if legacy_src_url:
        default_src_path, default_src_kind = _resolve_asset(legacy_src_url, cache_dir)

    # Pre-resolve every per-window source up front so we fail fast on bad
    # URLs (rather than mid-trim after some windows already succeed).
    resolved: dict[int, tuple[Path, str]] = {}
    for i, w in enumerate(shotlist["windows"]):
        win_src = w.get("source_url")
        if not win_src:
            continue
        try:
            resolved[i] = _resolve_asset(win_src, cache_dir)
        except Exception as exc:
            raise RuntimeError(
                f"window {i} ({(w.get('match_text') or '')[:50]!r}): "
                f"failed to resolve {win_src!r}: {exc}"
            ) from exc

    clip_paths: list[Path] = []
    pending_jobs: list = []
    n_windows = len(shotlist["windows"])
    for i, w in enumerate(shotlist["windows"]):
        in_s = float(w["in_s"])
        out_s = float(w["out_s"])
        if i in resolved:
            asset_path, asset_kind = resolved[i]
        elif default_src_path is not None:
            asset_path, asset_kind = default_src_path, default_src_kind  # type: ignore[assignment]
        else:
            raise ValueError(
                f"window {i}: no source_url at window level and no top-level "
                f"shotlist['source_url'] to fall back on"
            )

        clip_path = scratch / f"clip_{i:02d}.mp4"
        clip_paths.append(clip_path)
        if clip_path.exists() and clip_path.stat().st_size > 1024:
            continue

        if asset_kind == "image":
            duration = max(0.05, out_s - in_s)
            kb_filter = _ken_burns_filter(aspect, duration)

            def _job(asset=asset_path, dur=duration, clip=clip_path, idx=i, filt=kb_filter):
                cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-loop", "1", "-framerate", "30", "-t", f"{dur:.3f}",
                    "-i", str(asset),
                    "-filter_complex", filt,
                    "-map", "[vout]", "-an",
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
                    "-crf", "23", "-pix_fmt", "yuv420p", "-r", "30",
                    str(clip),
                ]
                print(f"[footage] image {idx}/{n_windows-1}: {dur:.1f}s ken-burns → {clip.name}")
                subprocess.run(cmd, check=True)

            pending_jobs.append(_job)
        else:
            # Aspect short-circuit: when src aspect matches output aspect (within
            # 1% tolerance), skip the blurred-letterbox gblur+overlay chain and
            # use a plain scale. The heavy filter on aspect-matched 1080p sources
            # OOMs the parallel-4 ffmpeg fanout (SIGKILL-9 on M2 Max with 4
            # concurrent libx264 + gblur sigma=24). Mirrors the
            # long_form_trim_aspect_short_circuit.md learning.
            target_w, target_h = (1920, 1080) if aspect == "16:9" else (1080, 1920)
            target_ratio = target_w / target_h
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "csv=p=0",
                     str(asset_path)],
                    capture_output=True, text=True, check=True,
                )
                sw, sh = (int(x) for x in probe.stdout.strip().split(","))
                src_ratio = sw / sh
                aspect_match = abs(src_ratio - target_ratio) / target_ratio < 0.01
            except Exception:
                aspect_match = False

            if aspect_match:
                vf = f"scale={target_w}:{target_h}:flags=lanczos,setsar=1"
                def _job(asset=asset_path, in_=in_s, out=out_s, clip=clip_path, idx=i, vf=vf):
                    cmd = [
                        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", f"{in_:.3f}", "-t", f"{out - in_:.3f}",
                        "-i", str(asset),
                        "-vf", vf, "-an",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-r", "30",
                        str(clip),
                    ]
                    print(f"[footage] trim {idx}/{n_windows-1}: {in_:.1f}-{out:.1f}s (passthrough) → {clip.name}")
                    subprocess.run(cmd, check=True)
            else:
                def _job(asset=asset_path, in_=in_s, out=out_s, clip=clip_path, idx=i):
                    cmd = [
                        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", f"{in_:.3f}", "-t", f"{out - in_:.3f}",
                        "-i", str(asset),
                        "-filter_complex", letterbox,
                        "-map", "[vout]", "-an",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
                        str(clip),
                    ]
                    print(f"[footage] trim {idx}/{n_windows-1}: {in_:.1f}-{out:.1f}s (letterbox) → {clip.name}")
                    subprocess.run(cmd, check=True)

            pending_jobs.append(_job)

    if pending_jobs:
        from pipeline.parallel import run_parallel
        run_parallel(pending_jobs, label="footage-trim")

    concat_list = scratch / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p.name}'" for p in clip_paths) + "\n")

    silent = scratch / "video.mp4"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-an",
        "-fflags", "+genpts",
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
    input_pos = 1   # base_video is input 0, PNGs start at 1
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
            f"{cur}[{input_pos}:v]overlay=x=(W-w)/2:y=(H-h)/2:"
            f"enable='between(t,{st:.3f},{en:.3f})'{out_lbl}"
        )
        cur = out_lbl
        input_pos += 1
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


def _mux_audio_no_captions(silent: Path, narration_path: Path, out_path: Path) -> None:
    """Long-form / kathaa path: tpad the silent concat to narration length
    and mux audio. No caption overlays, no emoji compositing — saves the
    word-PNG + per-word ffmpeg overlay pass entirely (the most expensive
    step on dense Hindi narration)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _dur(p: Path) -> float:
        r = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(p),
        ]).strip()
        return float(r)

    silent_dur = _dur(silent)
    narr_dur = _dur(narration_path)
    pad = max(0.0, narr_dur - silent_dur)
    print(f"[mux] silent={silent_dur:.2f}s narration={narr_dur:.2f}s pad={pad:.2f}s (no captions)")
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(silent), "-i", str(narration_path),
        "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={pad:.3f}[vout]",
        "-map", "[vout]", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ], check=True)
    print(f"[done] {out_path}")


# --- main entry ----------------------------------------------------------

def render(channel: str, slug: str, *, do_upload: bool = False, aspect_override: str | None = None) -> Path:
    chan_dir = REPO_ROOT / channel
    cfg = yaml.safe_load((chan_dir / "config.yaml").read_text())
    shotlist_path = chan_dir / "shotlist" / f"{slug}.json"
    if not shotlist_path.exists():
        raise FileNotFoundError(
            f"No shotlist for {slug}. Author "
            f"{shotlist_path.relative_to(REPO_ROOT)} with source_url + windows."
        )
    shotlist = json.loads(shotlist_path.read_text())
    if aspect_override:
        shotlist["aspect"] = aspect_override

    aspect = shotlist.get("aspect") or (cfg.get("kathaa") or {}).get("aspect") or "9:16"
    if aspect not in ASPECT_DIMS:
        raise ValueError(f"unsupported aspect {aspect!r}; must be 9:16 or 16:9")

    # caption_mode resolution: shotlist > kathaa block > shorts default.
    # 2026-05-05: post-mortem of top10-alien-abductions-202605 flipped
    # the 16:9 default from "none" → "shorts". Kathaa renders still opt
    # out by setting `cfg.kathaa.caption_mode = "none"` (their channel
    # config has the kathaa block; everything else doesn't). For
    # /make-top10 list-format long-form, captions are mandatory — see
    # .claude/skills/make-top10/learnings/wallpaper_mode_ban.md.
    caption_mode = (
        shotlist.get("caption_mode")
        or (cfg.get("kathaa") or {}).get("caption_mode")
        or "shorts"
    )

    cache = chan_dir / "cache" / slug
    scratch = chan_dir / "scratch" / slug

    print(f"[cfg] aspect={aspect} caption_mode={caption_mode} channel={channel} slug={slug}")

    narration_path, _, beat_list = _regen_audio_caps(channel, slug, cfg, caption_mode=caption_mode)
    silent = _build_silent_video(channel, slug, shotlist, scratch)

    out_dir = chan_dir / ("long_form" if aspect == "16:9" else "shorts")
    out = out_dir / f"{slug}.mp4"

    if caption_mode == "none":
        _mux_audio_no_captions(silent, narration_path, out)
    else:
        words_dir = _composited_word_pngs(cache, beat_list, channel, slug)
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
    ap.add_argument("--aspect", default=None, help="Override shotlist aspect (e.g. 16:9)")
    ap.add_argument("--upload", action="store_true", help="Upload to YouTube on success")
    args = ap.parse_args()

    # Source .env for any provider env vars (HF tokens, etc.).
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    render(args.channel, args.slug, do_upload=args.upload, aspect_override=args.aspect)


def cli_main() -> None:
    """CLI entry point. Invoked by ``scripts/historyrecapped/render_footage_only.py``."""
    main()


if __name__ == "__main__":
    cli_main()
