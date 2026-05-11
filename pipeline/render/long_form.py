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
       F5-TTS-MLX (the only supported provider), post-process each with
       ffmpeg atempo (channel YAML tts_post_atempo) for true sleep-cadence.
       Concatenate with silence joiners → narration.wav.
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
    .venv/bin/python historyrecapped/scripts/render_long_form.py \
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

# Repo root = parent.parent of pipeline/render/long_form.py (was parent.parent.parent
# when this file lived under historyrecapped/scripts/).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

import yaml

from pipeline import observability as obs


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


# ---------- chunked TTS ----------------------------------------------------


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


# ---------- per-chunk TTS adapters ----------------------------------------
# `synth_long_narration` is the shared chunked+resumable+atempo'd long-form
# narration synth used by historyrecapped (sleep), sportsrecapped
# (sports docs), and any future long-form channel. The historyrecapped
# RENDERER rejects tts_provider != f5_tts upstream (strict-F5 rule), so the
# shared function can support multiple providers without violating it.


def _f5_chunk(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str,
    out_wav: Path,
    speed: float = 0.95,
    provider: str = "f5_tts",
) -> None:
    """Single TTS chunk → wav. Routes by ``provider`` to the matching
    backend in ``pipeline.audio.synthesize``:

    * ``f5_tts`` / ``cloudrun_f5`` — F5-TTS (local MLX or Cloud Run L4).
      Both produce the same voice character because cloud uses the same
      checkpoint + flow-matching params (see docs/cloudrun_tts.md and
      cloud/tts-f5/models/f5.py).
    * ``cloudrun_chatterbox`` — Chatterbox via Cloud Run L4. Picked as
      the canonical long-form English provider on 2026-05-10 after
      ``ytfactory-tts-f5`` was retired in favour of
      ``ytfactory-tts-chatterbox``. Conditions on the ref-WAV embedding
      only — ``ref_audio_text`` is ignored.

    Name retained for backward compat (sports_doc.py and tests import
    ``_f5_chunk``); a future refactor can rename to ``_synth_chunk``.
    """
    from pipeline import audio as _aud
    ref_path = ref_audio_path
    if not Path(ref_path).is_absolute():
        ref_path = str(REPO_ROOT / ref_path)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    _aud.synthesize(
        text=text,
        voice=ref_path,
        ref_audio_text=ref_audio_text,
        out_path=out_wav,
        speed=speed,
        provider=provider,
    )


def _kokoro_chunk(
    text: str,
    voice: str,
    out_wav: Path,
    speed: float = 1.0,
) -> None:
    """Single Kokoro chunk synth → wav at out_wav.

    Delegates to pipeline.audio._synth_kokoro (per-sentence internally with
    modulation). Used by long-form renderers whose channel config picks
    ``tts_provider: kokoro`` — currently sportsrecapped long-form doc
    after theo.wav was lost in the 2026-05-04 recovery wipe.
    """
    from pipeline import audio as _aud
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    _aud._synth_kokoro(
        text=text,
        voice=voice,
        out_path=out_wav,
        speed=speed,
    )


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
    speed: float = 0.80,
    ref_audio_text: str | None = None,
    provider: str = "f5_tts",
) -> tuple[Path, list[Path]]:
    """Chunked TTS + atempo. Returns (final narration.wav, list of chunk wavs).

    Resumable: skips chunks whose stretched wav already exists.

    Providers:
      * ``f5_tts`` (default, historyrecapped sleep) — local MLX zero-shot
        voice clone. ``voice_id`` is the path to a 5-15s reference WAV;
        ``ref_audio_text`` is its transcript (required). The model is
        held in a singleton at pipeline/audio.py to avoid the 1.35GB
        checkpoint re-load.
      * ``cloudrun_f5`` — same model, hosted on Cloud Run GPU L4.
        Eligible for parallel chunk dispatch (see CHUNK_PARALLEL_WORKERS
        below) — Cloud Run scales to N independent L4 instances, so
        chunks render concurrently rather than serially. Auto-falls
        back to local f5_tts on cloud failure (handled inside
        pipeline.tts.cloudrun).
      * ``cloudrun_chatterbox`` — Chatterbox via Cloud Run GPU L4.
        Same fan-out / prewarm / fallback semantics as cloudrun_f5
        (single shared cloud-TTS infra in pipeline.tts.cloudrun).
        Does NOT use ``ref_audio_text`` — Chatterbox conditions on
        the ref-WAV embedding only. ``voice_id`` is still the path
        to the 5-15s ref WAV (e.g. ``pipeline/voice_refs/sarah.wav``).
        Picked as the canonical long-form English provider on
        2026-05-10 after ``ytfactory-tts-f5`` was retired in favour
        of ``ytfactory-tts-chatterbox``.
      * ``kokoro`` (sportsrecapped long-form doc) — Kokoro 82M
        voice catalogue. ``voice_id`` is a Kokoro voice id (e.g.
        ``am_michael``). ``ref_audio_text`` is unused. Local-only;
        not parallelised (Kokoro is fast enough that the fan-out
        coordination overhead exceeds the savings).
    """
    if provider in ("f5_tts", "cloudrun_f5") and not ref_audio_text:
        raise RuntimeError(
            "long-form F5 (local or cloud) requires `tts_ref_text` in "
            "long_form config (the spoken transcript of the ref WAV at "
            "tts_voice)"
        )
    if provider not in ("f5_tts", "cloudrun_f5", "cloudrun_chatterbox", "kokoro"):
        raise RuntimeError(
            f"synth_long_narration: unsupported provider {provider!r}. "
            "Supported: f5_tts, cloudrun_f5, cloudrun_chatterbox, kokoro."
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    chunks = _split_into_chunks(text, target_chars=chunk_target_chars)
    print(f"[tts] {len(text)} chars → {len(chunks)} chunks via {provider} (target {chunk_target_chars} chars each)")

    # Cloud Run providers can fan out — each chunk hits an independent
    # L4 instance up to the configured concurrency. Local providers
    # MUST stay serial (shared M2 Max GPU; concurrent MLX/Metal ops
    # serialise + fragment unified memory — see
    # docs/long_form_model_inventory.md "Parallelism" section).
    is_cloud = provider in ("cloudrun_f5", "cloudrun_chatterbox")
    parallel_workers = 0
    if is_cloud:
        parallel_workers = int(os.environ.get(
            "YTFACTORY_CLOUD_TTS_WORKERS", "5",
        ))
        # Cap at our quota so we don't get rejected; quota=5 today.
        parallel_workers = max(1, min(parallel_workers, 5))
        print(f"[tts] cloud provider — fan out {parallel_workers}-way "
              f"(scaling each chunk to its own L4 instance)")
        # PREWARM: hit /readyz once before the fan-out so the first
        # `parallel_workers` chunks don't all pay simultaneous cold-start.
        # One pre-warm = one cold-start cost amortised across all chunks
        # in this render. Subsequent renders within ~15 min reuse warm.
        try:
            from pipeline.tts.cloudrun import _service_url, _get_id_token
            import urllib.request
            # Map provider → cloudrun.py model alias for the URL lookup.
            _model_alias = "chatterbox" if provider == "cloudrun_chatterbox" else "f5"
            url = _service_url(model=_model_alias)
            token = _get_id_token(url)
            req = urllib.request.Request(
                url=f"{url}/readyz",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                _info = resp.read().decode("utf-8")[:120]
                print(f"[tts] cloud /readyz: {_info}")
        except Exception as e:
            print(f"[tts] prewarm /readyz failed (will pay cold-start "
                  f"per chunk): {e}")

    # MLX heap hygiene — only matters for local MLX path. Cloud path
    # doesn't run any MLX ops on the laptop side.
    try:
        import mlx.core as _mx  # type: ignore
        _has_mlx = True
    except Exception:
        _mx = None
        _has_mlx = False
    MLX_FLUSH_EVERY = int(os.environ.get("YTFACTORY_MLX_FLUSH_EVERY", "10"))

    chunk_dir = cache_dir / "tts_chunks"
    chunk_dir.mkdir(exist_ok=True)
    final_chunks: list[Path] = []

    # ---- Build the work plan: (idx, chunk_text, raw_path, stretched_path) ----
    # Skip chunks already cached. Parallel and serial paths share this plan.
    work: list[tuple[int, str, Path, Path]] = []
    for i, chunk_text in enumerate(chunks):
        raw = chunk_dir / f"raw_{i:04d}.wav"
        stretched = chunk_dir / f"chunk_{i:04d}.wav"
        if stretched.exists() and stretched.stat().st_size > 1024:
            final_chunks.append(stretched)  # already done
            continue
        work.append((i, chunk_text, raw, stretched))
        # Reserve the slot in final_chunks; we'll fill in order below.
        final_chunks.append(stretched)

    if not work:
        print(f"[tts] all {len(chunks)} chunks already cached; nothing to synth")
    elif is_cloud and parallel_workers > 1 and len(work) > 1:
        # ---- PARALLEL CLOUD PATH ----
        # Cloud Run cold-starts ~30-60 s for the first instance; warm
        # calls 2-5 s. Fan-out lets us pay cold-start once across many
        # chunks instead of once per render-batch.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _do_chunk(item: tuple[int, str, Path, Path]) -> tuple[int, float]:
            i, chunk_text, raw, stretched = item
            t0 = time.time()
            if not raw.exists() or raw.stat().st_size < 1024:
                _f5_chunk(
                    chunk_text, voice_id, ref_audio_text, raw,
                    speed=speed, provider=provider,
                )
            if abs(atempo - 1.0) < 1e-3:
                shutil.copy2(raw, stretched)
            else:
                _atempo(raw, stretched, atempo)
            return (i, time.time() - t0)

        print(f"[tts] cloud fan-out: {len(work)} chunks × {parallel_workers} workers")
        completed = 0
        with ThreadPoolExecutor(max_workers=parallel_workers) as pool:
            futures = {pool.submit(_do_chunk, w): w[0] for w in work}
            for fut in as_completed(futures):
                idx, dt = fut.result()
                completed += 1
                chunk_text = chunks[idx]
                print(f"[tts] cloud chunk {idx:04d}/{len(chunks)-1}: "
                      f"{len(chunk_text)} chars in {dt:.1f}s "
                      f"({completed}/{len(work)} done)")
    else:
        # ---- SERIAL PATH (local provider, OR cloud with workers=1) ----
        synthed_this_run = 0
        for i, chunk_text, raw, stretched in work:
            if not raw.exists() or raw.stat().st_size < 1024:
                t0 = time.time()
                if provider == "kokoro":
                    _kokoro_chunk(chunk_text, voice_id, raw, speed=speed)
                else:
                    _f5_chunk(
                        chunk_text, voice_id, ref_audio_text, raw,
                        speed=speed, provider=provider,
                    )
                print(f"[tts] chunk {i:04d}/{len(chunks)-1}: "
                      f"{len(chunk_text)} chars in {time.time()-t0:.1f}s")
                synthed_this_run += 1
                # Periodic Metal heap flush (no-op for cloud path).
                if _has_mlx and synthed_this_run % MLX_FLUSH_EVERY == 0 and not is_cloud:
                    try:
                        if hasattr(_mx, "clear_cache"):
                            _mx.clear_cache()
                        elif hasattr(_mx, "metal") and hasattr(_mx.metal, "clear_cache"):
                            _mx.metal.clear_cache()
                        print(f"[mem] flushed Metal cache after {synthed_this_run} chunks")
                    except Exception as _e:
                        print(f"[mem] flush failed: {_e}")
            if abs(atempo - 1.0) < 1e-3:
                shutil.copy2(raw, stretched)
            else:
                _atempo(raw, stretched, atempo)

    narration_wav = cache_dir / "narration.wav"
    _wav_concat_with_silence(final_chunks, join_silence_s, narration_wav)
    return narration_wav, final_chunks


# ---------- video stage: trim + 16:9 letterbox + concat -------------------


def _probe_duration(path: Path) -> float:
    # Backward-compat shim over the canonical helper in
    # ``pipeline.probe``. New callers should import probe_duration
    # directly; this module-local alias is kept so the existing
    # ``from pipeline.render.long_form import _probe_duration`` (used
    # by sports_doc.py and tests) keeps working unchanged.
    from pipeline.probe import probe_duration  # noqa: PLC0415
    return probe_duration(path)


def _trim_clip_letterbox(
    src: Path, in_s: float, out_s: float, out_path: Path,
    out_w: int = 1920, out_h: int = 1080, fps: int = 30,
    grade_filter: str | None = None,
) -> None:
    """Trim [in_s, out_s] from src, scale to fit 16:9 with blurred letterbox.

    For 4:3 sources (640x480, 320x240) this gives a centered scaled-up
    image with a blurred copy of the same frame filling the side bars —
    same aesthetic as the Shorts blurred-letterbox filter, just sideways.

    If grade_filter is set, it's appended after the overlay step — this is
    where the warm-firelight color grade lives (long_form_visual_signature.md).
    Single ffmpeg pass: grade applies to the composited 1920x1080 frame so
    both the foreground subject and the blurred letterbox bars share the
    same warm tone — keeps the lantern-lit feel consistent across letterboxed
    4:3 archival sources.

    Three-tier short-circuit (in order, first match wins):

    1. **Exact match + no grade** → ``-c:v copy`` stream-copy. Fastest;
       no re-encode at all.
    2. **Aspect match (within 1%) + no grade**, any source resolution
       → plain ``scale + setsar=1`` re-encode. Skips the
       ``split→gblur sigma=22→overlay`` chain entirely. The blurred
       letterbox is a visual no-op when the source already covers the
       output canvas; running gblur on every frame just to throw the
       result away wastes 10–40 min on 90-min renders with mixed-
       resolution 16:9 sources (1280×720 / 1440×1080 / 1920×1080
       reuploads of the same 16:9 documentary). Promoted to Tier 1
       in the 2026-05-05 efficiency overhaul.
    3. **Otherwise** (4:3 source, grade requested, or aspect mismatch)
       → full split+gblur+overlay chain. Required for letterboxing
       4:3 archival into 16:9 and for warm-firelight grading.
    """
    duration = max(0.1, out_s - in_s)
    src_w: int | None = None
    src_h: int | None = None
    try:
        probe = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "default=noprint_wrappers=1:nokey=1", str(src),
        ]).decode().strip().splitlines()
        src_w, src_h = int(probe[0]), int(probe[1])
    except (subprocess.CalledProcessError, ValueError, IndexError):
        pass  # fall through to full chain below

    if grade_filter is None and src_w and src_h:
        # Tier 1: exact match → stream copy (fastest).
        if src_w == out_w and src_h == out_h:
            print(f"[trim] aspect-match {src_w}x{src_h} == {out_w}x{out_h} — stream-copy")
            _ffmpeg([
                "-ss", f"{in_s}", "-t", f"{duration}", "-i", str(src),
                "-an", "-c:v", "copy", str(out_path),
            ])
            return
        # Tier 2: aspect match within 1% but different resolution → plain scale.
        # Skip the split+gblur+overlay chain entirely — it's a no-op when the
        # source already covers the output canvas.
        src_ratio = src_w / src_h
        out_ratio = out_w / out_h
        aspect_match = abs(src_ratio - out_ratio) / out_ratio < 0.01
        if aspect_match:
            print(f"[trim] aspect-match {src_w}x{src_h} ~ {out_w}x{out_h} — plain scale (no gblur)")
            vf = f"scale={out_w}:{out_h}:flags=lanczos,setsar=1,fps={fps},format=yuv420p"
            _ffmpeg([
                "-ss", f"{in_s}", "-t", f"{duration}", "-i", str(src),
                "-vf", vf, "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-threads", "3",
                "-pix_fmt", "yuv420p",
                str(out_path),
            ])
            return

    grade_tail = f",{grade_filter}" if grade_filter else ""
    vf = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},gblur=sigma=22[bg2];"
        f"[fg]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[fg2];"
        f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2,fps={fps}{grade_tail},format=yuv420p"
    )
    _ffmpeg([
        "-ss", f"{in_s}", "-t", f"{duration}", "-i", str(src),
        "-filter_complex", vf,
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-threads", "3",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ])


def build_image_panels_video(
    panels: list[dict[str, Any]],
    style_prefix: str,
    image_provider: str,
    image_seed: int,
    image_steps: int,
    image_width: int,
    image_height: int,
    cache_dir: Path,
    out_w: int = 1920,
    out_h: int = 1080,
    fps: int = 30,
    crossfade_s: float = 1.5,
    zoom_factor: float = 1.08,
) -> Path:
    """Path B render: comic-illustrated panels via Z-Image-Turbo + Ken Burns.

    For each panel:
      1. Generate the still via pipeline.images.generate (cached on disk).
      2. Render a slow Ken-Burns video segment — zoom from 1.00x to
         `zoom_factor` over the panel's `hold_s` duration.
      3. Concat all segments with `xfade` cross-fades of `crossfade_s`
         between adjacent panels.

    Each `panel` dict expects keys:
      - `scene` (str, required) — the descriptive scene prompt; the
        long-form image_style_prefix is appended automatically.
      - `hold_s` (float, optional, default 20) — how long to hold the panel.
      - `seed_offset` (int, optional, default panel_index) — added to the
        channel-level image_seed so each panel gets a distinct generation
        but the channel stays consistent.

    See learnings/long_form_visual_signature.md for the locked style prefix
    and per-panel scene authoring rules (must explicitly name a small warm
    light source — diffusion drops it otherwise).
    """
    from pipeline import images

    panel_dir = cache_dir / "panels"
    panel_dir.mkdir(parents=True, exist_ok=True)
    seg_dir = cache_dir / "panel_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    # Stage 1 — generate all panel stills (cached)
    panel_pngs: list[Path] = []
    for i, p in enumerate(panels):
        png = panel_dir / f"panel_{i:03d}.png"
        if not png.exists() or png.stat().st_size < 4096:
            scene = (p.get("scene") or "").strip()
            if not scene:
                raise ValueError(f"panel {i} missing 'scene' field")
            seed = int(image_seed) + int(p.get("seed_offset", i))
            print(f"[panel] {i+1}/{len(panels)} gen → {png.name} (seed {seed})")
            images.generate(
                prompt=scene,
                style_prefix=style_prefix,
                seed=seed,
                out_path=png,
                width=int(image_width),
                height=int(image_height),
                steps=int(image_steps),
                provider=image_provider,
            )
        panel_pngs.append(png)

    # Stage 2 — Ken-Burns segment per panel (cached).
    #
    # zoompan's `d=N` semantics: it re-runs the zoom cycle every N output
    # frames. Pairing `-loop 1 -t hold_s` (which produces a looping still
    # at the demuxer's default ~25fps) with `d=hold_s*fps` causes a multi-
    # plicative blow-up — the segment ends up ~150× too long. The fix is
    # to FIRST drive the looped still up to our target fps, THEN run
    # zoompan with `d=1` so it emits exactly one output frame per input
    # frame and ramps `zoom` once across the whole clip.
    seg_paths: list[tuple[Path, float]] = []
    for i, (png, p) in enumerate(zip(panel_pngs, panels)):
        hold_s = float(p.get("hold_s", 20))
        seg = seg_dir / f"seg_{i:03d}.mp4"
        if not seg.exists() or seg.stat().st_size < 4096:
            n_frames = max(1, int(round(hold_s * fps)))
            ramp = (zoom_factor - 1.0) / max(1, n_frames - 1)
            zp = (
                f"fps={fps},"
                f"scale={out_w*2}:{out_h*2}:flags=lanczos,"
                f"zoompan=z='min(zoom+{ramp:.6f},{zoom_factor:.4f})':"
                f"d=1:s={out_w}x{out_h}:fps={fps},format=yuv420p"
            )
            print(f"[seg ] {i+1}/{len(panels)} {hold_s:.1f}s zoom→{zoom_factor:.2f} ({n_frames}f) → {seg.name}")
            _ffmpeg([
                "-loop", "1", "-t", f"{hold_s}", "-i", str(png),
                "-filter_complex", zp,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", str(seg),
            ])
        seg_paths.append((seg, hold_s))

    # Stage 3 — xfade chain. With N segments we run N-1 transitions.
    video_path = cache_dir / "video.mp4"
    if len(seg_paths) == 1:
        _ffmpeg(["-i", str(seg_paths[0][0]), "-c", "copy", str(video_path)])
        return video_path

    inputs: list[str] = []
    for seg, _ in seg_paths:
        inputs += ["-i", str(seg)]

    flt_parts: list[str] = []
    cur_label = "[0:v]"
    cumtime = seg_paths[0][1] - crossfade_s
    for i in range(1, len(seg_paths)):
        out_label = f"[v{i}]"
        flt_parts.append(
            f"{cur_label}[{i}:v]xfade=transition=fade:"
            f"duration={crossfade_s:.3f}:offset={cumtime:.3f}{out_label}"
        )
        cur_label = out_label
        cumtime += seg_paths[i][1] - crossfade_s
    flt = ";".join(flt_parts)

    print(f"[xfade] {len(seg_paths)} panels → {video_path.name} (crossfade={crossfade_s}s)")
    _ffmpeg(inputs + [
        "-filter_complex", flt,
        "-map", cur_label,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", str(video_path),
    ])
    return video_path


def build_video_track(
    shotlist: dict[str, Any],
    sources_dir: Path,
    cache_dir: Path,
    target_duration_s: float,
    out_w: int = 1920,
    out_h: int = 1080,
    fps: int = 30,
    grade_filter: str | None = None,
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
    pending_jobs: list = []
    for i, clip in enumerate(clips):
        src = sources_dir / clip["source"]
        if not src.exists():
            raise FileNotFoundError(f"shotlist clip {i} source missing: {src}")
        out = long_clip_dir / f"clip_{i:03d}.mp4"
        clip_paths.append(out)
        if out.exists() and out.stat().st_size > 1024:
            continue

        # Capture loop vars in default args so each closure binds its own values.
        def _job(src=src, in_s=float(clip["in_s"]), out_s=float(clip["out_s"]),
                 out=out, idx=i, total=len(clips)):
            print(f"[trim] {idx+1}/{total} {in_s:.1f}-{out_s:.1f}s of {src.name}")
            _trim_clip_letterbox(src, in_s, out_s, out, out_w, out_h, fps, grade_filter)

        pending_jobs.append(_job)

    if pending_jobs:
        from pipeline.parallel import run_parallel
        run_parallel(pending_jobs, label="trim")

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
        if not sent:  # pragma: no cover — sentence list is built from non-empty cur groups
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
    text_color: tuple = (255, 217, 61, 255),  # warm yellow #FFD93D
    italic: bool = True,
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

    # Pick an italic sans-serif font when italic=True (matches the Sleepy
    # Time History yellow-italic caption signature). Fall back through a
    # chain of system italic faces, then to plain Helvetica, then PIL default.
    italic_paths = [
        "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf",
        "/Library/Fonts/Arial Italic.ttf",
    ]
    plain_paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Avenir.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    font_paths = (italic_paths + plain_paths) if italic else plain_paths
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
        if not sent:  # pragma: no cover — sentence list is built from non-empty cur groups
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
                _render_caption_png(ch, png, canvas_w=canvas_w, font=font, text_color=text_color)
            cues.append((png, cs, ce))
    print(f"[cap] {len(cues)} sentence PNGs (cached: {sum(1 for c in cues if c[0].exists())})")
    return cues


def build_caption_pngs_from_chunks(
    narration_text: str,
    chunk_wavs: list[Path],
    join_silence_s: float,
    out_dir: Path,
    canvas_w: int = 1920,
    max_chars_per_line: int = 70,
    max_lines_per_cue: int = 2,
    text_color: tuple = (255, 217, 61, 255),
    italic: bool = True,
    chunk_target_chars: int = 380,
) -> list[tuple[Path, float, float]]:
    """Generate sentence-level caption PNGs from the AUTHORED narration text,
    timed against the cached TTS chunk durations.

    Why this exists: the whisper-aligned variant (build_caption_pngs) re-
    transcribes the rendered audio, which (a) drops case + punctuation, (b)
    mistranscribes proper nouns (Bar-le-Duc → barladuk), and (c) hallucinates
    sentences during quiet stretches. We have the canonical narration text and
    the per-chunk audio — that's a perfect time anchor for forced alignment
    without running whisper at all.

    Algorithm:
      1. Re-split the authored narration into chunks using the same algorithm
         as the TTS stage (_split_into_chunks). Must produce exactly len(chunk_wavs).
      2. ffprobe each chunk wav to get its post-atempo duration.
      3. Each chunk i starts at sum(prev durations) + i * join_silence_s.
      4. Inside each chunk, split its text on .!? into sentences, then
         distribute time proportionally to character count.
      5. Render each sentence to a PNG (cached idempotently).
    """
    import re as _re
    from PIL import ImageFont

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[cap] authored-aligning narration ({len(chunk_wavs)} chunks)…")

    chunk_texts = _split_into_chunks(narration_text, target_chars=chunk_target_chars)
    if len(chunk_texts) != len(chunk_wavs):
        raise RuntimeError(
            f"chunk count mismatch: authored split → {len(chunk_texts)} chunks, "
            f"cached wav count → {len(chunk_wavs)}. Caption alignment requires "
            f"the same chunk_target_chars used during TTS synth."
        )

    # Probe durations once per chunk (cached lookup is fast, ~1s for 200 chunks).
    chunk_durs: list[float] = [_probe_duration(p) for p in chunk_wavs]

    # Pick font. Same chain as build_caption_pngs.
    italic_paths = [
        "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf",
        "/Library/Fonts/Arial Italic.ttf",
    ]
    plain_paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Avenir.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    font_paths = (italic_paths + plain_paths) if italic else plain_paths
    font: ImageFont.FreeTypeFont | None = None
    for fp in font_paths:
        if Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, 38)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    # Build the cue manifest first (cheap, sequential — timing math + text wrap)
    # then fan out the PIL renders. Decoupling lets us cap the worker pool
    # independently of cue count; PIL releases the GIL inside its C draw
    # routines so a ThreadPool gets near-linear speedup on N cores.
    cues: list[tuple[Path, float, float]] = []
    cue_idx = 0
    chunk_start = 0.0
    pending_renders: list = []
    for ci, (ctext, cdur) in enumerate(zip(chunk_texts, chunk_durs)):
        # Chunk text → sentences. Keep punctuation. Empty paragraphs collapse.
        sentences = _re.findall(r"[^.!?]+[.!?]+(?:\s|$)|\S[^.!?]*$", ctext)
        sentences = [s.strip() for s in sentences if s.strip()]
        if not sentences:
            chunk_start += cdur + join_silence_s
            continue
        total_chars = sum(len(s) for s in sentences) or 1
        # Distribute time within chunk proportionally to character count.
        cursor = chunk_start
        for sent in sentences:
            frac = len(sent) / total_chars
            sent_dur = cdur * frac
            sent_start = cursor
            sent_end = cursor + sent_dur
            cursor = sent_end

            # Soft-wrap to <= max_chars_per_line.
            wrapped: list[str] = []
            cur_line = ""
            for tok in sent.split():
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
            sub_chunks = [wrapped[i:i+max_lines_per_cue] for i in range(0, len(wrapped), max_lines_per_cue)]
            per = sent_dur / max(1, len(sub_chunks))
            for j, ch in enumerate(sub_chunks):
                cs = sent_start + j * per
                ce = cs + per
                png = out_dir / f"cap_{cue_idx:04d}_{j}.png"
                cues.append((png, cs, ce))
                if not png.exists():
                    def _job(ch=ch, png=png):
                        _render_caption_png(ch, png, canvas_w=canvas_w, font=font, text_color=text_color)
                    pending_renders.append(_job)
            cue_idx += 1
        chunk_start += cdur + join_silence_s

    if pending_renders:
        # PIL is fast (~2-3 ms per PNG). Use a wider pool than for ffmpeg.
        from pipeline.parallel import run_parallel
        run_parallel(pending_renders, max_workers=8, label="caption-png")

    print(f"[cap] {len(cues)} authored sentence PNGs across {len(chunk_wavs)} chunks")
    return cues


def render_watermark_png(
    text: str,
    out_path: Path,
    font_size: int = 28,
    text_color: tuple = (255, 255, 255, 140),  # ~55% opacity white
    italic: bool = False,
) -> Path:
    """Generate a transparent PNG of the channel name for top-right overlay.

    Match the Sleepy Time History watermark spec: faint white sans-serif in
    the top-right corner of every frame, low opacity so it doesn't dominate.
    Cached at <branding_dir>/watermark_topright.png — re-render only when the
    file is missing.
    """
    from PIL import Image, ImageDraw, ImageFont

    italic_paths = [
        "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf",
    ]
    plain_paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Avenir.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    font_paths = (italic_paths + plain_paths) if italic else plain_paths
    font: ImageFont.FreeTypeFont | None = None
    for fp in font_paths:
        if Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, font_size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    dummy = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    bb = ImageDraw.Draw(dummy).textbbox((0, 0), text, font=font)
    w = bb[2] - bb[0] + 6
    h = bb[3] - bb[1] + 6
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((-bb[0] + 3, -bb[1] + 3), text, font=font, fill=text_color)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))
    return out_path


def _render_caption_png(
    lines: list[str],
    out_path: Path,
    canvas_w: int,
    font,
    text_color=(255, 217, 61, 255),  # warm yellow #FFD93D
    shadow_color=(0, 0, 0, 220),
    shadow_offset=(2, 3),
    line_spacing: int = 8,
    pad_y: int = 12,
) -> None:
    """Render one cue (1-2 lines) as a transparent PNG, sleep-history styling.

    Yellow italic text (matches Sleepy Time History caption signature) with
    a subtle drop shadow for legibility on busy/light backgrounds. PNG is
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


def _ass_escape(text: str) -> str:
    r"""Escape characters that have special meaning inside ASS Dialogue text.

    libass treats ``{...}`` as override blocks (e.g. ``{\an8}``) and ``\``
    as the escape prefix. Apostrophes / em-dashes / accented chars are
    plain UTF-8 and pass through unchanged.

    We also fold any literal newlines/CRs into ``\N`` (ASS line break)
    to avoid breaking the Dialogue line format.
    """
    if not text:
        return ""
    return (
        text
        .replace("\\", r"\\")     # backslash first
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", r"\N")
        .replace("\n", r"\N")
        .replace("\r", r"\N")
    )


def _ass_color_from_rgba(rgba: tuple | list, alpha_override: int | None = None) -> str:
    """Convert an (R, G, B[, A]) tuple on 0-255 into ASS ``&HAABBGGRR``.

    Channel ``caption_style.text_rgba`` uses the standard PIL/web order
    (R, G, B, A). ASS uses the Windows BGR order with an alpha byte where
    **0 = fully opaque, 255 = fully transparent** (inverse of the usual
    convention — easy to get wrong).

    ``alpha_override``: if set, use this alpha (0-255) instead of the
    rgba's alpha channel. Used to emit ``OutlineColour`` / ``BackColour``
    with full opacity regardless of caller's alpha.
    """
    if len(rgba) == 4:
        r, g, b, a = rgba
    else:
        r, g, b = rgba
        a = 255
    if alpha_override is not None:
        a = alpha_override
    # ASS alpha is inverted: 0=opaque, 255=transparent
    ass_alpha = 255 - max(0, min(255, int(a)))
    return f"&H{ass_alpha:02X}{int(b):02X}{int(g):02X}{int(r):02X}"


def build_captions_ass(
    out_ass: Path,
    *,
    narration_text: str | None = None,
    chunk_wavs: list[Path] | None = None,
    join_silence_s: float = 0.0,
    chunk_target_chars: int = 380,
    narration_wav: Path | None = None,
    text_color: tuple | list = (255, 217, 61, 255),
    italic: bool = True,
    font_name: str = "Helvetica",
    font_size: int = 38,
    margin_v: int = 80,
    max_chars: int = 70,
    max_lines: int = 2,
) -> tuple[Path, int]:
    """Build a libass-compatible ASS subtitle file for the long-form mux.

    Two alignment modes (mutually exclusive — pass exactly one set):

    1. **Authored** (default; pass ``narration_text`` + ``chunk_wavs``):
       reuse the same ``_split_into_chunks`` algorithm the TTS stage
       used, ffprobe each chunk wav for its post-atempo duration, then
       distribute time across sentences within each chunk by character
       count. Identical timing math to ``build_caption_pngs_from_chunks``
       so the look matches what the legacy PNG path produced — just
       rendered by libass as one input instead of N.
    2. **Whisper-aligned** (pass ``narration_wav`` only): re-transcribe
       the rendered narration with whisper and group words by
       sentence-ending punctuation. Legacy fallback for
       ``caption_align: whisper`` configs.

    Style is parameterised from the channel's ``long_form.caption_style``
    block (``text_rgba``, ``italic``). Defaults to **yellow italic** to
    match the Sleepy-Time-History caption signature (channel learning
    ``historyrecapped/learnings/long_form_captions.md``). Earlier
    hardcoded "off-white non-italic" was wrong — fixed 2026-05-05 in
    the rubber-duck-flagged style mismatch.

    Returns ``(out_ass, cue_count)``.
    """
    import re as _re

    # ---- alignment ---------------------------------------------------------
    # Build a list of (start_s, end_s, text) triples — sentence-level cues.
    cues_raw: list[tuple[float, float, str]] = []

    if narration_text is not None and chunk_wavs is not None:
        # Authored alignment (default).
        chunk_texts = _split_into_chunks(narration_text, target_chars=chunk_target_chars)
        if len(chunk_texts) != len(chunk_wavs):
            raise RuntimeError(
                f"chunk count mismatch: authored split → {len(chunk_texts)} chunks, "
                f"cached wav count → {len(chunk_wavs)}. Caption alignment requires "
                f"the same chunk_target_chars used during TTS synth."
            )
        chunk_durs = [_probe_duration(p) for p in chunk_wavs]
        chunk_start = 0.0
        for ctext, cdur in zip(chunk_texts, chunk_durs):
            sentences = _re.findall(r"[^.!?]+[.!?]+(?:\s|$)|\S[^.!?]*$", ctext)
            sentences = [s.strip() for s in sentences if s.strip()]
            if not sentences:
                chunk_start += cdur + join_silence_s
                continue
            total_chars = sum(len(s) for s in sentences) or 1
            cursor = chunk_start
            for sent in sentences:
                frac = len(sent) / total_chars
                sent_dur = cdur * frac
                cues_raw.append((cursor, cursor + sent_dur, sent))
                cursor += sent_dur
            chunk_start += cdur + join_silence_s
    elif narration_wav is not None:
        # Whisper alignment (legacy fallback).
        from pipeline import beats as _beats  # noqa: PLC0415
        print(f"[cap] whisper-aligning {narration_wav.name}…")
        words = _beats.transcribe_words(narration_wav)
        if not words:
            raise RuntimeError("whisper returned no words for caption alignment")
        sentences: list[list] = []
        cur: list = []
        for w in words:
            cur.append(w)
            wt = (w.text or "").strip()
            if wt and wt[-1] in ".!?":
                sentences.append(cur)
                cur = []
        if cur:
            sentences.append(cur)
        for sent in sentences:
            if not sent:  # pragma: no cover — sentence list is built from non-empty cur groups
                continue
            text = " ".join((w.text or "").strip() for w in sent).strip()
            text = _re.sub(r"\s+", " ", text)
            cues_raw.append((float(sent[0].start), float(sent[-1].end), text))
    else:
        raise ValueError(
            "build_captions_ass: pass either (narration_text + chunk_wavs) for "
            "authored alignment, or narration_wav for whisper alignment."
        )

    # ---- style block -------------------------------------------------------
    primary = _ass_color_from_rgba(text_color)
    # Outline / back / shadow always opaque black for readability.
    outline = _ass_color_from_rgba((0, 0, 0, 255))
    back = _ass_color_from_rgba((0, 0, 0, 255))
    italic_flag = 1 if italic else 0
    # BorderStyle=1 + Outline=1.5 + Shadow=2 = soft outline + drop shadow,
    # readable on warm-firelight-graded archival footage. Alignment=2 =
    # bottom-center (libass numpad layout).
    style_line = (
        f"Style: Default,{font_name},{int(font_size)},"
        f"{primary},{primary},{outline},{back},"
        f"0,{italic_flag},0,0,"
        f"100,100,1,0,1,1.5,2,"
        f"2,80,80,{int(margin_v)},1"
    )
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
        f"{style_line}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )

    # ---- events: wrap + emit -----------------------------------------------
    events: list[str] = []
    cue_idx = 0
    for start, end, text in cues_raw:
        # Soft-wrap to <= max_chars per line.
        wrapped: list[str] = []
        cur_line = ""
        for tok in text.split():
            if not cur_line:
                cur_line = tok
            elif len(cur_line) + 1 + len(tok) <= max_chars:
                cur_line = f"{cur_line} {tok}"
            else:
                wrapped.append(cur_line)
                cur_line = tok
        if cur_line:
            wrapped.append(cur_line)
        sub_chunks = [wrapped[i:i+max_lines] for i in range(0, len(wrapped), max_lines)]
        per = (end - start) / max(1, len(sub_chunks))
        for i, lines in enumerate(sub_chunks):
            cs = start + i * per
            ce = cs + per
            ass_text = "\\N".join(_ass_escape(line) for line in lines)
            events.append(
                f"Dialogue: 0,{_hms_ass(cs)},{_hms_ass(ce)},Default,,0,0,0,,{ass_text}"
            )
            cue_idx += 1

    out_ass.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    print(f"[cap] wrote {cue_idx} ASS cues → {out_ass.name}")
    return out_ass, cue_idx


def _ffmpeg_has_libass() -> bool:
    """Probe (once, memoised) whether the local ffmpeg has libass.

    Required for the ``subtitles=`` filter that renders the ASS file.
    Falls back to PNG-overlay path when False — keeps Tier-0-less
    deployments working.
    """
    cached = getattr(_ffmpeg_has_libass, "_cached", None)
    if cached is not None:
        return cached
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-h", "filter=subtitles"],
            stderr=subprocess.STDOUT, text=True, timeout=5,
        )
        ok = "Render text subtitles" in out or "libass" in out
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        ok = False
    _ffmpeg_has_libass._cached = ok  # type: ignore[attr-defined]
    return ok


# ---------- final mux: video + (narration + music) ------------------------


def final_mux(
    video_path: Path, narration_wav: Path, music_wav: Path,
    out_path: Path, narration_db: float = -6.0, music_db: float = -28.0,
    caption_cues: list[tuple[Path, float, float]] | None = None,
    margin_v: int = 80,
    watermark_png: Path | None = None,
    watermark_margin: int = 32,
    captions_ass: Path | None = None,
) -> Path:
    """Mix narration + music; mux against the silent video track.

    **Caption rendering** — three modes (mutually exclusive; pick the
    first that's set, in this order):

    1. ``captions_ass`` (preferred): a single libass ASS file is fed to
       ffmpeg's ``subtitles=`` filter as ONE input + ONE filter step,
       regardless of cue count. This is the OOM fix (2026-05-05). For
       a 90-min sleep video with 800 sentence cues, peak ffmpeg RAM
       drops from ~10 GB → ~1.5 GB. Requires the ffmpeg binary to have
       libass — probe with :func:`_ffmpeg_has_libass`.
    2. ``caption_cues`` (legacy fallback): N PNG inputs + N-deep
       overlay chain. Kept for environments without libass; do NOT
       pass this if ``captions_ass`` is set.
    3. None: no captions burned.

    If ``watermark_png`` is provided, it's overlaid TOP-RIGHT for the
    full duration (Sleepy Time History watermark style). Watermark and
    captions can be combined in any mode.
    """
    if captions_ass is not None and caption_cues:
        raise ValueError(
            "final_mux: pass either captions_ass OR caption_cues, not both"
        )

    a_flt = (
        f"[1:a]volume={narration_db}dB[narr];"
        f"[2:a]volume={music_db}dB[bed];"
        f"[narr][bed]amix=inputs=2:duration=first:dropout_transition=2[a]"
    )

    needs_filter = bool(caption_cues) or bool(watermark_png) or bool(captions_ass)
    if needs_filter:
        # Inputs ordering: 0=video, 1=narration, 2=music, 3=watermark (if any),
        # then each caption PNG follows (in legacy mode).
        v_chain_parts: list[str] = []
        cur_label = "[0:v]"
        next_input = 3

        # Watermark first — under captions if both present, but they don't
        # spatially overlap (watermark top-right, captions bottom-center).
        if watermark_png:
            wm_label = f"[{next_input}:v]"
            out_label = "[vwm]"
            ov = (
                f"{cur_label}{wm_label}overlay="
                f"x=W-w-{watermark_margin}:y={watermark_margin}"
                f"{out_label}"
            )
            v_chain_parts.append(ov)
            cur_label = out_label
            next_input += 1

        if captions_ass is not None:
            # Single libass step — fixed cost regardless of cue count.
            # Path needs ffmpeg-style escaping for the filter argument
            # (colons + backslashes break the filter parser).
            ass_path_str = str(captions_ass).replace("\\", "/").replace(":", "\\:")
            out_label = "[vass]"
            v_chain_parts.append(f"{cur_label}subtitles={ass_path_str}{out_label}")
            cur_label = out_label
        elif caption_cues:
            for i, (_png, cs, ce) in enumerate(caption_cues):
                in_label = f"[{next_input + i}:v]"
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
        if watermark_png:
            cmd += ["-i", str(watermark_png)]
        if caption_cues:
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


def _preflight_power_check() -> None:
    """Backward-compat shim. The implementation moved to
    :func:`pipeline.preflight.power_check` 2026-05-05 so the same guard
    can be used by every renderer (footage_only / shorts / sports_doc),
    not just long_form. Existing callers (and tests that patch this
    name) keep working unchanged.
    """
    from pipeline.preflight import power_check  # noqa: PLC0415

    power_check(label="long-form")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--tts-only", action="store_true",
                    help="synthesize narration but skip video build (for iterative dev)")
    ap.add_argument("--render-mode", choices=["archival_footage", "image_panels"], default=None,
                    help="override config.yaml long_form.render_mode for this run")
    ap.add_argument("--no-grade", action="store_true",
                    help="skip the visual_grade filter chain — lets the aspect-match "
                         "short-circuit stream-copy 1080p sources (avoids the long-clip crash)")
    args = ap.parse_args()

    # OTel render envelope: pushes RenderContext + opens
    # render.long_form parent span so every nested telemetry call
    # (TTS chunks, footage trims, compose, upload) inherits
    # channel/slug attrs.
    with obs.render_envelope(
        channel=args.channel,
        slug=args.slug,
        render_kind="long_form",
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

    _preflight_power_check()
    _load_env(REPO_ROOT)

    # Reset the cloud-image circuit breaker per-render. Even though
    # long-form sleep videos use archival footage (no cloud image
    # gen), the image_panels render mode does call into image-gen
    # providers — keep the reset so that path also gets a fresh
    # breaker state.
    from pipeline.images_cloudrun import reset_circuit_breaker  # noqa: PLC0415
    reset_circuit_breaker()

    # Single source of truth for per-slug paths. ``args.channel`` may be
    # a flat channel slug ("historyrecapped") or a compound
    # ``<channel>/<niche>`` form (rare for long-form, but supported via
    # ``RenderPaths.from_channel_dir``).
    from pipeline.paths import RenderPaths  # noqa: PLC0415
    paths = RenderPaths.from_channel_dir(args.channel, project_root=REPO_ROOT)
    channel_dir = paths.root  # backward-compat: subsequent code uses channel_dir

    config = yaml.safe_load(paths.config_yaml.read_text())
    narration_path = paths.narration_for(args.slug)
    if not narration_path.exists():
        raise SystemExit(f"missing narration: {narration_path}")
    script = json.loads(narration_path.read_text())

    cache_dir = paths.cache_for(args.slug)
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
    # Long-form supports the F5 family (local f5_tts MLX + cloudrun_f5)
    # AND cloudrun_chatterbox. Other tts_provider values are
    # hard-rejected so the run fails fast instead of silently picking a
    # stale path. (kokoro is accepted by synth_long_narration for the
    # separate sports_doc renderer, but the historyrecapped/cosmosdecoded
    # renderer entered through this main() rejects it.)
    #
    # 2026-05-06 cloud-first migration: Shorts flipped to
    #   cloudrun_chatterbox.
    # 2026-05-10 long-form cloud parity: cloudrun_chatterbox added here
    #   so cosmosdecoded + historyrecapped long-form can use the same
    #   canonical English service (ytfactory-tts-chatterbox); the legacy
    #   ytfactory-tts-f5 service was never deployed under
    #   ytfactory-prod-v2 / asia-southeast1.
    #
    # To add Higgs / CosyVoice / etc., extend synth_long_narration's
    # provider whitelist + the is_cloud check + the prewarm
    # _service_url(model=...) lookup in this module.
    provider = str(lf.get("tts_provider", "f5_tts"))
    _ALLOWED_LONGFORM_PROVIDERS = (
        "f5_tts", "cloudrun_f5", "cloudrun_chatterbox",
    )
    if provider not in _ALLOWED_LONGFORM_PROVIDERS:
        raise SystemExit(
            f"long-form tts_provider={provider!r} is not supported. "
            f"Allowed: {', '.join(_ALLOWED_LONGFORM_PROVIDERS)}. "
            "Set `long_form.tts_provider: cloudrun_chatterbox` for "
            "cloud-first English (the canonical pick post-2026-05-10) "
            "or `f5_tts` for laptop fallback. "
            "(Shorts can use any cloudrun_* provider; long-form is "
            "narrower because synth_long_narration's chunk + resume + "
            "atempo + fan-out path is wired only for these providers. "
            "To add Higgs / CosyVoice / etc., extend synth_long_narration "
            "in pipeline/render/long_form.py.)"
        )
    speed = float(lf.get("tts_speed", 0.80))
    chunk_target_chars = int(lf.get("tts_chunk_target_chars", 380))
    join_silence_s = float(lf.get("tts_chunk_join_silence_s", 0.4))
    ref_audio_text = lf.get("tts_ref_text")

    text = script.get("narration") or "\n\n".join(s["text"] for s in script.get("sections", []))
    if not text:
        raise SystemExit("narration JSON has neither 'narration' nor non-empty 'sections'")

    print(f"[1/5] chunked TTS via {provider} voice={voice_id} speed={speed} atempo={atempo}…")
    narration_wav, chunks = synth_long_narration(
        text=text,
        voice_id=voice_id,
        cache_dir=cache_dir,
        atempo=atempo,
        chunk_target_chars=chunk_target_chars,
        join_silence_s=join_silence_s,
        speed=speed,
        ref_audio_text=ref_audio_text,
        provider=provider,
    )
    from pipeline.probe import probe_duration  # noqa: PLC0415
    dur = probe_duration(narration_wav)
    print(f"[1/5] narration {len(chunks)} chunks → {narration_wav.name} {dur:.1f}s ({dur/60:.1f} min)")

    if args.tts_only:
        print("[done] --tts-only set; stopping after narration synth")
        return 0

    # 2026-05-05: drop F5-TTS-MLX (1.35 GB) at the renderer-stage boundary,
    # *before* the render_mode branch. Stage 1 is the only stage that needs
    # F5; both archival_footage (ffmpeg + libass) and image_panels
    # (z_image_turbo) need the unified-memory headroom F5 was holding. The
    # earlier code only freed F5 in the image_panels branch — leaving 1.35 GB
    # resident through 90 minutes of ffmpeg work in archival_footage runs.
    # That contributed to the 2026-05-04 / 2026-05-05 SIGABRT-on-Metal
    # crashes (see docs/long_form_model_inventory.md and the dual-save memory
    # entry feedback_f5_reset_at_renderer_boundary.md).
    from pipeline.preflight import reset_mlx_state  # noqa: PLC0415
    reset_mlx_state(drop_f5=True, label="long-form stage-1 TTS")

    # Stage 2 — video track. Two paths controlled by long_form.render_mode:
    #   "image_panels" (Path B) — comic-illustrated Z-Image-Turbo panels with
    #       Ken-Burns + cross-fade. Reads `panels: [{scene, hold_s, seed_offset}]`
    #       from the narration JSON. Default for new sleep episodes.
    #   "archival_footage" (Path A) — trim + letterbox + concat of source
    #       clips listed in shotlist/<slug>.json with the warm-firelight grade.
    #       Used for episodes that already have a curated shotlist.
    out_w, out_h = lf.get("output_resolution", [1920, 1080])
    fps = int(lf.get("output_fps", 30))
    render_mode = (args.render_mode or lf.get("render_mode") or "archival_footage").strip()

    # 2026-05-04: image_panels (z_image_turbo) caused Metal GPU command-buffer
    # timeouts on the 89-panel run for western-front-1914-1918-sleep. Root
    # causes (see docs/long_form_model_inventory.md):
    #   1. F5-TTS-MLX (1.35 GB) stayed resident from stage 1 → ~5 GB MLX state
    #      by the time image gen schedules its compute buffers — fixed above
    #      at the renderer-stage boundary so BOTH branches benefit (2026-05-05).
    #   2. Long pure-diffusion timelines fragment unified memory faster than
    #      Metal's watchdog allows
    #
    # Fix at engineering level (not a hard ban — image renders are still
    # available for limited / hybrid use):
    #   (a) Cap the panel count. Pure 89-panel runs are out; <= panel_max_count
    #       (configurable, default 24) is fine. Author shotlists for the rest.
    #   (b) F5 drop is now unconditional above, so this stage starts on a
    #       clean Metal heap regardless of render_mode.
    PANEL_HARD_CAP = int(lf.get("panel_max_count", 24))

    if render_mode == "image_panels":
        panels = script.get("panels") or []
        if not panels:
            raise SystemExit(
                f"render_mode=image_panels but narration JSON has no `panels` field.\n"
                "Author per-panel scene strings (see learnings/long_form_visual_signature.md)\n"
                "or switch render_mode to 'archival_footage'."
            )
        if len(panels) > PANEL_HARD_CAP:
            raise SystemExit(
                f"render_mode=image_panels with {len(panels)} panels exceeds "
                f"PANEL_HARD_CAP={PANEL_HARD_CAP}.\n"
                "Pure long-form image-panel runs hit Metal command-buffer "
                "timeouts (see docs/long_form_model_inventory.md). Use "
                "archival_footage for the bulk of the timeline and reserve "
                "image gen for chapter cards / hero shots. Raise "
                "long_form.panel_max_count in config.yaml only if you've "
                "verified the new ceiling on a test render."
            )
        # F5 already dropped at the renderer-stage boundary above; nothing
        # more to do here. Image gen starts on a clean Metal heap regardless
        # of which branch we're in.
        # Auto-pad hold_s if total panel duration < narration duration.
        total_panel_s = sum(float(p.get("hold_s", 20)) for p in panels)
        if total_panel_s < dur - 1.0:
            shortfall = dur - total_panel_s
            extra_per_panel = shortfall / len(panels)
            print(f"[2/5] panels total {total_panel_s:.1f}s < narration {dur:.1f}s "
                  f"— extending each by {extra_per_panel:.1f}s to fill")
            for p in panels:
                p["hold_s"] = float(p.get("hold_s", 20)) + extra_per_panel
        elif total_panel_s > dur + 1.0:
            # If panels exceed narration, scale them down proportionally so the
            # final mux's -shortest still trims to narration end.
            scale = dur / total_panel_s
            print(f"[2/5] panels total {total_panel_s:.1f}s > narration {dur:.1f}s "
                  f"— scaling holds by {scale:.3f}")
            for p in panels:
                p["hold_s"] = float(p.get("hold_s", 20)) * scale
        print(f"[2/5] image_panels: {len(panels)} panels → {out_w}x{out_h} {fps}fps "
              f"with Ken-Burns + cross-fade…")
        video_path = build_image_panels_video(
            panels=panels,
            style_prefix=lf.get("image_style_prefix", "").strip().replace("\n", " "),
            image_provider=lf.get("image_provider", "z_image_turbo"),
            image_seed=int(lf.get("image_seed", 1944)),
            image_steps=int(lf.get("image_steps", 4)),
            image_width=int(lf.get("image_width", 1344)),
            image_height=int(lf.get("image_height", 768)),
            cache_dir=cache_dir,
            out_w=out_w, out_h=out_h, fps=fps,
            crossfade_s=float(lf.get("panel_crossfade_s", 1.5)),
            zoom_factor=float(lf.get("panel_zoom_factor", 1.08)),
        )
    else:
        shotlist_path = paths.shotlist_for(args.slug)
        if not shotlist_path.exists():
            raise SystemExit(
                f"missing shotlist: {shotlist_path}\n"
                "Long-form needs a shotlist with `clips: [{source, in_s, out_s}, ...]`\n"
                "(or switch render_mode to 'image_panels')."
            )
        shotlist = json.loads(shotlist_path.read_text())
        # ``footage_dir`` is YAML-overridable (some channels store
        # long-form sources in unconventional dirs). Default points at
        # the canonical ``<channel>/footage/long_sources/`` location
        # exposed via ``paths.footage_long_sources`` for new code.
        sources_dir = channel_dir / lf.get("footage_dir", "footage/long_sources")
        grade_cfg = lf.get("visual_grade") or {}
        grade_filter = grade_cfg.get("filter") if grade_cfg.get("enabled") else None
        if args.no_grade:
            grade_filter = None
        grade_label = "warm-firelight grade" if grade_filter else "no grade"
        print(f"[2/5] archival_footage: trim {len(shotlist['clips'])} clips → "
              f"{out_w}x{out_h} {fps}fps blurred letterbox + {grade_label}…")
        video_path = build_video_track(
            shotlist=shotlist,
            sources_dir=sources_dir,
            cache_dir=cache_dir,
            target_duration_s=dur,
            out_w=out_w, out_h=out_h, fps=fps,
            grade_filter=grade_filter,
        )
    video_dur = _probe_duration(video_path)
    print(f"[2/5] video → {video_path.name} {video_dur:.1f}s")

    # Stage 3 — music bed (synthetic ambient placeholder until a curated wav is dropped in)
    # ``music`` is a channel-wide subdir (multiple slugs share the same
    # ambient track). Use paths.music for the canonical location.
    music_default = lf.get("music_bed_default", "aether-loop.wav")
    music_path = paths.music / music_default
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

    # Stage 4 — captions
    # Default path: build_captions_ass produces a single libass file consumed
    # via ffmpeg's `subtitles=` filter. Mux peak RAM stays ~1.5 GB regardless
    # of cue count — the legacy PNG-overlay path balloons to ~10 GB on long
    # sleep videos with 600+ sentence cues (italian-campaign had 617). When
    # libass isn't available (`ffmpeg-full` / `homebrew-ffmpeg/ffmpeg/ffmpeg`
    # not installed), falls back to PNG overlays automatically. See:
    # docs/long_form_model_inventory.md "Caption alignment" + the dual-save
    # memory entry feedback_long_form_captions_ass_path.md.
    #
    # Authored alignment uses canonical narration text + cached TTS chunk
    # durations; whisper alignment is the legacy fallback for
    # `caption_align: whisper` configs.
    caption_cues: list[tuple[Path, float, float]] | None = None
    captions_ass: Path | None = None
    cap_cue_count = 0
    if bool(lf.get("captions_enabled", False)):
        cap_dir = cache_dir / "captions"
        cap_dir.mkdir(parents=True, exist_ok=True)
        cap_style = lf.get("caption_style") or {}
        cap_color = tuple(cap_style.get("text_rgba", (255, 217, 61, 255)))
        cap_italic = bool(cap_style.get("italic", True))
        cap_font = str(cap_style.get("font_name", "Helvetica"))
        cap_size = int(cap_style.get("font_size", 38))
        cap_margin_v = int(cap_style.get("margin_v", 80))
        align_mode = str(lf.get("caption_align", "authored"))
        use_ass = _ffmpeg_has_libass()
        if not use_ass:
            print("[cap] libass not available in local ffmpeg — falling back to PNG-overlay path. "
                  "Install with `brew install homebrew-ffmpeg/ffmpeg/ffmpeg` for the 1-input ASS path.")
        if use_ass:
            ass_path = cap_dir / "captions.ass"
            if align_mode == "authored":
                _, cap_cue_count = build_captions_ass(
                    ass_path,
                    narration_text=text,
                    chunk_wavs=chunks,
                    join_silence_s=join_silence_s,
                    chunk_target_chars=chunk_target_chars,
                    text_color=cap_color,
                    italic=cap_italic,
                    font_name=cap_font,
                    font_size=cap_size,
                    margin_v=cap_margin_v,
                )
            else:
                _, cap_cue_count = build_captions_ass(
                    ass_path,
                    narration_wav=narration_wav,
                    text_color=cap_color,
                    italic=cap_italic,
                    font_name=cap_font,
                    font_size=cap_size,
                    margin_v=cap_margin_v,
                )
            captions_ass = ass_path
        else:
            # Legacy PNG-overlay fallback (no libass).
            if align_mode == "authored":
                if cap_dir.exists():
                    for old in cap_dir.glob("cap_*.png"):
                        old.unlink()
                caption_cues = build_caption_pngs_from_chunks(
                    narration_text=text,
                    chunk_wavs=chunks,
                    join_silence_s=join_silence_s,
                    out_dir=cap_dir,
                    text_color=cap_color,
                    italic=cap_italic,
                    chunk_target_chars=chunk_target_chars,
                )
            else:
                caption_cues = build_caption_pngs(
                    narration_wav, cap_dir,
                    text_color=cap_color, italic=cap_italic,
                )
            cap_cue_count = len(caption_cues)
    else:
        print("[cap] captions_enabled=false — skipping subtitle burn")

    # Stage 5 — final mux (asks are inline in narration; no separate stage)
    out_dir = channel_dir / "shorts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.slug}.mp4"
    nb = float(lf.get("audio_narration_db", -6.0))
    mb = float(lf.get("audio_music_bed_db", -28.0))

    # Channel watermark (top-right, faint white) — render once, cache in branding/.
    wm_cfg = lf.get("watermark") or {}
    wm_text = wm_cfg.get("text") or config.get("name", "").upper() or "HISTORY RECAPPED"
    wm_enabled = bool(wm_cfg.get("enabled", True))
    watermark_png: Path | None = None
    if wm_enabled:
        wm_path = channel_dir / "branding" / "watermark_topright.png"
        if not wm_path.exists():
            print(f"[wm ] rendering watermark '{wm_text}' → {wm_path.name}")
            render_watermark_png(
                wm_text, wm_path,
                font_size=int(wm_cfg.get("font_size", 28)),
                text_color=tuple(wm_cfg.get("text_rgba", (255, 255, 255, 140))),
            )
        watermark_png = wm_path

    cap_label = "no captions"
    if captions_ass is not None:
        cap_label = f"captions ({cap_cue_count} ASS cues, libass)"
    elif caption_cues:
        cap_label = f"captions ({len(caption_cues)} PNG cues)"
    print(f"[4/4] muxing video + (narration {nb:+.0f}dB + music {mb:+.0f}dB) "
          f"+ {cap_label}{' + watermark' if watermark_png else ''} → {out_path.name}…")
    final_mux(video_path, narration_wav, music_wav, out_path,
              narration_db=nb, music_db=mb,
              caption_cues=caption_cues,
              captions_ass=captions_ass,
              watermark_png=watermark_png,
              watermark_margin=int(wm_cfg.get("margin", 32)))
    final_dur = _probe_duration(out_path)
    final_size = out_path.stat().st_size // 1024 // 1024
    print(f"[done] {out_path} — {final_dur:.1f}s ({final_dur/60:.1f} min), {final_size} MB")
    return 0


def cli_main() -> int:
    """CLI entry point. Invoked by ``historyrecapped/scripts/render_long_form.py``."""
    return main()


if __name__ == "__main__":
    sys.exit(cli_main())
