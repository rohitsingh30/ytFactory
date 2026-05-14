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
    """Audit D3.48 — delegates to the shared loader so all three
    render entry points (long_form / sports_doc / footage_only) use
    the same parser, including outer-quote stripping."""
    from pipeline.render.shared.env_loader import load_dotenv_into_environ
    load_dotenv_into_environ(repo_root)


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
    from .shared.concat_safe import concat_file_line  # noqa: PLC0415
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
    from .shared.concat_safe import concat_file_line  # noqa: PLC0415
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
    from .shared.concat_safe import concat_file_line  # noqa: PLC0415
    list_txt.write_text("\n".join(concat_file_line(p.resolve()) for p in parts))
    out = cache_dir / "spliced.mp4"
    _ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(list_txt),
        "-c", "copy", str(out),
    ])
    list_txt.unlink(missing_ok=True)
    return out


# ---------- driver ---------------------------------------------------------
