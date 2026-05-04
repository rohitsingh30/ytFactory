#!/usr/bin/env python3
"""Long-form sleep-history renderer (16:9, 60-120 min, soft narrator).

Inputs:
    historyrecapped/narrations/<slug>.json — sectioned narration + topic metadata
    historyrecapped/shotlist/<slug>.json   — clips: list of {source_file, in_s, out_s}
    historyrecapped/config.yaml            — channel TTS + atempo + music + ask cadence
                                              (long-form settings under `long_form:` block)

Output:
    historyrecapped/shorts/<slug>.mp4 (1920x1080, 30 fps, AAC 192k)

Pipeline (very different from the Shorts footage_only path):
    1. Chunked TTS — split narration into ~25-30s chunks, render each via
       Cartesia, post-process each with ffmpeg atempo (channel YAML
       tts_post_atempo) for true sleep-cadence. Concatenate with silence
       joiners → narration.wav.
    2. Trim each shotlist clip from its source mp4. 16:9 letterbox/scale to
       1920x1080. Long windows (60-300s each) are preferred — jarring cuts
       wake the viewer.
    3. Concat clips → video.mp4. If video duration < narration duration,
       extend the last clip with slow zoom-pan to fill.
    4. Music bed: loop a royalty-free ambient track to match narration
       duration with crossfade joiners.
    5. Periodic support-ask insertion: at config'd cadence (~1080 s = 18
       min), pause the main timeline, fade to a pre-rendered animated ask
       screen + soft-voice ask audio (music continues underneath), fade
       back to main.
    6. Final mux: video stream + (narration -6 dB + music -28 dB + ask
       audio in their slots) → 1080p mp4.

NOT done by this script (yet — track in TODOs):
    - support-ask animated screen build (PIL+ffmpeg one-time per channel)
    - music-bed sourcing (manual: drop a wav into historyrecapped/music/)

Usage:
    .venv/bin/python scripts/historyrecapped/render_long_form.py \
        --channel historyrecapped --slug pacific-war-1941-1942-sleep
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml


# ---------- env loading ----------------------------------------------------


def _load_env(repo_root: Path) -> None:
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------- chunked Cartesia TTS ------------------------------------------


_CARTESIA_URL = "https://api.cartesia.ai/tts/bytes"
_CARTESIA_VERSION = "2024-11-13"


def _split_into_chunks(text: str, target_chars: int = 380) -> list[str]:
    """Split narration into TTS-friendly chunks.

    Splits on sentence boundaries first (`. `, `? `, `! `), packs sentences
    into chunks until target_chars is reached. Never breaks mid-sentence.
    Empty paragraphs (`\n\n`) are preserved as natural pause points and
    DO start a fresh chunk so the silence joiner reads as a paragraph
    break.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    for para in paragraphs:
        # Sentence-split: keep punctuation attached.
        sentences = re.findall(r"[^.!?]+[.!?]+(?:\s|$)|\S[^.!?]*$", para)
        sentences = [s.strip() for s in sentences if s.strip()]
        cur = ""
        for sent in sentences:
            if not cur:
                cur = sent
            elif len(cur) + 1 + len(sent) <= target_chars:
                cur = f"{cur} {sent}"
            else:
                chunks.append(cur)
                cur = sent
        if cur:
            chunks.append(cur)
    return chunks


def _cartesia_chunk(
    text: str,
    voice_id: str,
    api_key: str,
    out_wav: Path,
    speed: str = "slow",
) -> None:
    """Single Cartesia call → wav at out_wav."""
    body = json.dumps({
        "model_id": "sonic-2",
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {
            "container": "wav",
            "encoding": "pcm_s16le",
            "sample_rate": 44100,
        },
        "language": "en",
        "__experimental_controls": {"speed": speed},
    }).encode("utf-8")
    headers = {
        "X-API-Key": api_key,
        "Cartesia-Version": _CARTESIA_VERSION,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(_CARTESIA_URL, data=body, headers=headers, method="POST")
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                out_wav.write_bytes(r.read())
            return
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Cartesia failed after 3 retries: {last_err}")


# ---------- Kokoro local TTS (free, unlimited) ----------------------------


_KOKORO_INSTANCE = None


def _kokoro_get():
    """Lazy-load the project's Kokoro instance from pipeline.audio."""
    global _KOKORO_INSTANCE
    if _KOKORO_INSTANCE is None:
        from pipeline import audio as _aud
        _KOKORO_INSTANCE = _aud._kokoro()
    return _KOKORO_INSTANCE


def _kokoro_chunk(
    text: str,
    voice: str,
    out_wav: Path,
    speed: float = 0.80,
    lang: str = "en-us",
) -> None:
    """Single Kokoro synth → wav at out_wav. No modulation, flat soft delivery.

    Kokoro speed is continuous (unlike Cartesia's bucketed slow/normal/fast),
    so we set speed directly here and the long_form atempo post-pass is
    typically set to 1.0 (no further stretch needed).
    """
    import soundfile as sf
    kk = _kokoro_get()
    samples, sample_rate = kk.create(text, voice=voice, speed=speed, lang=lang)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_wav), samples, sample_rate)


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(args)}")


def _atempo(in_wav: Path, out_wav: Path, factor: float) -> None:
    _ffmpeg(["-i", str(in_wav), "-filter:a", f"atempo={factor}", str(out_wav)])


def _wav_concat_with_silence(wavs: list[Path], silence_s: float, out_wav: Path) -> None:
    """Concat wavs with explicit silence joiners between."""
    if not wavs:
        raise ValueError("no wavs to concat")
    # Build a filter graph: [0:a][1silence][1:a][2silence][2:a]...concat
    # Simpler: render silence.wav once, then concat-demux.
    silence_wav = out_wav.parent / "_silence.wav"
    _ffmpeg([
        "-f", "lavfi", "-t", f"{silence_s}",
        "-i", "anullsrc=r=44100:cl=mono",
        "-c:a", "pcm_s16le", str(silence_wav),
    ])
    list_txt = out_wav.parent / "_concat_list.txt"
    lines: list[str] = []
    for i, w in enumerate(wavs):
        if i > 0:
            lines.append(f"file '{silence_wav.resolve()}'")
        lines.append(f"file '{w.resolve()}'")
    list_txt.write_text("\n".join(lines))
    _ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(list_txt),
        "-c", "copy", str(out_wav),
    ])


def synth_long_narration(
    text: str,
    voice_id: str,
    cache_dir: Path,
    atempo: float,
    chunk_target_chars: int = 380,
    join_silence_s: float = 0.4,
    provider: str = "cartesia",
    speed: float = 0.80,
) -> tuple[Path, list[Path]]:
    """Chunked TTS + atempo. Returns (final narration.wav path, list of chunk wavs).

    Resumable: skips chunks whose stretched wav already exists.

    provider:
        "cartesia" → paid Cartesia Sonic-2 (per-char billing); 'speed' is
            mapped to slow/normal/fast bucket; atempo is typically 0.85 to
            reach true sleep cadence.
        "kokoro"   → free local Kokoro (M2 Max ~1-2x realtime); 'speed' is
            continuous so atempo is typically 1.0 (skip extra stretch).
    """
    api_key = None
    if provider == "cartesia":
        api_key = os.environ.get("CARTESIA_API_KEY")
        if not api_key:
            raise RuntimeError("CARTESIA_API_KEY env var not set")

    cache_dir.mkdir(parents=True, exist_ok=True)
    chunks = _split_into_chunks(text, target_chars=chunk_target_chars)
    print(f"[tts] {len(text)} chars → {len(chunks)} chunks via {provider} (target {chunk_target_chars} chars each)")

    chunk_dir = cache_dir / "tts_chunks"
    chunk_dir.mkdir(exist_ok=True)
    final_chunks: list[Path] = []
    for i, chunk_text in enumerate(chunks):
        raw = chunk_dir / f"raw_{i:04d}.wav"
        stretched = chunk_dir / f"chunk_{i:04d}.wav"
        if stretched.exists() and stretched.stat().st_size > 1024:
            final_chunks.append(stretched)
            continue
        if not raw.exists() or raw.stat().st_size < 1024:
            t0 = time.time()
            if provider == "cartesia":
                _cartesia_chunk(chunk_text, voice_id, api_key, raw, speed="slow")
            elif provider == "kokoro":
                _kokoro_chunk(chunk_text, voice_id, raw, speed=speed)
            else:
                raise RuntimeError(f"unknown TTS provider: {provider!r}")
            print(f"[tts] chunk {i:04d}/{len(chunks)-1}: {len(chunk_text)} chars in {time.time()-t0:.1f}s")
        if abs(atempo - 1.0) < 1e-3:
            shutil.copy2(raw, stretched)
        else:
            _atempo(raw, stretched, atempo)
        final_chunks.append(stretched)

    narration_wav = cache_dir / "narration.wav"
    _wav_concat_with_silence(final_chunks, join_silence_s, narration_wav)
    return narration_wav, final_chunks


# ---------- video stage: trim + 16:9 letterbox + concat -------------------


def _probe_duration(path: Path) -> float:
    return float(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]).decode().strip())


def _trim_clip_letterbox(
    src: Path, in_s: float, out_s: float, out_path: Path,
    out_w: int = 1920, out_h: int = 1080, fps: int = 30,
) -> None:
    """Trim [in_s, out_s] from src, scale to fit 16:9 with blurred letterbox.

    For 4:3 sources (640x480, 320x240) this gives a centered scaled-up
    image with a blurred copy of the same frame filling the side bars —
    same aesthetic as the Shorts blurred-letterbox filter, just sideways.
    """
    duration = max(0.1, out_s - in_s)
    vf = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},gblur=sigma=22[bg2];"
        f"[fg]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[fg2];"
        f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2,fps={fps},format=yuv420p"
    )
    _ffmpeg([
        "-ss", f"{in_s}", "-t", f"{duration}", "-i", str(src),
        "-filter_complex", vf,
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ])


def build_video_track(
    shotlist: dict[str, Any],
    sources_dir: Path,
    cache_dir: Path,
    target_duration_s: float,
    out_w: int = 1920,
    out_h: int = 1080,
    fps: int = 30,
) -> Path:
    """Trim each shotlist clip, concat into one silent video.mp4.

    If concatenated duration < target_duration_s, the last clip is extended
    by replaying its tail at 0.6x speed (slow-mo pad) until the gap closes.
    """
    clips = shotlist.get("clips") or []
    if not clips:
        raise ValueError("shotlist has no `clips`")

    long_clip_dir = cache_dir / "long_clips"
    long_clip_dir.mkdir(exist_ok=True)
    clip_paths: list[Path] = []
    for i, clip in enumerate(clips):
        src = sources_dir / clip["source"]
        if not src.exists():
            raise FileNotFoundError(f"shotlist clip {i} source missing: {src}")
        out = long_clip_dir / f"clip_{i:03d}.mp4"
        if not out.exists() or out.stat().st_size < 1024:
            print(f"[trim] {i+1}/{len(clips)} {clip['in_s']:.1f}-{clip['out_s']:.1f}s of {src.name}")
            _trim_clip_letterbox(src, float(clip["in_s"]), float(clip["out_s"]), out, out_w, out_h, fps)
        clip_paths.append(out)

    # Concat-demux. Re-encoding sidestepped because all clips share params.
    list_txt = cache_dir / "_concat_clips.txt"
    list_txt.write_text("\n".join(f"file '{p.resolve()}'" for p in clip_paths))
    video_path = cache_dir / "video.mp4"
    _ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(list_txt),
        "-c", "copy", str(video_path),
    ])

    # Pad to narration duration via slow-mo on the tail (only if short).
    have = _probe_duration(video_path)
    if have < target_duration_s - 1.0:
        gap = target_duration_s - have
        print(f"[pad ] video {have:.1f}s < target {target_duration_s:.1f}s — slow-mo pad {gap:.1f}s")
        # Slow-mo last 60s at speed 60/(60+gap) so it stretches to fill
        tail_s = min(60.0, have - 1.0)
        slow_factor = tail_s / (tail_s + gap)
        slow_clip = cache_dir / "_tail_slow.mp4"
        _ffmpeg([
            "-ss", f"{have - tail_s}", "-i", str(video_path),
            "-filter:v", f"setpts={1/slow_factor:.4f}*PTS,fps={fps}",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", str(slow_clip),
        ])
        # New concat: original head + slow tail
        head_clip = cache_dir / "_head.mp4"
        _ffmpeg([
            "-t", f"{have - tail_s}", "-i", str(video_path),
            "-c", "copy", str(head_clip),
        ])
        list_txt.write_text(f"file '{head_clip.resolve()}'\nfile '{slow_clip.resolve()}'")
        _ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(list_txt),
            "-c", "copy", str(video_path),
        ])

    return video_path


# ---------- music stage: ambient bed (synthetic placeholder) --------------


def build_music_bed(out_path: Path, duration_s: float) -> Path:
    """Synthesize a low ambient drone bed.

    Until a curated ambient track is dropped into historyrecapped/music/,
    we synthesize a minimal pad: slow-detuned sine waves at low frequencies
    (~80 Hz + 120 Hz fifth) with reverb and a low-pass filter. Not as
    polished as a real ambient track but lets the pipeline render
    end-to-end. Replace by setting long_form.music_bed_default to a wav in
    historyrecapped/music/.
    """
    flt = (
        "sine=frequency=82:duration={d}[s1];"
        "sine=frequency=123:duration={d}[s2];"
        "sine=frequency=164:duration={d}[s3];"
        "[s1][s2][s3]amix=inputs=3:duration=longest:weights=1.0 0.6 0.4,"
        "lowpass=f=400,aecho=0.6:0.5:1000:0.4,volume=-22dB[a]"
    ).format(d=duration_s)
    _ffmpeg([
        "-filter_complex", flt, "-map", "[a]",
        "-c:a", "pcm_s16le", str(out_path),
    ])
    return out_path


# ---------- captions: whisper-aligned sentence-level SRT ------------------


def _hms(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def build_captions_srt(
    narration_wav: Path,
    out_srt: Path,
    max_chars_per_line: int = 70,
    max_lines_per_cue: int = 2,
) -> Path:
    """Whisper-align narration → sentence-level SRT.

    Sleep-mode captions are SENTENCES not word-by-word (the latter is for
    Shorts where attention bursts matter). One or two short lines per cue.
    Style is baked in via the sibling ASS file produced by build_captions_ass.
    """
    import re as _re
    from pipeline import beats as _beats

    print(f"[cap] whisper-aligning {narration_wav.name}…")
    words = _beats.transcribe_words(narration_wav)
    if not words:
        raise RuntimeError("whisper returned no words for caption alignment")

    # Group words into sentences by punctuation in the word text.
    sentences: list[list] = []
    cur: list = []
    for w in words:
        cur.append(w)
        text = (w.text or "").strip()
        if text and text[-1] in ".!?":
            sentences.append(cur)
            cur = []
    if cur:
        sentences.append(cur)

    # Each sentence becomes one or more cues, line-wrapped at max_chars_per_line.
    lines: list[str] = []
    cue_idx = 1
    for sent in sentences:
        if not sent:
            continue
        text = " ".join((w.text or "").strip() for w in sent).strip()
        text = _re.sub(r"\s+", " ", text)
        start = float(sent[0].start)
        end = float(sent[-1].end)
        # Soft-wrap into max_lines_per_cue lines of <= max_chars_per_line each.
        wrapped: list[str] = []
        cur_line = ""
        for tok in text.split(" "):
            if not cur_line:
                cur_line = tok
            elif len(cur_line) + 1 + len(tok) <= max_chars_per_line:
                cur_line = f"{cur_line} {tok}"
            else:
                wrapped.append(cur_line)
                cur_line = tok
        if cur_line:
            wrapped.append(cur_line)
        # If wrapped > max_lines_per_cue, split into multiple cues with even time slices.
        chunks = [wrapped[i:i+max_lines_per_cue] for i in range(0, len(wrapped), max_lines_per_cue)]
        per = (end - start) / max(1, len(chunks))
        for i, ch in enumerate(chunks):
            cs = start + i * per
            ce = cs + per
            lines.append(str(cue_idx))
            lines.append(f"{_hms(cs)} --> {_hms(ce)}")
            lines.extend(ch)
            lines.append("")
            cue_idx += 1
    out_srt.write_text("\n".join(lines), encoding="utf-8")
    print(f"[cap] wrote {cue_idx-1} cues → {out_srt.name}")
    return out_srt


# ---------- captions: PIL-rendered sentence PNGs + ffmpeg overlay chain ---


def build_caption_pngs(
    narration_wav: Path,
    out_dir: Path,
    canvas_w: int = 1920,
    max_chars_per_line: int = 70,
    max_lines_per_cue: int = 2,
) -> list[tuple[Path, float, float]]:
    """Whisper-align narration → one PNG per sentence with sleep styling.

    PIL renders the PNGs (no libass dependency). Returns a list of
    (png_path, start_s, end_s) tuples for the overlay chain. Idempotent
    — re-running re-uses cached PNGs.
    """
    import re as _re
    from PIL import Image, ImageDraw, ImageFont
    from pipeline import beats as _beats

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[cap] whisper-aligning {narration_wav.name}…")
    words = _beats.transcribe_words(narration_wav)
    if not words:
        raise RuntimeError("whisper returned no words for caption alignment")

    # Group words into sentences by punctuation.
    sentences: list[list] = []
    cur: list = []
    for w in words:
        cur.append(w)
        text = (w.text or "").strip()
        if text and text[-1] in ".!?":
            sentences.append(cur)
            cur = []
    if cur:
        sentences.append(cur)

    # Pick a soft sans-serif font. Fallback to PIL default if not found.
    font_paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Avenir.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    font: ImageFont.FreeTypeFont | None = None
    for fp in font_paths:
        if Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, 38)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()  # last-resort, will look basic

    cues: list[tuple[Path, float, float]] = []
    for idx, sent in enumerate(sentences):
        if not sent:
            continue
        text = " ".join((w.text or "").strip() for w in sent).strip()
        text = _re.sub(r"\s+", " ", text)
        start = float(sent[0].start)
        end = float(sent[-1].end)
        # Soft-wrap to <= max_chars per line.
        wrapped: list[str] = []
        cur_line = ""
        for tok in text.split(" "):
            if not cur_line:
                cur_line = tok
            elif len(cur_line) + 1 + len(tok) <= max_chars_per_line:
                cur_line = f"{cur_line} {tok}"
            else:
                wrapped.append(cur_line)
                cur_line = tok
        if cur_line:
            wrapped.append(cur_line)
        # Split into multi-cue if more than max_lines_per_cue lines.
        chunks = [wrapped[i:i+max_lines_per_cue] for i in range(0, len(wrapped), max_lines_per_cue)]
        per = (end - start) / max(1, len(chunks))
        for j, ch in enumerate(chunks):
            cs = start + j * per
            ce = cs + per
            png = out_dir / f"cap_{idx:04d}_{j}.png"
            if not png.exists():
                _render_caption_png(ch, png, canvas_w=canvas_w, font=font)
            cues.append((png, cs, ce))
    print(f"[cap] {len(cues)} sentence PNGs (cached: {sum(1 for c in cues if c[0].exists())})")
    return cues


def _render_caption_png(
    lines: list[str],
    out_path: Path,
    canvas_w: int,
    font,
    text_color=(245, 245, 245, 255),
    shadow_color=(0, 0, 0, 200),
    shadow_offset=(2, 3),
    line_spacing: int = 8,
    pad_y: int = 12,
) -> None:
    """Render one cue (1-2 lines) as a transparent PNG, sleep styling.

    Soft white text with a subtle drop shadow (no harsh outline). PNG is
    sized to the bounding box of the longest line and overlaid bottom-
    centered by ffmpeg.
    """
    from PIL import Image, ImageDraw

    # Measure each line's bbox.
    dummy = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dummy)
    line_metrics = [draw.textbbox((0, 0), line, font=font) for line in lines]
    line_widths = [bb[2] - bb[0] for bb in line_metrics]
    line_heights = [bb[3] - bb[1] for bb in line_metrics]
    max_w = max(line_widths) if line_widths else 1
    total_h = sum(line_heights) + line_spacing * (len(lines) - 1) if lines else 1
    pad_x = 40

    img_w = max_w + pad_x * 2 + abs(shadow_offset[0])
    img_h = total_h + pad_y * 2 + abs(shadow_offset[1])
    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    y = pad_y
    for line, bb, lw, lh in zip(lines, line_metrics, line_widths, line_heights):
        x = (img_w - lw) // 2 - bb[0]
        # Shadow first
        draw.text((x + shadow_offset[0], y + shadow_offset[1]),
                  line, font=font, fill=shadow_color)
        # Then main text
        draw.text((x, y), line, font=font, fill=text_color)
        y += lh + line_spacing

    img.save(str(out_path))


def _hms_ass(t: float) -> str:
    """ASS time format: H:MM:SS.cs (centiseconds)."""
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def build_captions_ass(
    narration_wav: Path,
    out_ass: Path,
) -> Path:
    """Whisper-align narration → ASS subtitle file with sleep-friendly styling baked in.

    ASS bypasses the `subtitles=...:force_style=...` escaping mess in ffmpeg.
    Styling: Helvetica 36, soft white (slightly off-white #F0F0F0), drop shadow
    Shadow=2 (soft, not hard outline), no border outline, bottom-centered with
    margin 80 px. Aligns the look with the calm-evening register.
    """
    import re as _re
    from pipeline import beats as _beats

    print(f"[cap] whisper-aligning {narration_wav.name}…")
    words = _beats.transcribe_words(narration_wav)
    if not words:
        raise RuntimeError("whisper returned no words for caption alignment")

    # Group by sentence-ending punctuation in the word text.
    sentences: list[list] = []
    cur: list = []
    for w in words:
        cur.append(w)
        text = (w.text or "").strip()
        if text and text[-1] in ".!?":
            sentences.append(cur)
            cur = []
    if cur:
        sentences.append(cur)

    # ASS header — single Default style with our sleep-friendly look.
    # PrimaryColour ASS format is &HAABBGGRR (alpha-blue-green-red).
    # &H00F0F0F0 = soft off-white, fully opaque.
    # OutlineColour &H00000000 with Outline=0 + Shadow=2 = pure drop shadow.
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Helvetica,38,&H00F0F0F0,&H00F0F0F0,"
        "&H00000000,&H00000000,0,0,0,0,"
        "100,100,1,0,1,0,3,"
        "2,80,80,80,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )

    events: list[str] = []
    cue_idx = 0
    max_chars = 70
    max_lines = 2
    for sent in sentences:
        if not sent:
            continue
        text = " ".join((w.text or "").strip() for w in sent).strip()
        text = _re.sub(r"\s+", " ", text)
        start = float(sent[0].start)
        end = float(sent[-1].end)
        # Wrap to <= max_chars per line.
        wrapped: list[str] = []
        cur_line = ""
        for tok in text.split(" "):
            if not cur_line:
                cur_line = tok
            elif len(cur_line) + 1 + len(tok) <= max_chars:
                cur_line = f"{cur_line} {tok}"
            else:
                wrapped.append(cur_line)
                cur_line = tok
        if cur_line:
            wrapped.append(cur_line)
        chunks = [wrapped[i:i+max_lines] for i in range(0, len(wrapped), max_lines)]
        per = (end - start) / max(1, len(chunks))
        for i, ch in enumerate(chunks):
            cs = start + i * per
            ce = cs + per
            ass_text = "\\N".join(ch)  # ASS line break is \N
            events.append(
                f"Dialogue: 0,{_hms_ass(cs)},{_hms_ass(ce)},Default,,0,0,0,,{ass_text}"
            )
            cue_idx += 1

    out_ass.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    print(f"[cap] wrote {cue_idx} ASS cues → {out_ass.name}")
    return out_ass


# ---------- final mux: video + (narration + music) ------------------------


def final_mux(
    video_path: Path, narration_wav: Path, music_wav: Path,
    out_path: Path, narration_db: float = -6.0, music_db: float = -28.0,
    caption_cues: list[tuple[Path, float, float]] | None = None,
    margin_v: int = 80,
) -> Path:
    """Mix narration + music; mux against the silent video track.

    If caption_cues is provided, builds an N-overlay filter chain that
    shows each PNG only during its [start, end] window. Bottom-centered
    with margin_v from the bottom edge. Pure overlay = no libass needed
    on the local ffmpeg build.
    """
    a_flt = (
        f"[1:a]volume={narration_db}dB[narr];"
        f"[2:a]volume={music_db}dB[bed];"
        f"[narr][bed]amix=inputs=2:duration=first:dropout_transition=2[a]"
    )
    if caption_cues:
        # Inputs: 0=video, 1=narration, 2=music, then each PNG starts at index 3.
        v_chain_parts: list[str] = []
        cur_label = "[0:v]"
        for i, (_png, cs, ce) in enumerate(caption_cues):
            in_label = f"[{i+3}:v]"
            out_label = f"[v{i}]"
            ov = (
                f"{cur_label}{in_label}overlay="
                f"x=(W-w)/2:y=H-h-{margin_v}:"
                f"enable='between(t,{cs:.3f},{ce:.3f})'"
                f"{out_label}"
            )
            v_chain_parts.append(ov)
            cur_label = out_label
        v_chain = ";".join(v_chain_parts)
        full_flt = f"{v_chain};{a_flt}"
        cmd: list[str] = [
            "-i", str(video_path),
            "-i", str(narration_wav),
            "-i", str(music_wav),
        ]
        for png, _cs, _ce in caption_cues:
            cmd += ["-i", str(png)]
        cmd += [
            "-filter_complex", full_flt,
            "-map", cur_label, "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(out_path),
        ]
        _ffmpeg(cmd)
    else:
        _ffmpeg([
            "-i", str(video_path),
            "-i", str(narration_wav),
            "-i", str(music_wav),
            "-filter_complex", a_flt,
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(out_path),
        ])
    return out_path


# ---------- driver ---------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--tts-only", action="store_true",
                    help="synthesize narration but skip video build (for iterative dev)")
    args = ap.parse_args()

    _load_env(REPO_ROOT)

    channel_dir = REPO_ROOT / args.channel
    config = yaml.safe_load((channel_dir / "config.yaml").read_text())
    narration_path = channel_dir / "narrations" / f"{args.slug}.json"
    if not narration_path.exists():
        raise SystemExit(f"missing narration: {narration_path}")
    script = json.loads(narration_path.read_text())

    cache_dir = channel_dir / "cache" / args.slug
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Long-form mode reads from config["long_form"] (channel YAML may also
    # define top-level Shorts settings — they don't apply here).
    lf = config.get("long_form") or {}
    if not lf:
        raise SystemExit(
            f"{args.channel}/config.yaml has no `long_form:` block — "
            "long-form sleep videos require their own voice / atempo / "
            "ask-cadence config separate from the Shorts settings."
        )
    voice_id = lf["tts_voice"]
    atempo = float(lf.get("tts_post_atempo", 0.85))

    text = script.get("narration") or "\n\n".join(s["text"] for s in script.get("sections", []))
    if not text:
        raise SystemExit("narration JSON has neither 'narration' nor non-empty 'sections'")

    print(f"[1/5] chunked TTS via cartesia voice={voice_id} atempo={atempo}…")
    narration_wav, chunks = synth_long_narration(
        text=text,
        voice_id=voice_id,
        cache_dir=cache_dir,
        atempo=atempo,
        chunk_target_chars=380,
        join_silence_s=0.4,
    )
    dur = float(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(narration_wav),
    ]).decode().strip())
    print(f"[1/5] narration {len(chunks)} chunks → {narration_wav.name} {dur:.1f}s ({dur/60:.1f} min)")

    if args.tts_only:
        print("[done] --tts-only set; stopping after narration synth")
        return 0

    # Stage 2 — trim + letterbox + concat
    shotlist_path = channel_dir / "shotlist" / f"{args.slug}.json"
    if not shotlist_path.exists():
        raise SystemExit(
            f"missing shotlist: {shotlist_path}\n"
            "Long-form needs a shotlist with `clips: [{source, in_s, out_s}, ...]`."
        )
    shotlist = json.loads(shotlist_path.read_text())
    sources_dir = channel_dir / lf.get("footage_dir", "footage/long_sources")
    out_w, out_h = lf.get("output_resolution", [1920, 1080])
    fps = int(lf.get("output_fps", 30))
    print(f"[2/5] trim {len(shotlist['clips'])} clips → {out_w}x{out_h} {fps}fps blurred letterbox…")
    video_path = build_video_track(
        shotlist=shotlist,
        sources_dir=sources_dir,
        cache_dir=cache_dir,
        target_duration_s=dur,
        out_w=out_w, out_h=out_h, fps=fps,
    )
    video_dur = _probe_duration(video_path)
    print(f"[2/5] video → {video_path.name} {video_dur:.1f}s")

    # Stage 3 — music bed (synthetic ambient placeholder until a curated wav is dropped in)
    music_default = lf.get("music_bed_default", "aether-loop.wav")
    music_path = channel_dir / "music" / music_default
    music_wav = cache_dir / "music_bed.wav"
    if music_path.exists():
        # Loop curated track to match narration duration.
        print(f"[3/5] looping {music_default} to {dur:.1f}s…")
        _ffmpeg([
            "-stream_loop", "-1", "-i", str(music_path),
            "-t", f"{dur}", "-c:a", "pcm_s16le", str(music_wav),
        ])
    else:
        print(f"[3/5] {music_default} missing — synthesizing ambient placeholder ({dur:.1f}s)")
        build_music_bed(music_wav, dur)

    # Stage 4 — captions (whisper sentence PNGs + ffmpeg overlay chain)
    caption_cues: list[tuple[Path, float, float]] | None = None
    if bool(lf.get("captions_enabled", False)):
        cap_dir = cache_dir / "captions"
        caption_cues = build_caption_pngs(narration_wav, cap_dir)
    else:
        print("[cap] captions_enabled=false — skipping subtitle burn")

    # Stage 5 — final mux (asks are inline in narration; no separate stage)
    out_dir = channel_dir / "shorts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.slug}.mp4"
    nb = float(lf.get("audio_narration_db", -6.0))
    mb = float(lf.get("audio_music_bed_db", -28.0))
    print(f"[4/4] muxing video + (narration {nb:+.0f}dB + music {mb:+.0f}dB) "
          f"+ {'captions (' + str(len(caption_cues)) + ' cues)' if caption_cues else 'no captions'} → {out_path.name}…")
    final_mux(video_path, narration_wav, music_wav, out_path,
              narration_db=nb, music_db=mb, caption_cues=caption_cues)
    final_dur = _probe_duration(out_path)
    final_size = out_path.stat().st_size // 1024 // 1024
    print(f"[done] {out_path} — {final_dur:.1f}s ({final_dur/60:.1f} min), {final_size} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
