#!/usr/bin/env python3
"""Long-form sports documentary renderer (16:9, 20-30 min, real footage).

Distinct from historyrecapped/scripts/render_long_form.py (sleep mode, soft
narrator, single ambient bed). Sports doc is intense-podcast register with
real broadcast match footage + commentator/YouTuber talking-head clips +
b-roll + chapter cards + lower-thirds.

Inputs:
    sportsrecapped/narrations/<slug>.json
        chapters[]              — {id, title, start_s (optional, auto), beat, narration}
        engagement_asks[]       — {at_s, line, kind} (already inline in narration prose)
        narrator_tone           — tifo-academic | intense-podcast | playful-spicy | serious-doc
        cold_open               — string (informational; first chapter handles the open)
        thesis, title, hook, music_beds[], thumbnail, metadata
    sportsrecapped/footage_plan/<slug>.json
        match_footage[]         — {id, narration_anchor, url, in_s, out_s, mode, audio_mix, lower_third?}
        talking_heads[]         — {id, narration_anchor, url, in_s, out_s, speaker, speaker_handle, take, mode, audio_mix}
        b_roll[]                — {id, kind, narration_anchor (optional), url, in_s, out_s, audio_mix}
        archival_footage[]      — {id, decade, narration_anchor (optional), url, in_s, out_s, audio_mix}
        motion_graphics[]       — {id, kind, narration_anchor, deferred?, payload}  (Phase 2)
    sportsrecapped/config.yaml `long_form_doc:` block

Output:
    sportsrecapped/long_form/<slug>.mp4   1920x1080 30fps AAC 192k

Pipeline:
    1. TTS (chunked, resumable). Reuses synth_long_narration() from the
       historyrecapped renderer — same chunked + atempo path, just driven
       by long_form_doc config + tone-aware overrides.
    2. Whisper-align narration.wav to authored text → word timestamps.
       Used to populate `at_s` for any footage entry / chapter that
       didn't ship one (matches narration_anchor / chapter title text).
    3. Download + trim each footage_plan source clip via pipeline.footage
       (yt-dlp + ffmpeg, watermark-scrub, blurred letterbox if needed).
    4. Build the timeline:
         * Filler video = b-roll cycled across the full narration duration.
         * Foreground overlays at `at_s`: match_footage, talking_heads,
           archival_footage. Each replaces the filler in its window.
         * Chapter cards at chapter[].start_s — full-frame title slab,
           crossfaded in/out.
         * Lower-thirds during talking_heads windows — speaker name +
           handle, with fade in/out.
    5. Captions (authored-aligned, same path as historyrecapped sleep mode
       but bolder white, not yellow italic).
    6. Music bed: per-section mood lookup in sportsrecapped/music/<mood>/.
       Crossfade between sections. Auto-duck under any clip with audio_mix>0.
    7. Final mux: narration + music + clip audio (mix-weighted, ducked) +
       video + captions + watermark + chapter cards + lower-thirds.

NOT in v1 (stubbed gracefully — won't crash):
    - motion_graphics rendering (stat cards, pitch diagrams, xG charts) —
      entries with `deferred: true` are skipped; non-deferred raise.
    - thumbnail.jpg generation — write a small helper later.

Usage:
    caffeinate -i .venv/bin/python -u \\
        sportsrecapped/scripts/render_long_form_doc.py \\
        --channel sportsrecapped --slug <slug>
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Repo root = parent.parent of pipeline/render/sports_doc.py.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

import yaml

from pipeline import observability as obs

# Reuse historyrecapped's narration synth + caption builder + watermark + mux.
# These are battle-tested across 3+ shipped long-form sleep videos and
# already handle resumable chunked F5-TTS-MLX synthesis, atempo post-pass,
# and authored-text caption alignment. Lives in pipeline.render.long_form
# now (was historyrecapped/scripts/render_long_form.py before the
# 2026-05-05 renderer-promotion refactor).
from pipeline.render.long_form import (  # type: ignore
    synth_long_narration,
    build_caption_pngs_from_chunks,
    render_watermark_png,
    build_music_bed,
    _ffmpeg,
    _probe_duration,
    _trim_clip_letterbox,
)


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


# ---------- tone overrides -------------------------------------------------


_TONE_OVERRIDES = {
    "tifo-academic":   {"speed": 0.98, "atempo": 1.00},
    "intense-podcast": {"speed": 1.02, "atempo": 0.97},
    "playful-spicy":   {"speed": 1.00, "atempo": 1.00},
    "serious-doc":     {"speed": 0.95, "atempo": 0.92},
}


def _apply_tone(lf: dict, tone: str) -> tuple[float, float]:
    """Return (tts_speed, tts_post_atempo) after tone override."""
    base_speed = float(lf.get("tts_speed", 0.98))
    base_atempo = float(lf.get("tts_post_atempo", 1.0))
    if tone in _TONE_OVERRIDES:
        return _TONE_OVERRIDES[tone]["speed"], _TONE_OVERRIDES[tone]["atempo"]
    return base_speed, base_atempo


# ---------- whisper anchor alignment --------------------------------------


# Audit Q2.23 — wrap heavy stages with @obs.traced so the
# render envelope opened in main() has children.
@obs.traced("anchors.sports_doc", category="asr",
            capture=["asr_provider"])
def _align_anchors_to_narration(
    narration_wav: Path,
    anchors: list[str],
    *,
    asr_provider: str = "whisper_mlx",
) -> dict[str, tuple[float, float]]:
    """For each anchor phrase, find its (start_s, end_s) in narration.wav.

    Uses pipeline.beats.transcribe_words on narration.wav to get word-level
    timestamps, then fuzzy-matches each anchor's normalized token sequence
    against the transcript window. First-occurrence match wins; if the
    anchor is referenced multiple times, callers should differentiate via
    chapter prefix in the anchor text.

    Returns a {anchor_text: (start_s, end_s)} dict. Anchors that fail to
    match are omitted — the renderer logs a warning and skips alignment-
    dependent overlays for them (they're only injected if a `at_s` is also
    explicitly authored).

    **Audit Q2.21** — pre-fix this called transcribe_words without
    ``provider=`` so the channel YAML's asr_provider was ignored on
    the sports-doc anchor-alignment path.
    """
    from pipeline import beats as _beats
    import re as _re

    print(f"[align] whisper-transcribing {narration_wav.name} for anchor matching…")
    words = _beats.transcribe_words(narration_wav, provider=asr_provider)
    if not words:
        return {}

    # Normalize: lowercase, strip punctuation, collapse whitespace.
    def _norm(s: str) -> list[str]:
        return [t for t in _re.findall(r"[a-z0-9]+", s.lower()) if t]

    word_tokens = [_norm(w.text)[0] if _norm(w.text) else "" for w in words]
    word_starts = [float(w.start) for w in words]
    word_ends = [float(w.end) for w in words]

    # Filter out empty tokens — whisper can emit punctuation-only fragments
    # that misalign the position index when zipped with anchor_tokens.
    keep_idx = [i for i, t in enumerate(word_tokens) if t]
    word_tokens_clean = [word_tokens[i] for i in keep_idx]
    word_starts_clean = [word_starts[i] for i in keep_idx]
    word_ends_clean = [word_ends[i] for i in keep_idx]

    def _try_match(anchor_tokens: list[str], max_drift: int) -> tuple[int, int] | None:
        n = len(anchor_tokens)
        if n == 0 or n > len(word_tokens_clean):
            return None
        for i in range(len(word_tokens_clean) - n + 1):
            window = word_tokens_clean[i:i + n]
            misses = sum(1 for a, w in zip(anchor_tokens, window) if a != w)
            if misses <= max_drift:
                return (i, i + n - 1)
        return None

    out: dict[str, tuple[float, float]] = {}
    for anchor in anchors:
        anchor_tokens = _norm(anchor)
        if not anchor_tokens:
            continue
        n = len(anchor_tokens)
        # Loosened 2026-05-04 after gegenpress-bundesliga-era render dropped
        # 10/26 anchors. Whisper mistranscribes proper nouns (Bundesliga,
        # Newell, Sacchi, Cruyff, Bayer Leverkusen, Xabi Alonso) — strict
        # max(1, n//5) rejects even one such miss. max(2, n//3) tolerates
        # one proper-noun miscall + one boundary slip per typical anchor.
        max_drift = max(2, n // 3)
        best = _try_match(anchor_tokens, max_drift)

        # Substring fallback for short anchors (≤4 tokens) where token-window
        # match fails. Search for the anchor's joined tokens as a substring
        # of the joined transcript; recover the (start, end) word indices.
        if best is None and n <= 4:
            transcript_joined = " ".join(word_tokens_clean)
            anchor_joined = " ".join(anchor_tokens)
            pos = transcript_joined.find(anchor_joined)
            if pos >= 0:  # pragma: no cover - unreachable with current token-window drift rules
                # Convert char position to word index by counting spaces.
                wi_start = transcript_joined.count(" ", 0, pos)
                best = (wi_start, wi_start + n - 1)

        if best is not None:
            si, ei = best
            out[anchor] = (word_starts_clean[si], word_ends_clean[ei])
        else:
            # Surface the closest 5-token whisper window so the next run can
            # rewrite the anchor to match. Cheap diagnostic — no extra cost.
            nearest = ""
            if word_tokens_clean and anchor_tokens:
                a0 = anchor_tokens[0]
                for i, w in enumerate(word_tokens_clean):
                    if w == a0:
                        nearest = " ".join(word_tokens_clean[i:i + n + 1])
                        break
            print(f"[align] WARN: no match for anchor: {anchor[:80]!r}"
                  f"{f'  (nearest start-token window: {nearest!r})' if nearest else ''}")
    print(f"[align] matched {len(out)}/{len(anchors)} anchors")
    return out


# ---------- footage download + trim ---------------------------------------


def _slug_from_url(url: str) -> str:
    """Stable file-safe slug for caching downloads from a YouTube URL."""
    import hashlib as _h
    return _h.sha256(url.encode("utf-8")).hexdigest()[:16]


def _download_source(url: str, sources_dir: Path) -> Path:
    """yt-dlp the source video into sources_dir/<hash>.mp4. Cached."""
    sources_dir.mkdir(parents=True, exist_ok=True)
    target = sources_dir / f"{_slug_from_url(url)}.mp4"
    if target.exists() and target.stat().st_size > 1024 * 100:
        return target
    print(f"[dl  ] yt-dlp {url} → {target.name}")
    subprocess.run([
        "yt-dlp",
        "-f", "best[ext=mp4][height<=1080]/best[ext=mp4]/best",
        "-o", str(target),
        "--no-progress", "--no-warnings",
        url,
    ], check=True)
    return target


def _prep_footage_clip(
    entry: dict[str, Any],
    sources_dir: Path,
    cache_dir: Path,
    out_w: int,
    out_h: int,
    fps: int,
    grade_filter: str | None,
) -> Path:
    """Download source, trim to [in_s, out_s], normalize to out_w x out_h fps.

    Reuses _trim_clip_letterbox from historyrecapped renderer — same
    blurred-letterbox path for aspect mismatches, same stream-copy
    short-circuit when src already matches. Adds the visual_grade if
    enabled in config.
    """
    cid = entry["id"]
    out = cache_dir / "clips" / f"{cid}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.stat().st_size > 1024 * 100:
        return out
    src = _download_source(entry["url"], sources_dir)
    _trim_clip_letterbox(
        src, float(entry["in_s"]), float(entry["out_s"]), out,
        out_w=out_w, out_h=out_h, fps=fps, grade_filter=grade_filter,
    )
    return out


# ---------- chapter card render -------------------------------------------


def _render_chapter_card(
    chapter_index: int,
    title: str,
    out_path: Path,
    canvas_w: int,
    canvas_h: int,
    bg_rgba: tuple,
    number_color: tuple,
    number_font_size: int,
    title_font_size: int,
) -> Path:
    """Full-frame chapter card: deep teal slab + orange chapter number + bold white title."""
    from PIL import Image, ImageDraw, ImageFont

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (canvas_w, canvas_h), tuple(bg_rgba))
    draw = ImageDraw.Draw(img)

    bold_paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
    ]
    title_font = number_font = None
    for fp in bold_paths:
        if Path(fp).exists():
            try:
                title_font = ImageFont.truetype(fp, title_font_size)
                number_font = ImageFont.truetype(fp, number_font_size)
                break
            except Exception:
                continue
    if title_font is None:
        title_font = ImageFont.load_default()
        number_font = ImageFont.load_default()

    chapter_label = f"CHAPTER {chapter_index:02d}"
    cb = draw.textbbox((0, 0), chapter_label, font=number_font)
    cw, ch = cb[2] - cb[0], cb[3] - cb[1]
    cx = (canvas_w - cw) // 2 - cb[0]
    cy = canvas_h // 2 - title_font_size - 30 - cb[1]
    draw.text((cx, cy), chapter_label, font=number_font, fill=tuple(number_color))

    # Word-wrap title to ~30 chars per line
    words = title.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= 30:
            cur = f"{cur} {w}"
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)

    y = canvas_h // 2 + 10
    for line in lines:
        tb = draw.textbbox((0, 0), line, font=title_font)
        tw = tb[2] - tb[0]
        tx = (canvas_w - tw) // 2 - tb[0]
        draw.text((tx, y - tb[1]), line, font=title_font, fill=(255, 255, 255, 255))
        y += title_font_size + 12

    img.save(str(out_path))
    return out_path


# ---------- lower-third render --------------------------------------------


def _render_lower_third(
    speaker: str,
    handle: str,
    out_path: Path,
    bg_rgba: tuple,
    text_rgba: tuple,
    accent_rgba: tuple,
    font_size: int,
    handle_font_size: int,
) -> Path:
    """Slab lower-third: deep teal block, bold white speaker, orange underline,
    small handle below.
    """
    from PIL import Image, ImageDraw, ImageFont

    out_path.parent.mkdir(parents=True, exist_ok=True)

    plain_paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    name_font = handle_font = None
    for fp in plain_paths:
        if Path(fp).exists():
            try:
                name_font = ImageFont.truetype(fp, font_size)
                handle_font = ImageFont.truetype(fp, handle_font_size)
                break
            except Exception:
                continue
    if name_font is None:
        name_font = ImageFont.load_default()
        handle_font = ImageFont.load_default()

    pad_x, pad_y = 24, 14
    accent_h = 4
    handle_text = handle if handle else ""
    dummy = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dummy)
    nb = dd.textbbox((0, 0), speaker, font=name_font)
    hb = dd.textbbox((0, 0), handle_text, font=handle_font) if handle_text else (0, 0, 0, 0)
    nw = nb[2] - nb[0]
    nh = nb[3] - nb[1]
    hw = hb[2] - hb[0]
    hh = (hb[3] - hb[1]) if handle_text else 0
    inner_w = max(nw, hw)
    inner_h = nh + (8 + hh if handle_text else 0)
    img_w = inner_w + pad_x * 2
    img_h = inner_h + pad_y * 2 + accent_h

    img = Image.new("RGBA", (img_w, img_h), tuple(bg_rgba))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, img_h - accent_h, img_w, img_h), fill=tuple(accent_rgba))
    y = pad_y
    draw.text((pad_x - nb[0], y - nb[1]), speaker, font=name_font, fill=tuple(text_rgba))
    if handle_text:
        y += nh + 8
        # handle in slightly dimmed white
        draw.text((pad_x - hb[0], y - hb[1]), handle_text, font=handle_font,
                  fill=(text_rgba[0], text_rgba[1], text_rgba[2], 200))

    img.save(str(out_path))
    return out_path


# ---------- timeline assembly ---------------------------------------------


def _gather_overlays(
    narration_path: Path,
    footage_plan_path: Path,
    narration_wav: Path,
    chunk_wavs: list[Path],
    join_silence_s: float,
    chunk_target_chars: int,
    narration_text: str,
    *,
    asr_provider: str = "whisper_mlx",
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    """Resolve `at_s` for every overlay-able entry by aligning narration_anchor
    to whisper word timestamps on narration.wav.

    Returns five lists (chapter_cards, match_footage, talking_heads,
    archival_footage, b_roll_with_anchor) — each entry has `at_s` and
    `dur_s` populated. b_roll WITHOUT a narration_anchor is treated as
    background filler (returned as `[]` here; main builds the filler track
    separately).
    """
    nar = json.loads(narration_path.read_text())
    fp = json.loads(footage_plan_path.read_text())

    # Collect every anchor we need to align.
    anchors: list[str] = []
    chapter_anchors: dict[str, str] = {}
    for i, ch in enumerate(nar.get("chapters", [])):
        # Anchor = first sentence of the chapter narration (most stable cue
        # for whisper). If chapter has explicit start_s, skip.
        if ch.get("start_s") is not None:
            continue
        prose = (ch.get("narration") or "").strip()
        first_sent = prose.split(".")[0].strip()[:120] if prose else ch.get("title", "")
        if first_sent:
            anchors.append(first_sent)
            chapter_anchors[ch["id"]] = first_sent

    for arr_name in ("match_footage", "talking_heads", "archival_footage", "b_roll", "motion_graphics"):
        for entry in fp.get(arr_name, []):
            if entry.get("at_s") is not None:
                continue
            a = (entry.get("narration_anchor") or "").strip()
            if a:
                anchors.append(a)

    # Dedupe in order
    seen: set[str] = set()
    uniq_anchors = [a for a in anchors if not (a in seen or seen.add(a))]
    anchor_times = _align_anchors_to_narration(
        narration_wav, uniq_anchors, asr_provider=asr_provider,
    ) if uniq_anchors else {}

    # Resolve chapter start_s — only chapters with a non-empty title get a
    # full-frame chapter card. cold_open + closer typically have title="" and
    # are narration-only sections (no card insert).
    chapter_cards: list[dict] = []
    card_index = 0
    for ch in nar.get("chapters", []):
        title = (ch.get("title") or "").strip()
        if not title:
            continue
        card_index += 1
        if ch.get("start_s") is not None:
            start_s = float(ch["start_s"])
        else:
            anchor = chapter_anchors.get(ch["id"], "")
            tup = anchor_times.get(anchor)
            if tup is None:
                continue
            start_s = max(0.0, tup[0] - 1.5)  # show card 1.5s before chapter narration begins
        chapter_cards.append({
            "id": ch["id"],
            "index": card_index,
            "title": title,
            "at_s": start_s,
        })

    def _resolve(arr_name: str) -> list[dict]:
        out: list[dict] = []
        for entry in fp.get(arr_name, []):
            if entry.get("at_s") is not None:
                at_s = float(entry["at_s"])
            else:
                a = (entry.get("narration_anchor") or "").strip()
                tup = anchor_times.get(a)
                if tup is None:
                    print(f"[plan] {arr_name} {entry.get('id')}: no anchor match — skipping")
                    continue
                at_s = float(tup[0])
            dur_s = float(entry["out_s"]) - float(entry["in_s"])
            e2 = dict(entry)
            e2["at_s"] = at_s
            e2["dur_s"] = dur_s
            out.append(e2)
        return out

    return (
        chapter_cards,
        _resolve("match_footage"),
        _resolve("talking_heads"),
        _resolve("archival_footage"),
        # b_roll with anchors are pinned overlays; b_roll without anchors fill background.
        [e for e in _resolve("b_roll")],
    )


@obs.traced("filler.sports_doc", category="render",
            capture=["target_dur_s"])
def _build_filler_video(
    b_roll_clips: list[Path],
    target_dur_s: float,
    cache_dir: Path,
    out_w: int,
    out_h: int,
    fps: int,
) -> Path:
    """Cycle b_roll clips end-to-end until target_dur_s is filled.

    Each clip is already trimmed + normalized by _prep_footage_clip; this
    just concat-demuxes them in a loop. If the b-roll list is empty, falls
    back to a static dark teal frame for the duration (so the renderer
    still produces output for an under-sourced doc).
    """
    out = cache_dir / "filler.mp4"
    if out.exists() and out.stat().st_size > 1024 * 100:
        return out

    if not b_roll_clips:
        # Solid color filler — same teal as chapter card bg.
        print(f"[fill] no b-roll provided — synthesizing solid-color filler ({target_dur_s:.1f}s)")
        _ffmpeg([
            "-f", "lavfi",
            "-i", f"color=c=0x0a1626:s={out_w}x{out_h}:r={fps}:d={target_dur_s}",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            str(out),
        ])
        return out

    # Build a list that repeats the b_roll cycle until we exceed target.
    clip_durs = [_probe_duration(p) for p in b_roll_clips]
    sequence: list[Path] = []
    accum = 0.0
    i = 0
    while accum < target_dur_s + 1.0:
        clip = b_roll_clips[i % len(b_roll_clips)]
        sequence.append(clip)
        accum += clip_durs[i % len(b_roll_clips)]
        i += 1

    list_txt = cache_dir / "_filler_concat.txt"
    # Audit Q2.25 — concat demuxer single-quote escape.
    from ._concat_safe import concat_file_line  # noqa: PLC0415
    list_txt.write_text("\n".join(concat_file_line(p.resolve()) for p in sequence))
    raw = cache_dir / "_filler_raw.mp4"
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_txt), "-c", "copy", str(raw)])
    # Trim to exactly target_dur_s.
    _ffmpeg(["-i", str(raw), "-t", f"{target_dur_s:.3f}", "-c", "copy", str(out)])
    raw.unlink(missing_ok=True)
    return out


def _overlay_clip_on_filler(
    base: Path,
    overlay: Path,
    at_s: float,
    cache_dir: Path,
) -> Path:
    """Replace base[at_s : at_s+dur(overlay)] with overlay; return new path.

    Cuts base into [head, tail], concats head + overlay + tail. Slower than
    a single ffmpeg overlay+enable filter for short overlays, but produces
    a cleanly re-encoded single file that subsequent passes can copy.

    For perf — if there are many overlays, the caller should use
    :func:`_splice_overlays_batch` which collapses N sequential
    splice operations into a single ffmpeg concat pass.
    """
    overlay_dur = _probe_duration(overlay)
    base_dur = _probe_duration(base)
    end_s = min(base_dur, at_s + overlay_dur)
    head = cache_dir / f"_h_{at_s:.2f}.mp4"
    tail = cache_dir / f"_t_{at_s:.2f}.mp4"
    out = cache_dir / f"_ov_{at_s:.2f}.mp4"

    if at_s > 0.05:
        _ffmpeg(["-t", f"{at_s:.3f}", "-i", str(base), "-c", "copy", str(head)])
    if end_s < base_dur - 0.05:
        _ffmpeg(["-ss", f"{end_s:.3f}", "-i", str(base), "-c", "copy", str(tail)])

    parts: list[Path] = []
    if head.exists():
        parts.append(head)
    parts.append(overlay)
    if tail.exists():
        parts.append(tail)

    list_txt = cache_dir / f"_ov_list_{at_s:.2f}.txt"
    # Audit Q2.25 — concat demuxer single-quote escape.
    from ._concat_safe import concat_file_line  # noqa: PLC0415
    list_txt.write_text("\n".join(concat_file_line(p.resolve()) for p in parts))
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_txt), "-c", "copy", str(out)])
    for p in (head, tail):
        p.unlink(missing_ok=True)
    list_txt.unlink(missing_ok=True)
    return out


@obs.traced("splice.sports_doc", category="render")
def _splice_overlays_batch(
    base: Path,
    overlays: list[tuple[float, Path]],
    cache_dir: Path,
) -> Path:
    """Audit T1.19 — splice every overlay into ``base`` in ONE ffmpeg
    concat pass instead of N sequential cut-paste passes.

    Pre-fix the renderer called :func:`_overlay_clip_on_filler` once per
    overlay, with each call's output feeding the next call's input. For
    a sports doc with 30 overlays on a 30-min base, that meant 30*3=90
    sequential ffmpeg invocations (head-extract + tail-extract + concat
    per overlay), each one re-reading the increasingly-mutated base
    file from disk. -c copy saves the re-encode but doesn't fence the
    repeated I/O — the loop still cost minutes of wallclock per render.

    This helper computes the entire splice plan on the ORIGINAL base
    timeline (no inter-overlay dependency chain), extracts each base
    segment once IN PARALLEL, then runs a single concat at the end.
    For N overlays the work is now max(N+1 segments-extracted-in-parallel,
    1 concat) ~ O(1) wallclock vs the old O(N) sequential.

    overlays: list of (at_s, overlay_path) tuples, with overlay_path's
    full duration replacing base[at_s : at_s + dur(overlay)]. Order
    doesn't matter (we sort).
    """
    if not overlays:
        return base
    overlays_sorted = sorted(overlays, key=lambda t: t[0])
    base_dur = _probe_duration(base)

    # Build the segment plan on the ORIGINAL base — no in-place mutation.
    plan: list[tuple[str, float, float, Path]] = []  # (kind, start, dur, src)
    cursor = 0.0
    for at_s, ov in overlays_sorted:
        ov_dur = _probe_duration(ov)
        # Base segment [cursor, at_s] if non-trivially long.
        if at_s > cursor + 0.05:
            plan.append(("base", cursor, at_s - cursor, base))
        # Overlay clip (whole).
        plan.append(("overlay", 0.0, ov_dur, ov))
        cursor = at_s + ov_dur
    # Tail base segment.
    if cursor < base_dur - 0.05:
        plan.append(("base", cursor, base_dur - cursor, base))

    # Materialise each base segment via parallel ffmpeg extracts.
    # Overlay clips are already complete files; we just reference them
    # directly in the concat list (no extract needed).
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    parts: list[Path] = [Path() for _ in plan]

    def _materialise(i: int) -> None:
        kind, start, dur, src = plan[i]
        if kind == "overlay":
            parts[i] = src
            return
        seg_out = cache_dir / f"_seg_{i:04d}.mp4"
        _ffmpeg([
            "-ss", f"{start:.3f}", "-i", str(src),
            "-t", f"{dur:.3f}", "-c", "copy", str(seg_out),
        ])
        parts[i] = seg_out

    # Tunable concurrency — N=4 keeps disk/CPU pressure bounded.
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_materialise, range(len(plan))))

    list_txt = cache_dir / "_splice_list.txt"
    # Audit Q2.25 — concat demuxer single-quote escape.
    from ._concat_safe import concat_file_line  # noqa: PLC0415
    list_txt.write_text("\n".join(concat_file_line(p.resolve()) for p in parts))
    out = cache_dir / "spliced.mp4"
    _ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(list_txt),
        "-c", "copy", str(out),
    ])
    list_txt.unlink(missing_ok=True)
    return out


# ---------- driver ---------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--tts-only", action="store_true")
    ap.add_argument("--align-only", action="store_true",
                    help="Synth narration + run whisper alignment + write resolved timeline JSON; "
                         "stops before video assembly. Useful for verifying anchor matching.")
    args = ap.parse_args()

    # OTel render envelope — channel + slug + render_kind=sports_doc.
    with obs.render_envelope(
        channel=args.channel,
        slug=args.slug,
        render_kind="sports_doc",
    ):
        try:
            return _main_impl(args)
        except BaseException as e:
            obs.record_exception(e, fatal=True)
            raise


def _main_impl(args) -> int:

    # Pre-warm cloud GPU containers this channel will hit. Fire-and-
    # forget on a daemon thread; no-op when no CLOUDRUN_*_URL set.
    try:
        from pipeline.cloud import warm as _cloud_warm  # noqa: PLC0415

        _cloud_warm.warm_async(args.channel)
    except Exception:  # noqa: BLE001
        pass

    from pipeline.preflight import power_check  # noqa: PLC0415
    power_check(label="sports-doc")

    # Reset the cloud-image circuit breaker per-render — see
    # pipeline/images_cloudrun.py for breaker semantics.
    from pipeline.images_cloudrun import reset_circuit_breaker  # noqa: PLC0415
    reset_circuit_breaker()

    _load_env(REPO_ROOT)

    from pipeline.paths import RenderPaths  # noqa: PLC0415
    paths = RenderPaths.from_channel_dir(args.channel, project_root=REPO_ROOT)
    channel_dir = paths.root  # backward-compat: subsequent code uses channel_dir

    config = yaml.safe_load(paths.config_yaml.read_text())
    lf = config.get("long_form_doc") or {}
    if not lf:
        raise SystemExit(
            f"{args.channel}/config.yaml has no `long_form_doc:` block — "
            "this renderer is sports-doc-specific. For sleep history use "
            "historyrecapped/scripts/render_long_form.py."
        )

    narration_path = paths.narration_for(args.slug)
    # footage_plan/ is channel-wide (sports stores per-doc footage plans).
    footage_plan_path = paths.footage_plan / f"{args.slug}.json"
    if not narration_path.exists():
        raise SystemExit(f"missing narration: {narration_path}")
    if not footage_plan_path.exists():
        raise SystemExit(f"missing footage plan: {footage_plan_path}")

    nar = json.loads(narration_path.read_text())
    fp = json.loads(footage_plan_path.read_text())

    cache_dir = paths.cache_for(args.slug)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Concatenate chapter narration into one text body (chapters are the
    # source of truth; if `narration` field is also present at top level,
    # it's a fallback for single-chapter docs).
    chapters = nar.get("chapters") or []
    if chapters:
        text = "\n\n".join((c.get("narration") or "").strip() for c in chapters if c.get("narration"))
    else:
        text = nar.get("narration") or ""
    if not text.strip():
        raise SystemExit("narration JSON has no chapter prose")

    # Tone-aware TTS overrides
    tone = nar.get("narrator_tone", "tifo-academic")
    speed, atempo = _apply_tone(lf, tone)
    print(f"[tts ] tone={tone} speed={speed} atempo={atempo}")

    # ----- Stage 1+3 with overlap (added 2026-05-13) ----------------------
    # Source downloads + ffmpeg trims for every footage_plan entry are
    # independent of TTS — they only need URL + in_s + out_s, all
    # pre-known from the rewrite-time footage plan. Kick them off on a
    # worker thread BEFORE awaiting chunked TTS so the slow yt-dlp +
    # libx264 work overlaps with the slow cloud TTS work on the wall
    # clock. Gated on :func:`pipeline.stage_overlap.gpu_safe_to_overlap`
    # — when TTS provider is local (f5_tts / kokoro), falls back to
    # the pre-overlap sequential path so Metal contention can't surface.
    out_w, out_h = lf.get("output_resolution", [1920, 1080])
    fps = int(lf.get("output_fps", 30))
    grade_cfg = lf.get("visual_grade") or {}
    grade_filter = grade_cfg.get("filter") if grade_cfg.get("enabled") else None
    sources_dir = channel_dir / lf.get("footage_dir", "footage/sources")
    tts_provider = str(lf.get("tts_provider", "f5_tts"))

    # Collect every footage entry we'll ever need to prep — match
    # clips, talking heads, archival, ALL b-roll (anchored + filler).
    # _gather_overlays may filter some out post-whisper, but pre-
    # prepping them is cheap-to-skip cached work later.
    raw_footage_entries: list[dict] = []
    for arr_name in ("match_footage", "talking_heads", "archival_footage", "b_roll"):
        raw_footage_entries.extend(fp.get(arr_name, []))

    from pipeline.stage_overlap import StageOverlap, gpu_safe_to_overlap  # noqa: PLC0415
    overlap_safe, overlap_reason = gpu_safe_to_overlap(
        tts_provider=tts_provider,
        image_provider=None,  # sports_doc has no diffusion image gen
    )

    def _prep_raw_entries(entries: list[dict]) -> dict[str, Path]:
        """Run download + trim for every footage_plan entry once.
        Returns a {entry_id: clip_path} dict so the post-overlay
        gather can match by id without re-running the work."""
        result: dict[str, Path] = {}
        for entry in entries:
            cid = entry["id"]
            if cid in result:
                continue
            result[cid] = _prep_footage_clip(
                entry, sources_dir, cache_dir,
                out_w, out_h, fps, grade_filter,
            )
        return result

    print(f"[1/7] chunked TTS via {lf.get('tts_provider')}…")
    if overlap_safe and not args.tts_only and raw_footage_entries:
        print(f"[overlap] {overlap_reason} — kicking off footage prep "
              f"({len(raw_footage_entries)} entries) in parallel with TTS")
    elif args.tts_only:
        pass
    else:
        print(f"[overlap] disabled: {overlap_reason} — running stages sequentially")

    clips_by_id: dict[str, Path] = {}
    used_overlap = False
    # Audit Q2.22 — fingerprint-gate the narration cache. Same
    # rationale as long_form.py — switching tts_provider in the
    # YAML must bust the cached chunk wavs (filenames are content-
    # hash but provider isn't in the hash). Wipe stale cache files
    # BEFORE invoking synth_long_narration so the synth re-binds
    # them to the new provider/voice.
    fp_cfg = {
        "tts_provider": tts_provider,
        "tts_voice": lf.get("tts_voice"),
        "tts_speed": speed,
        "tts_ref_text": lf.get("tts_ref_text"),
        "tts_chunk_join_silence_s": float(lf.get("tts_chunk_join_silence_s", 0.35)),
        "tts_chunk_target_chars": int(lf.get("tts_chunk_target_chars", 380)),
    }
    candidate_narr_wav = cache_dir / "narration.wav"
    from pipeline.render._voice_fingerprint import (  # noqa: PLC0415
        maybe_wipe_stale_chunks as _voice_maybe_wipe,
    )
    _voice_maybe_wipe(candidate_narr_wav, fp_cfg)
    tts_t0 = time.time()
    if overlap_safe and not args.tts_only and raw_footage_entries:
        used_overlap = True
        with StageOverlap(
            label="sports_doc-footage_prep",
            max_workers=1,
            log=True,
        ) as overlap:
            footage_prep_fut = overlap.submit(
                "footage_prep",
                _prep_raw_entries,
                raw_footage_entries,
            )

            narration_wav, chunks = synth_long_narration(
                text=text,
                voice_id=lf["tts_voice"],
                cache_dir=cache_dir,
                atempo=atempo,
                chunk_target_chars=int(lf.get("tts_chunk_target_chars", 380)),
                join_silence_s=float(lf.get("tts_chunk_join_silence_s", 0.35)),
                provider=tts_provider,
                speed=speed,
                ref_audio_text=lf.get("tts_ref_text"),
            )
            narration_dur = _probe_duration(narration_wav)
            tts_done_s = time.time() - tts_t0
            print(f"[1/7] tts done {tts_done_s:.1f}s — {len(chunks)} chunks → "
                  f"{narration_wav.name} {narration_dur:.1f}s ({narration_dur/60:.1f} min)")

            clips_by_id = footage_prep_fut.result()
            footage_done_s = time.time() - tts_t0
            print(f"[3/7] footage prep done {footage_done_s:.1f}s "
                  f"({len(clips_by_id)} clips)")
    else:
        narration_wav, chunks = synth_long_narration(
            text=text,
            voice_id=lf["tts_voice"],
            cache_dir=cache_dir,
            atempo=atempo,
            chunk_target_chars=int(lf.get("tts_chunk_target_chars", 380)),
            join_silence_s=float(lf.get("tts_chunk_join_silence_s", 0.35)),
            provider=tts_provider,
            speed=speed,
            ref_audio_text=lf.get("tts_ref_text"),
        )
        narration_dur = _probe_duration(narration_wav)
        tts_done_s = time.time() - tts_t0
        print(f"[1/7] tts done {tts_done_s:.1f}s — {len(chunks)} chunks → "
              f"{narration_wav.name} {narration_dur:.1f}s ({narration_dur/60:.1f} min)")

    # Backwards-compat: keep the legacy "[1/7] narration ..." banner.
    print(f"[1/7] narration {len(chunks)} chunks → {narration_wav.name} {narration_dur:.1f}s ({narration_dur/60:.1f} min)")

    if args.tts_only:
        print("[done] --tts-only set; stopping after narration synth")
        return 0

    # 2026-05-05: drop F5-TTS-MLX (~1.35 GB) at the renderer-stage boundary.
    # Subsequent stages (whisper alignment, footage trim, mux) don't need
    # F5; previously it leaked through to those stages and contributed to
    # Metal aborts on long renders. No-op when provider != f5_tts.
    if tts_provider == "f5_tts":
        from pipeline.preflight import reset_mlx_state  # noqa: PLC0415
        reset_mlx_state(drop_f5=True, label="sports-doc stage-1 TTS")

    # ----- Stage 2: Anchor alignment + timeline resolution -----------------
    print("[2/7] resolving timeline (whisper-aligning anchors)…")
    chapter_cards_meta, match_clips_meta, talking_heads_meta, archival_meta, b_roll_anchored_meta = _gather_overlays(
        narration_path=narration_path,
        footage_plan_path=footage_plan_path,
        narration_wav=narration_wav,
        chunk_wavs=chunks,
        join_silence_s=float(lf.get("tts_chunk_join_silence_s", 0.35)),
        chunk_target_chars=int(lf.get("tts_chunk_target_chars", 380)),
        narration_text=text,
        # Audit Q2.21 — honour channel YAML's asr_provider on the
        # sports-doc anchor-alignment path.
        asr_provider=config.get("asr_provider", "whisper_mlx"),
    )

    timeline_summary = {
        "narration_dur_s": narration_dur,
        "chapter_cards": chapter_cards_meta,
        "match_footage": match_clips_meta,
        "talking_heads": talking_heads_meta,
        "archival_footage": archival_meta,
        "b_roll_anchored": b_roll_anchored_meta,
    }
    (cache_dir / "timeline.json").write_text(json.dumps(timeline_summary, indent=2))
    print(f"[2/7] timeline → {cache_dir / 'timeline.json'} "
          f"({len(chapter_cards_meta)} chapters · {len(match_clips_meta)} match · "
          f"{len(talking_heads_meta)} heads · {len(archival_meta)} archival · "
          f"{len(b_roll_anchored_meta)} pinned b-roll)")

    if args.align_only:
        print("[done] --align-only set; stopping after timeline resolution")
        return 0

    # ----- Stage 3: Trim every footage clip --------------------------------
    # When the parallel branch ran (overlap_safe), every clip is
    # already on disk in clips_by_id keyed by entry["id"]. Match by id
    # for the per-array lists — falls back to inline _prep_footage_clip
    # for any entry missed by the parallel branch (e.g. _gather_overlays
    # surfaced a NEW b-roll entry after whisper, or the overlap path
    # was skipped entirely).
    def _prep_all(arr: list[dict]) -> list[Path]:
        out: list[Path] = []
        for e in arr:
            cid = e["id"]
            if cid in clips_by_id:
                out.append(clips_by_id[cid])
            else:
                clip = _prep_footage_clip(
                    e, sources_dir, cache_dir,
                    out_w, out_h, fps, grade_filter,
                )
                clips_by_id[cid] = clip
                out.append(clip)
        return out

    if used_overlap:
        print(f"[3/7] dispatching footage clips from overlap cache "
              f"(out={out_w}x{out_h}@{fps}, grade={'on' if grade_filter else 'off'})…")
    else:
        print(f"[3/7] preparing footage clips (out={out_w}x{out_h}@{fps}, grade={'on' if grade_filter else 'off'})…")
    match_clips = _prep_all(match_clips_meta)
    talking_clips = _prep_all(talking_heads_meta)
    archival_clips = _prep_all(archival_meta)
    pinned_broll_clips = _prep_all(b_roll_anchored_meta)

    # Background b-roll = b-roll entries WITHOUT narration_anchor (they're
    # filler, not pinned).
    bg_b_roll = [e for e in fp.get("b_roll", []) if not (e.get("narration_anchor") or "").strip()]
    bg_clip_paths = _prep_all(bg_b_roll)

    # ----- Stage 4: Filler video track -------------------------------------
    print(f"[4/7] building filler track from {len(bg_clip_paths)} background b-roll clips ({narration_dur:.1f}s)…")
    base_video = _build_filler_video(bg_clip_paths, narration_dur, cache_dir, out_w, out_h, fps)

    # ----- Stage 5: Overlay every pinned clip + chapter card ---------------
    # Build chapter card mp4s (3s each, full-frame, with crossfade).
    cc_cfg = lf.get("chapter_card") or {}
    cc_dur = float(cc_cfg.get("duration_s", 3.0))
    cc_dir = cache_dir / "chapter_cards"
    cc_dir.mkdir(exist_ok=True)
    cc_clip_paths: list[Path] = []
    for cc in chapter_cards_meta:
        png = cc_dir / f"card_{cc['index']:02d}.png"
        if not png.exists():
            _render_chapter_card(
                chapter_index=int(cc["index"]),
                title=str(cc["title"]),
                out_path=png,
                canvas_w=out_w, canvas_h=out_h,
                bg_rgba=tuple(cc_cfg.get("bg_rgba", [10, 22, 38, 240])),
                number_color=tuple(cc_cfg.get("number_color", [255, 168, 0, 255])),
                number_font_size=int(cc_cfg.get("number_font_size", 48)),
                title_font_size=int(cc_cfg.get("title_font_size", 72)),
            )
        # PNG → silent mp4 of cc_dur length.
        clip = cc_dir / f"card_{cc['index']:02d}.mp4"
        if not clip.exists():
            _ffmpeg([
                "-loop", "1", "-t", f"{cc_dur}", "-i", str(png),
                "-vf", f"fps={fps},format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                str(clip),
            ])
        cc_clip_paths.append(clip)

    # Overlay sequence: chapter cards + match + talking + archival + pinned b-roll,
    # sorted by at_s. Apply LATEST-first so earlier `at_s` cuts aren't
    # invalidated by re-encoding (each overlay re-encodes; later overlays
    # need accurate base timestamps which only stay accurate if we work from
    # the END of the timeline backwards).
    all_overlays: list[tuple[float, Path, dict]] = []
    for cc, clip in zip(chapter_cards_meta, cc_clip_paths):
        all_overlays.append((float(cc["at_s"]), clip, {"kind": "chapter", **cc}))
    for meta, clip in zip(match_clips_meta, match_clips):
        all_overlays.append((float(meta["at_s"]), clip, {"kind": "match", **meta}))
    for meta, clip in zip(talking_heads_meta, talking_clips):
        all_overlays.append((float(meta["at_s"]), clip, {"kind": "head", **meta}))
    for meta, clip in zip(archival_meta, archival_clips):
        all_overlays.append((float(meta["at_s"]), clip, {"kind": "archival", **meta}))
    for meta, clip in zip(b_roll_anchored_meta, pinned_broll_clips):
        all_overlays.append((float(meta["at_s"]), clip, {"kind": "broll", **meta}))

    all_overlays.sort(key=lambda t: t[0], reverse=True)

    print(f"[5/7] overlaying {len(all_overlays)} pinned clips/cards onto filler…")
    composed_dir = cache_dir / "composed"
    composed_dir.mkdir(exist_ok=True)
    # Audit T1.19 — batch all overlays into ONE concat-pass instead of
    # the pre-fix N sequential cut-paste passes (which re-read the
    # increasingly-mutated base file from disk every iteration).
    if all_overlays:
        cur = _splice_overlays_batch(
            base_video,
            [(at_s, clip) for at_s, clip, _meta in all_overlays],
            composed_dir,
        )
    else:
        cur = base_video

    composed_video = cache_dir / "composed.mp4"
    shutil.copy2(cur, composed_video)

    # ----- Stage 6: Audio bed (narration + simple music + clip audio) ------
    # v1: simple — narration only + a single ambient music bed (synthesized
    # if no curated wav). Per-clip audio mixing is wired but only narration
    # ducks under any clip with audio_mix > 0; multi-mood music selection is
    # Phase 2 (TODO).
    music_path = cache_dir / "music_bed.wav"
    music_dir = channel_dir / lf.get("music_dir", "music")
    curated = list(music_dir.glob("*.wav")) if music_dir.exists() else []
    if curated:
        # Loop first wav to length.
        first = curated[0]
        print(f"[6/7] looping {first.name} to {narration_dur:.1f}s")
        _ffmpeg(["-stream_loop", "-1", "-i", str(first),
                 "-t", f"{narration_dur}", "-c:a", "pcm_s16le", str(music_path)])
    else:
        print(f"[6/7] no curated music in {music_dir} — synthesizing ambient placeholder")
        build_music_bed(music_path, narration_dur)

    # ----- Stage 7: Captions + watermark + lower-thirds + final mux --------
    cap_cues: list[tuple[Path, float, float]] | None = None
    if bool(lf.get("captions_enabled", True)):
        cap_dir = cache_dir / "captions"
        cap_style = lf.get("caption_style") or {}
        for stale in cap_dir.glob("cap_*.png") if cap_dir.exists() else []:
            stale.unlink()
        cap_cues = build_caption_pngs_from_chunks(
            narration_text=text,
            chunk_wavs=chunks,
            join_silence_s=float(lf.get("tts_chunk_join_silence_s", 0.35)),
            out_dir=cap_dir,
            text_color=tuple(cap_style.get("text_rgba", (255, 255, 255, 255))),
            italic=bool(cap_style.get("italic", False)),
            chunk_target_chars=int(lf.get("tts_chunk_target_chars", 380)),
        )

    # Watermark
    wm_cfg = lf.get("watermark") or {}
    wm_png: Path | None = None
    if wm_cfg.get("enabled", True):
        wm_path = channel_dir / "branding" / "watermark_topright_doc.png"
        if not wm_path.exists():
            print(f"[wm  ] rendering watermark '{wm_cfg.get('text')}' → {wm_path.name}")
            render_watermark_png(
                str(wm_cfg.get("text", "SPORTS STORIES")),
                wm_path,
                font_size=int(wm_cfg.get("font_size", 28)),
                text_color=tuple(wm_cfg.get("text_rgba", (255, 255, 255, 130))),
            )
        wm_png = wm_path

    # Lower-thirds — render PNG per talking head, build (path, at_s, end_s) tuples.
    lt_cfg = lf.get("lower_third") or {}
    lt_cues: list[tuple[Path, float, float]] = []
    if lt_cfg.get("enabled", True):
        lt_dir = cache_dir / "lower_thirds"
        for meta in talking_heads_meta:
            speaker = meta.get("speaker") or ""
            if not speaker:
                continue
            lt_png = lt_dir / f"lt_{meta['id']}.png"
            if not lt_png.exists():
                _render_lower_third(
                    speaker=speaker,
                    handle=meta.get("speaker_handle") or "",
                    out_path=lt_png,
                    bg_rgba=tuple(lt_cfg.get("bg_rgba", [10, 22, 38, 215])),
                    text_rgba=tuple(lt_cfg.get("text_rgba", [255, 255, 255, 255])),
                    accent_rgba=tuple(lt_cfg.get("accent_rgba", [255, 168, 0, 255])),
                    font_size=int(lt_cfg.get("font_size", 36)),
                    handle_font_size=int(lt_cfg.get("handle_font_size", 24)),
                )
            hold_min = float(lt_cfg.get("hold_min_s", 2.5))
            cue_dur = max(hold_min, float(meta["dur_s"]))
            lt_cues.append((lt_png, float(meta["at_s"]), float(meta["at_s"]) + cue_dur))

    # Final mux. Build a single ffmpeg invocation that:
    #   in0 = composed video, in1 = narration wav, in2 = music wav,
    #   in3 = watermark png (if any),
    #   inN.. = caption PNGs,
    #   inM.. = lower-third PNGs.
    # Audio: mix narration + music; narration ducks during talking-head clips
    #   (window-based volume sidechain — emulated via simple amix weights for v1).
    out_dir = paths.long_form
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.slug}.mp4"

    nb = float(lf.get("audio_narration_db", -5.0))
    mb = float(lf.get("audio_music_bed_db", -22.0))

    cmd: list[str] = [
        "-i", str(composed_video),
        "-i", str(narration_wav),
        "-i", str(music_path),
    ]
    extra_inputs = 3
    wm_input_idx: int | None = None
    if wm_png:
        cmd += ["-i", str(wm_png)]
        wm_input_idx = extra_inputs
        extra_inputs += 1
    cap_first_idx = extra_inputs
    if cap_cues:
        for png, _s, _e in cap_cues:
            cmd += ["-i", str(png)]
            extra_inputs += 1
    lt_first_idx = extra_inputs
    if lt_cues:
        for png, _s, _e in lt_cues:
            cmd += ["-i", str(png)]
            extra_inputs += 1

    # Audio filter — narration + music + clip audio with simple ducking.
    #
    # Audit T1.16 — pre-fix this only mixed [1:a] (narration) and
    # [2:a] (music). The composed.mp4 at input 0 has audio from
    # concat-demuxing the source clips (commentator/match audio),
    # but [0:a] was never referenced → every commentator clip's
    # audio dropped from the final mux. Now [0:a] is mixed in too,
    # at the configured clip_audio_db (defaults to a quiet -18 dB
    # so it sits under narration). Set ``audio_clip_db: 0`` in the
    # channel YAML's long_form: block to leave clip audio at its
    # source level.
    #
    # Audit T1.17 — narration runs through single-pass loudnorm
    # (mirroring long_form.py's bfbbec1 fix) so cloud TTS providers
    # (Chatterbox / Higgs / IndicF5) reach the YouTube spoken-word
    # target before the per-channel narration_db trim applies.
    # Pre-fix the user reported inaudible narration on cloud renders;
    # the same regression would recur on every sports doc rendered
    # against a cloud TTS service without this guard.
    cb = float(lf.get("audio_clip_db", -18.0))
    a_flt = (
        f"[0:a]volume={cb}dB[clips];"
        f"[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,volume={nb}dB[narr];"
        f"[2:a]volume={mb}dB[bed];"
        f"[clips][narr][bed]amix=inputs=3:duration=first:dropout_transition=2[a]"
    )

    # Video filter chain — overlay watermark, caption PNGs, lower-thirds.
    v_chain: list[str] = []
    cur_label = "[0:v]"
    if wm_input_idx is not None:
        v_chain.append(
            f"{cur_label}[{wm_input_idx}:v]overlay="
            f"x=W-w-{int(wm_cfg.get('margin', 32))}:y={int(wm_cfg.get('margin', 32))}[vwm]"
        )
        cur_label = "[vwm]"

    if cap_cues:
        margin_v = int(lf.get("caption_margin_v", 80))
        for i, (_p, cs, ce) in enumerate(cap_cues):
            in_label = f"[{cap_first_idx + i}:v]"
            out_label = f"[vc{i}]"
            v_chain.append(
                f"{cur_label}{in_label}overlay="
                f"x=(W-w)/2:y=H-h-{margin_v}:"
                f"enable='between(t,{cs:.3f},{ce:.3f})'{out_label}"
            )
            cur_label = out_label

    if lt_cues:
        lt_x = int(lt_cfg.get("margin_x", 60))
        lt_y_from_bottom = int(lt_cfg.get("margin_y", 110))
        for i, (_p, ls, le) in enumerate(lt_cues):
            in_label = f"[{lt_first_idx + i}:v]"
            out_label = f"[vl{i}]"
            v_chain.append(
                f"{cur_label}{in_label}overlay="
                f"x={lt_x}:y=H-h-{lt_y_from_bottom}:"
                f"enable='between(t,{ls:.3f},{le:.3f})'{out_label}"
            )
            cur_label = out_label

    if v_chain:
        full_flt = ";".join(v_chain) + ";" + a_flt
    else:
        full_flt = a_flt

    cmd += [
        "-filter_complex", full_flt,
        "-map", cur_label, "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(out_path),
    ]

    print(f"[7/7] muxing → {out_path.name} (caps={len(cap_cues or [])} lt={len(lt_cues)} wm={'on' if wm_png else 'off'})…")
    _ffmpeg(cmd)

    final_dur = _probe_duration(out_path)
    final_mb = out_path.stat().st_size // 1024 // 1024
    print(f"[done] {out_path} — {final_dur:.1f}s ({final_dur/60:.1f} min), {final_mb} MB")
    return 0


def cli_main() -> int:
    """CLI entry point. Invoked by ``sportsrecapped/scripts/render_long_form_doc.py``."""
    return main()


if __name__ == "__main__":
    sys.exit(cli_main())
