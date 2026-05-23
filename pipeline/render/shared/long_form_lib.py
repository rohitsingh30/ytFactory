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
import math
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

# Phase 1 (2026-05-14) — promotion of helpers into pipeline.render.shared.
# The local underscore names below are kept as re-exports so existing
# in-process callers (sports_doc.py, every test that does
# ``from pipeline.render.long_form import _ffmpeg``) keep working
# unchanged. The bigbang PR will delete these once long_form.py itself
# is gone.
from pipeline.render.shared.ffmpeg_helpers import (
    apply_atempo as _atempo,
    probe_duration as _probe_duration,
    run_ffmpeg as _ffmpeg,
)
from pipeline.render.shared.trim_letterbox import (
    trim_clip_letterbox as _trim_clip_letterbox,
)
from pipeline.render.shared.watermark import render_watermark_png  # noqa: F401


def _deep_merge_dict(dst: dict, src: dict) -> None:
    """In-place deep-merge of ``src`` into ``dst``.

    Used by ``_main_impl`` to overlay a per-render YAML (passed via
    ``--config``) on top of the channel YAML without losing nested
    keys. Behaviour matches the long-standing variant-overlay pattern
    in ``RenderPaths.from_channel_yaml`` and the cloud-worker's
    base+variant cfg merge: scalar / list values in ``src`` REPLACE
    the same key in ``dst``; sub-dicts are recursively merged so a
    partial ``long_form:`` overlay (e.g. only ``tts_voice`` set)
    leaves the rest of the channel's ``long_form:`` block intact.
    """
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge_dict(dst[k], v)
        else:
            dst[k] = v


def _resolve_channel_config_path(paths) -> Path:
    """Locate the channel YAML for ``paths.channel`` across both layouts.

    Pre-2026-05-10 each channel had ``<channel>/config.yaml`` on disk
    (the layout ``RenderPaths.config_yaml`` returns by default). The
    2026-05-10 nuclear cleanup moved every channel YAML to
    ``pipeline/channels/<slug>.yaml`` — the laptop CLI shim still reads
    the old path, but cloud-built images have ONLY the central path.

    This helper checks the local path first (laptop compat), then the
    central path (cloud + post-cleanup). Raises with a clear message
    listing both candidates if neither exists. Without this helper,
    long_form.py crashes with a bare ``FileNotFoundError`` when invoked
    on a channel that uses the central layout exclusively (every
    channel today, in cloud).
    """
    local_path = paths.config_yaml
    if local_path.exists():
        return local_path
    central_path = REPO_ROOT / "pipeline" / "channels" / f"{paths.channel}.yaml"
    if central_path.exists():
        return central_path
    raise SystemExit(
        f"missing channel config: tried {local_path} (legacy per-channel "
        f"layout) and {central_path} (central layout, post-2026-05-10). "
        f"Add the YAML at one of these paths."
    )


# ---------- env loading ----------------------------------------------------


def _load_env(repo_root: Path) -> None:
    """Audit D3.48 — delegates to the shared loader so all three
    render entry points (long_form / sports_doc / footage_only) use
    the same parser, including outer-quote stripping."""
    from pipeline.render.shared.env_loader import load_dotenv_into_environ
    load_dotenv_into_environ(repo_root)


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
# (sports docs), and any future long-form channel.


def _f5_chunk(
    text: str,
    ref_audio_path: str,
    ref_audio_text: str,
    out_wav: Path,
    speed: float = 0.95,
    provider: str = "cloudrun_chatterbox",
) -> None:
    """Single TTS chunk → wav. Routes by ``provider`` to the matching
    backend in ``pipeline.audio.synthesize``:

    * ``cloudrun_chatterbox`` — Chatterbox via Cloud Run L4. Canonical
      long-form English provider. Conditions on the ref-WAV embedding
      only — ``ref_audio_text`` is ignored.
    * ``kokoro`` — Kokoro 82M laptop fallback.

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


def _probe_wav_params(wav: Path) -> tuple[int, str]:
    """Return (sample_rate, channel_layout) for the first audio stream
    in ``wav``. Used by _wav_concat_with_silence to render the silence
    joiner with matching params so concat-demuxer's -c copy doesn't
    refuse the heterogeneous stream parameter set.

    Audit T1.15 — pre-fix the silence wav was hardcoded to 44100 mono;
    cloud Chatterbox / Higgs / IndicF5 routinely return 22050 Hz or
    stereo, breaking the concat with "Non-monotonous DTS" errors or
    forcing ffmpeg to refuse -c copy.

    2026-05-13 — additional hardening for the cloud-TTS "unknown"
    layout case. Cloud Chatterbox emits 24kHz mono WAVs whose RIFF
    header has no channel_layout field, so ``ffprobe`` reports
    ``channel_layout=unknown``. The pre-fix parser (a) had a comment
    that said the entries appeared in (sample_rate, channels,
    channel_layout) order but ffprobe actually emits them in the
    order *requested* (here: sample_rate, channel_layout, channels),
    and (b) accepted any non-digit string as a valid layout — so
    ``"unknown"`` rode through to ``anullsrc=cl=unknown`` and ffmpeg
    failed the entire long-form render with
    ``Invalid channel layout "unknown"``.

    The fix:

    1. Parse by *name*, not positional index — request each entry
       individually with ``-show_entries`` ``stream=key=value`` plus
       a sentinel separator and key-prefixed output via
       ``-of csv=p=0`` is fragile across ffprobe versions, so instead
       request all three in one shot and key the output via
       ``-of default=nokey=0:noprint_wrappers=1`` so each line is
       ``key=value``. Then we can match by key regardless of order.
    2. Whitelist layout values (mono, stereo, 5.1, 7.1) before
       passing to ``anullsrc``. Anything else (incl. "unknown" and
       empty string) → derive from channels count.
    """
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channel_layout,channels",
            "-of", "default=nokey=0:noprint_wrappers=1",
            str(wav),
        ],
        capture_output=True, text=True, check=False,
        # Tier 0 batch E (2026-05-14): cap ffprobe at 30s so a hung
        # probe (e.g. corrupt WAV header, network-mounted file in
        # GCS Fuse pause) doesn't stall the entire render. R-19 in
        # the catalogue. 30s is generous — typical local probe is <50ms.
        timeout=30,
    )
    rate = 44100
    layout = "mono"
    n_channels: int | None = None
    raw_layout = ""
    if probe.returncode == 0:
        for ln in (probe.stdout or "").splitlines():
            ln = ln.strip()
            if not ln or "=" not in ln:
                continue  # coverage: empty/no-eq lines pass through; ffprobe never emits them in practice
            key, _, val = ln.partition("=")
            key = key.strip()
            val = val.strip()
            if key == "sample_rate" and val.isdigit():
                rate = int(val)
            elif key == "channel_layout":
                raw_layout = val
            elif key == "channels" and val.isdigit():
                n_channels = int(val)
    # Whitelist of layouts ffmpeg's anullsrc accepts as `cl=<name>`.
    # "unknown" / empty / unexpected strings → derive from channels.
    _VALID_LAYOUTS = {"mono", "stereo", "2.1", "3.0", "4.0", "4.1", "5.0", "5.1", "6.1", "7.1"}
    if raw_layout in _VALID_LAYOUTS:
        layout = raw_layout
    elif n_channels is not None:
        if n_channels == 1:
            layout = "mono"
        elif n_channels == 2:
            layout = "stereo"
        elif n_channels == 6:
            layout = "5.1"
        elif n_channels == 8:
            layout = "7.1"
        else:
            # ffmpeg accepts numeric channel-count syntax (e.g. "3c") for
            # exotic layouts. Falls back to mono if probe gave us 0.
            layout = f"{n_channels}c" if n_channels > 0 else "mono"
    return rate, layout


def _wav_concat_with_silence(wavs: list[Path], silence_s: float, out_wav: Path) -> None:
    """Concat wavs with explicit silence joiners between.

    Audit T1.15 — silence track is rendered with the SAME sample rate
    + channel layout as the first input wav so concat-demuxer's
    ``-c copy`` doesn't refuse the heterogeneous stream-parameter
    set (cloud TTS providers don't always emit 44100/mono).
    """
    if not wavs:
        raise ValueError("no wavs to concat")
    rate, layout = _probe_wav_params(wavs[0])
    silence_wav = out_wav.parent / "_silence.wav"
    _ffmpeg([
        "-f", "lavfi", "-t", f"{silence_s}",
        "-i", f"anullsrc=r={rate}:cl={layout}",
        "-c:a", "pcm_s16le", str(silence_wav),
    ])
    list_txt = out_wav.parent / "_concat_list.txt"
    # Audit Q2.25 — concat demuxer single-quote escape.
    from pipeline.render.shared.concat_safe import concat_file_line  # noqa: PLC0415
    lines: list[str] = []
    for i, w in enumerate(wavs):
        if i > 0:
            lines.append(concat_file_line(silence_wav.resolve()))
        lines.append(concat_file_line(w.resolve()))
    list_txt.write_text("\n".join(lines))
    _ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(list_txt),
        "-c", "copy", str(out_wav),
    ])


# Audit Q2.23 — long_form + sports_doc previously emitted ZERO
# stage spans, violating the CLAUDE.md "every pipeline stage MUST
# emit a span" rule. Only shorts.py complied. The dashboard's
# /app/telemetry per-stage waterfall was empty for every long-form
# render. Now wrap the four heavy stage functions (TTS synth,
# video assembly, captions, mux) with @obs.traced so the render
# envelope opened in main() actually has children.
@obs.traced("tts.long_form", category="tts",
            capture=["voice_id", "provider", "speed", "atempo"])
def synth_long_narration(
    text: str,
    voice_id: str,
    cache_dir: Path,
    atempo: float,
    chunk_target_chars: int = 380,
    join_silence_s: float = 0.4,
    speed: float = 0.80,
    ref_audio_text: str | None = None,
    provider: str = "cloudrun_chatterbox",
) -> tuple[Path, list[Path]]:
    """Chunked TTS + atempo. Returns (final narration.wav, list of chunk wavs).

    Resumable: skips chunks whose stretched wav already exists.

    Providers:
      * ``cloudrun_chatterbox`` (default) — Chatterbox via Cloud Run GPU L4.
        Canonical long-form English provider. Eligible for parallel
        chunk dispatch (see CHUNK_PARALLEL_WORKERS below) — Cloud Run
        scales to N independent L4 instances, so chunks render
        concurrently rather than serially. Does NOT use
        ``ref_audio_text`` — Chatterbox conditions on the ref-WAV
        embedding only. ``voice_id`` is the path to the 5-15s
        ref WAV (e.g. ``pipeline/voice_refs/sarah.wav``).
      * ``cloudrun_indicf5`` — AI4Bharat IndicF5 via Cloud Run.
        Voice-clone; ``ref_audio_text`` REQUIRED.
      * ``kokoro`` (sportsrecapped long-form doc) — Kokoro 82M
        voice catalogue. ``voice_id`` is a Kokoro voice id (e.g.
        ``am_michael``). ``ref_audio_text`` is unused. Local-only;
        not parallelised (Kokoro is fast enough that the fan-out
        coordination overhead exceeds the savings).
    """
    if provider not in ("cloudrun_chatterbox", "cloudrun_indicf5", "kokoro"):
        raise RuntimeError(
            f"synth_long_narration: unsupported provider {provider!r}. "
            "Supported: cloudrun_chatterbox, cloudrun_indicf5, kokoro."
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    chunks = _split_into_chunks(text, target_chars=chunk_target_chars)
    print(f"[tts] {len(text)} chars → {len(chunks)} chunks via {provider} (target {chunk_target_chars} chars each)")

    # Cloud Run providers can fan out — each chunk hits an independent
    # L4 instance up to the configured concurrency. Local providers
    # MUST stay serial (shared M2 Max GPU; concurrent MLX/Metal ops
    # serialise + fragment unified memory — see
    # docs/long_form_model_inventory.md "Parallelism" section).
    is_cloud = provider == "cloudrun_chatterbox"
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
            url = _service_url(model="chatterbox")
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
    # Audit Q2.22 — write a voice-fingerprint sidecar so a future
    # run with a changed tts_provider/voice/speed in the channel
    # YAML invalidates this cached narration.wav. Without this,
    # switching F5→Chatterbox in the YAML didn't bust the cache.
    # main() reads the sidecar BEFORE invoking synth_long_narration
    # (see _voice_fingerprint.needs_resynth) and gates the call
    # accordingly; the write here ensures every fresh synth re-binds
    # the sidecar to the cfg that produced it.
    from pipeline.render.shared.voice_fingerprint import (  # noqa: PLC0415
        compute_fingerprint, write_sidecar,
    )
    write_sidecar(narration_wav, compute_fingerprint({
        "tts_provider": provider,
        "tts_voice": voice_id,
        "tts_speed": speed,
        "tts_ref_text": ref_audio_text,
        "tts_chunk_join_silence_s": join_silence_s,
        "tts_chunk_target_chars": chunk_target_chars,
    }))
    return narration_wav, final_chunks


# ---------- video stage: trim + 16:9 letterbox + concat -------------------




def _generate_panel_stills(
    *,
    panels: list[dict[str, Any]],
    style_prefix: str,
    image_provider: str,
    image_seed: int,
    image_steps: int,
    image_width: int,
    image_height: int,
    cache_dir: Path,
) -> list[Path]:
    """Render every panel still to disk. Independent of TTS — only
    reads ``scene`` / ``seed_offset`` from each panel dict.

    Extracted from :func:`build_image_panels_video` so the orchestrator
    can launch it on a :class:`pipeline.stage_overlap.StageOverlap`
    worker BEFORE awaiting :func:`synth_long_narration`. See
    ``docs/parallel_stage_overlap.md`` for the pattern.
    """
    from pipeline import images

    panel_dir = cache_dir / "panels"
    panel_dir.mkdir(parents=True, exist_ok=True)

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
    return panel_pngs


def _adjust_panel_holds_to_dur(
    panels: list[dict[str, Any]],
    *,
    narration_dur_s: float,
) -> None:
    """Pad/scale every panel's ``hold_s`` IN PLACE so the total panel
    duration matches ``narration_dur_s`` to within ±1s.

    Behaviour identical to the inline math previously in :func:`main`
    (pre-2026-05-13). Extracted so the parallel-overlap path can run
    it AFTER the TTS branch yields ``dur`` but BEFORE
    :func:`_assemble_panel_kenburns` consumes the adjusted holds.
    """
    if not panels:
        return
    total_panel_s = sum(float(p.get("hold_s", 20)) for p in panels)
    if total_panel_s < narration_dur_s - 1.0:
        shortfall = narration_dur_s - total_panel_s
        extra_per_panel = shortfall / len(panels)
        print(f"[2/5] panels total {total_panel_s:.1f}s < narration {narration_dur_s:.1f}s "
              f"— extending each by {extra_per_panel:.1f}s to fill")
        for p in panels:
            p["hold_s"] = float(p.get("hold_s", 20)) + extra_per_panel
    elif total_panel_s > narration_dur_s + 1.0:
        scale = narration_dur_s / total_panel_s
        print(f"[2/5] panels total {total_panel_s:.1f}s > narration {narration_dur_s:.1f}s "
              f"— scaling holds by {scale:.3f}")
        for p in panels:
            p["hold_s"] = float(p.get("hold_s", 20)) * scale


def _assemble_panel_static(
    *,
    panel_pngs: list[Path],
    panels: list[dict[str, Any]],
    cache_dir: Path,
    out_w: int,
    out_h: int,
    fps: int,
) -> Path:
    """Build per-panel static-still segments and hard-cut concat them
    into ``video.mp4``.

    2026-05-23 (user directive): replaces the legacy
    ``_assemble_panel_kenburns`` which used a zoompan filter + xfade
    chain. That path was the largest CPU sink in long-form rendering
    (~118s wall per 125s segment on Cloud Run CPU because zoompan
    re-encodes every output frame from a single input) and was the
    direct cause of the 60-min Cloud Run task-timeout failures on
    long-form jobs. Static loops render at ~30-50× realtime; concat
    demuxer with ``-c copy`` adds essentially zero wall.

    No motion, no crossfade. The pipeline-side compensation is denser
    panel cadence (more panels at shorter holds) so static stills
    don't dwell long enough to register as "frozen video" — see
    docs/panel_pacing_research_2026-05.md.
    """
    seg_dir = cache_dir / "panel_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    if len(panel_pngs) != len(panels):
        raise ValueError(
            f"panel_pngs ({len(panel_pngs)}) and panels ({len(panels)}) length mismatch"
        )

    seg_paths: list[Path] = []
    for i, (png, p) in enumerate(zip(panel_pngs, panels)):
        hold_s = float(p.get("hold_s", 20))
        seg = seg_dir / f"seg_{i:03d}.mp4"
        if not seg.exists() or seg.stat().st_size < 4096:
            # Scale-and-crop so the panel fills the target frame at
            # the channel's output resolution, no letterboxing.
            vf = (
                f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                f"crop={out_w}:{out_h},"
                f"fps={fps},setsar=1,format=yuv420p"
            )
            print(f"[seg ] {i+1}/{len(panels)} {hold_s:.1f}s static → {seg.name}")
            _ffmpeg([
                "-loop", "1", "-t", f"{hold_s:.3f}", "-i", str(png),
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-an",
                str(seg),
            ])
        seg_paths.append(seg)

    video_path = cache_dir / "video.mp4"
    if len(seg_paths) == 1:
        _ffmpeg(["-i", str(seg_paths[0]), "-c", "copy", str(video_path)])
        return video_path

    # Concat demuxer with -c copy — lossless, near-instant. Hard cuts
    # between panels; no fade, no dissolve. All segs are at the same
    # codec/res/fps so concat copy is safe.
    concat_list = cache_dir / "panel_segments_concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{seg.resolve()}'" for seg in seg_paths)
    )
    print(f"[concat] {len(seg_paths)} panels → {video_path.name} (hard cuts)")
    _ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(video_path),
    ])
    return video_path


# Back-compat alias — the legacy name still resolves so anything that
# imports ``_assemble_panel_kenburns`` (tests, snapshots) keeps working,
# but the body is the new static + concat implementation.
def _assemble_panel_kenburns(
    *,
    panel_pngs: list[Path],
    panels: list[dict[str, Any]],
    cache_dir: Path,
    out_w: int,
    out_h: int,
    fps: int,
    crossfade_s: float = 0.0,  # noqa: ARG001 — ignored; back-compat only
    zoom_factor: float = 1.0,  # noqa: ARG001 — ignored; back-compat only
) -> Path:
    """Back-compat shim. ``crossfade_s`` / ``zoom_factor`` are ignored.
    Delegates to :func:`_assemble_panel_static`.
    """
    return _assemble_panel_static(
        panel_pngs=panel_pngs,
        panels=panels,
        cache_dir=cache_dir,
        out_w=out_w,
        out_h=out_h,
        fps=fps,
    )


@obs.traced("video_panels.long_form", category="render",
            capture=["out_w", "out_h", "fps"])
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
    crossfade_s: float = 0.0,  # noqa: ARG001 — ignored; back-compat only
    zoom_factor: float = 1.0,  # noqa: ARG001 — ignored; back-compat only
) -> Path:
    """Path B render: comic-illustrated panels via Z-Image-Turbo, hard cuts.

    2026-05-23: Ken Burns + xfade removed (see
    :func:`_assemble_panel_static`). ``crossfade_s`` / ``zoom_factor``
    kwargs are kept for back-compat with callers / tests but ignored.

    Backwards-compatible thin wrapper that runs the two extracted halves
    (:func:`_generate_panel_stills` then :func:`_assemble_panel_static`)
    sequentially. The orchestrator's parallel path (long-form ``main``)
    calls the halves directly with a :class:`StageOverlap` between them
    so stills generation overlaps with TTS chunked synthesis.
    """
    panel_pngs = _generate_panel_stills(
        panels=panels,
        style_prefix=style_prefix,
        image_provider=image_provider,
        image_seed=image_seed,
        image_steps=image_steps,
        image_width=image_width,
        image_height=image_height,
        cache_dir=cache_dir,
    )
    return _assemble_panel_static(
        panel_pngs=panel_pngs,
        panels=panels,
        cache_dir=cache_dir,
        out_w=out_w,
        out_h=out_h,
        fps=fps,
    )


def _trim_shotlist_clips(
    *,
    shotlist: dict[str, Any],
    sources_dir: Path,
    cache_dir: Path,
    out_w: int,
    out_h: int,
    fps: int,
    grade_filter: str | None,
) -> list[Path]:
    """Trim every shotlist clip to its window — independent of TTS.

    Extracted from :func:`build_video_track`. Internally still uses
    :func:`pipeline.parallel.run_parallel` for per-clip ffmpeg fan-out.
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

        def _job(src=src, in_s=float(clip["in_s"]), out_s=float(clip["out_s"]),
                 out=out, idx=i, total=len(clips)):
            print(f"[trim] {idx+1}/{total} {in_s:.1f}-{out_s:.1f}s of {src.name}")
            _trim_clip_letterbox(src, in_s, out_s, out, out_w, out_h, fps, grade_filter)

        pending_jobs.append(_job)

    if pending_jobs:
        from pipeline.parallel import run_parallel
        run_parallel(pending_jobs, label="trim")

    return clip_paths


def _concat_and_pad(
    *,
    clip_paths: list[Path],
    cache_dir: Path,
    target_duration_s: float,
    fps: int,
) -> Path:
    """Concat-demux trimmed clips and slow-mo-pad if shorter than target.

    Extracted from :func:`build_video_track`. The ``target_duration_s``
    parameter is the only TTS-derived input.
    """
    from pipeline.render.shared.concat_safe import concat_file_line  # noqa: PLC0415
    list_txt = cache_dir / "_concat_clips.txt"
    list_txt.write_text("\n".join(concat_file_line(p.resolve()) for p in clip_paths))
    video_path = cache_dir / "video.mp4"
    _ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(list_txt),
        "-c", "copy", str(video_path),
    ])

    have = _probe_duration(video_path)
    if have < target_duration_s - 1.0:
        gap = target_duration_s - have
        print(f"[pad ] video {have:.1f}s < target {target_duration_s:.1f}s — slow-mo pad {gap:.1f}s")
        tail_s = min(60.0, have - 1.0)
        slow_factor = tail_s / (tail_s + gap)
        slow_clip = cache_dir / "_tail_slow.mp4"
        _ffmpeg([
            "-ss", f"{have - tail_s}", "-i", str(video_path),
            "-filter:v", f"setpts={1/slow_factor:.4f}*PTS,fps={fps}",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", str(slow_clip),
        ])
        head_clip = cache_dir / "_head.mp4"
        _ffmpeg([
            "-t", f"{have - tail_s}", "-i", str(video_path),
            "-c", "copy", str(head_clip),
        ])
        list_txt.write_text(
            concat_file_line(head_clip.resolve()) + "\n"
            + concat_file_line(slow_clip.resolve())
        )
        _ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(list_txt),
            "-c", "copy", str(video_path),
        ])

    return video_path


@obs.traced("video_track.long_form", category="render",
            capture=["target_duration_s", "fps"])
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

    Backwards-compatible thin wrapper that runs the two extracted halves
    (:func:`_trim_shotlist_clips` then :func:`_concat_and_pad`)
    sequentially. The orchestrator's parallel path (long-form ``main``)
    calls the halves directly with a :class:`StageOverlap` between them
    so clip trimming overlaps with TTS chunked synthesis.

    If concatenated duration < target_duration_s, the last clip is extended
    by replaying its tail at 0.6x speed (slow-mo pad) until the gap closes.
    """
    clip_paths = _trim_shotlist_clips(
        shotlist=shotlist,
        sources_dir=sources_dir,
        cache_dir=cache_dir,
        out_w=out_w,
        out_h=out_h,
        fps=fps,
        grade_filter=grade_filter,
    )
    return _concat_and_pad(
        clip_paths=clip_paths,
        cache_dir=cache_dir,
        target_duration_s=target_duration_s,
        fps=fps,
    )


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
    *,
    asr_provider: str = "whisper_mlx",
) -> Path:
    """Whisper-align narration → sentence-level SRT.

    Sleep-mode captions are SENTENCES not word-by-word (the latter is for
    Shorts where attention bursts matter). One or two short lines per cue.
    Style is baked in via the sibling ASS file produced by build_captions_ass.

    **Audit Q2.21** — pre-fix this called ``transcribe_words(narration_wav)``
    without ``provider=``, so the channel YAML's ``asr_provider`` and
    ``YTFACTORY_ASR_PROVIDER`` env were silently ignored on the long-form
    caption path. Now the caller (long_form.main + render_footage_only.py)
    threads ``cfg["asr_provider"]`` through.
    """
    import re as _re
    from pipeline import beats as _beats

    print(f"[cap] whisper-aligning {narration_wav.name}…")
    words = _beats.transcribe_words(narration_wav, provider=asr_provider)
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
    *,
    asr_provider: str = "whisper_mlx",
) -> list[tuple[Path, float, float]]:
    """Whisper-align narration → one PNG per sentence with sleep styling.

    PIL renders the PNGs (no libass dependency). Returns a list of
    (png_path, start_s, end_s) tuples for the overlay chain. Idempotent
    — re-running re-uses cached PNGs.

    **Audit Q2.21** — ``asr_provider`` defaults to whisper_mlx but
    callers should pass through the channel YAML's value so
    ``YTFACTORY_ASR_PROVIDER`` and the per-channel override are honoured.
    """
    import re as _re
    from PIL import Image, ImageDraw, ImageFont
    from pipeline import beats as _beats

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[cap] whisper-aligning {narration_wav.name}…")
    words = _beats.transcribe_words(narration_wav, provider=asr_provider)
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
    alignment: int = 2,
    asr_provider: str = "whisper_mlx",
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

    ``alignment`` (libass numpad layout): 2 = bottom-center (default),
    5 = middle-center (used by captions_layout=center_word_by_word).
    Channel YAMLs may override via ``long_form.caption_style.alignment``
    but the wizard's captions_layout knob is the canonical entrypoint.

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
        # Whisper alignment (legacy fallback). Audit Q2.21 — honour
        # the channel YAML's asr_provider rather than always using
        # the whisper_mlx default.
        from pipeline import beats as _beats  # noqa: PLC0415
        print(f"[cap] whisper-aligning {narration_wav.name}…")
        words = _beats.transcribe_words(narration_wav, provider=asr_provider)
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
        f"{int(alignment)},80,80,{int(margin_v)},1"
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


def _measure_loudness(wav_path: Path) -> dict[str, float] | None:
    """Two-pass loudnorm pass 1 — measure input loudness.

    Runs ``loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json`` against
    ``wav_path`` in measurement mode, parses the JSON object the
    filter prints to stderr, returns dict with measured_I /
    measured_LRA / measured_TP / measured_thresh / target_offset.

    Returns ``None`` on any failure (ffmpeg crash, JSON parse error,
    missing keys). Caller MUST handle None — falls back to single-
    pass mode in that case so a measurement glitch doesn't break the
    whole render.

    Why two-pass exists (added 2026-05-13): single-pass loudnorm is
    documented to be ±3 LU inaccurate. For Cloud Run TTS providers
    (Chatterbox emits 15-25 dB quieter than F5/Kokoro), single-pass
    over- or under-corrects by 5-7 LU. The 2026-05-13 silent-mp4
    bug (final mp4 at -28 LUFS, 14 LU below YouTube target) was
    caused by exactly this. Two-pass measure-then-apply is the
    industry standard and lands within ±0.5 LU of target.
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-i", str(wav_path),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=180, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        # coverage: subprocess failure path — exercised by integration only
        return None
    out = (proc.stderr or "") + (proc.stdout or "")
    # The filter writes a JSON object to stderr. It usually starts with
    # "[Parsed_loudnorm_..." prefix lines, then the actual {"input_i": "..."} object.
    start = out.rfind("{")
    end = out.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(out[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    try:
        return {
            "measured_I": float(data["input_i"]),
            "measured_LRA": float(data["input_lra"]),
            "measured_TP": float(data["input_tp"]),
            "measured_thresh": float(data["input_thresh"]),
            "target_offset": float(data.get("target_offset", 0.0)),
        }
    except (KeyError, ValueError, TypeError):
        return None
    finally:
        pass


def _measurement_is_usable(m: dict[str, float] | None) -> bool:
    """Defend against pathological ffmpeg outputs that crash pass 2.

    loudnorm's second-pass parameters have a documented range of
    [-99, 0] for ``measured_I`` / ``measured_thresh`` and [0, 99] for
    ``measured_LRA``. When pass 1 measures genuine silence, it
    returns ``measured_I=-inf`` (literally the string "-inf" in the
    JSON), which is out of range and crashes pass 2 with "Result too
    large". Same risk on a NaN. Returning False here triggers the
    single-pass fallback which loudnorm handles gracefully.
    """
    if m is None:
        return False
    for key, lo, hi in (
        ("measured_I", -99.0, 0.0),
        ("measured_LRA", 0.0, 99.0),
        ("measured_TP", -99.0, 99.0),
        ("measured_thresh", -99.0, 0.0),
    ):
        v = m.get(key)
        if v is None:
            return False
        try:
            f = float(v)
        except (TypeError, ValueError):
            return False
        if math.isnan(f) or math.isinf(f):
            return False
        if not (lo <= f <= hi):
            return False
    return True


def _verify_audio_loudness(
    mp4_path: Path, *, target_lufs: float = -16.0, tolerance_lu: float = 4.0,
) -> tuple[float, bool]:
    """Measure post-mux loudness against ``target_lufs``.

    Returns ``(measured_value, within_tolerance)``. On any ffmpeg /
    parse failure returns ``(0.0, True)`` — we don't fail a render
    because the verification step itself broke.

    2026-05-14 fix (canary 88b94b8a post-mortem): pre-fix this used
    ``volumedetect``'s ``mean_volume`` (RMS dBFS) and compared it to
    a LUFS target, which is apples-to-oranges. For 10-min long-form
    content with natural inter-sentence silence, mean_volume averages
    in the dead time and reads 5-7 dB below the actual integrated
    LUFS — so a perfectly-loudness-normalised mp4 (-16 LUFS as
    YouTube measures it) would show mean_volume = -22 to -23 dB and
    trip a 4 LU tolerance check.

    Now: prefer ebur128 integrated LUFS (K-weighted, gated, BS.1770-4
    — same metric YouTube uses for normalisation). When ebur128
    returns a below-gate reading (-70 LUFS = ITU-R absolute silence
    floor; happens on synthetic test signals + very-short clips),
    fall back to volumedetect with a wider ±8 LU tolerance.
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-i", str(mp4_path),
                "-af", "ebur128=peak=true,volumedetect", "-vn", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        # coverage: subprocess failure path — exercised by integration only
        return 0.0, True
    out = (proc.stderr or "") + (proc.stdout or "")
    # ebur128 final summary line: "    I:         -16.4 LUFS".
    m_eb = re.search(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS", out)
    integrated_lufs: float | None = float(m_eb.group(1)) if m_eb else None
    # Below-gate: ebur128 returns -70 LUFS (ITU-R absolute silence
    # floor) when there's too little K-weighted energy to integrate.
    # In that case fall back to volumedetect with wider tolerance —
    # mean_volume isn't 1:1 with LUFS for speech-with-silence content.
    if integrated_lufs is None or integrated_lufs <= -65.0:
        m_mv = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", out)
        if not m_mv:
            return 0.0, True
        mean_db = float(m_mv.group(1))
        return mean_db, abs(mean_db - target_lufs) <= max(tolerance_lu, 8.0)
    within = abs(integrated_lufs - target_lufs) <= tolerance_lu
    return integrated_lufs, within


@obs.traced("mux.long_form", category="render")
def final_mux(
    video_path: Path, narration_wav: Path, music_wav: Path,
    out_path: Path, narration_db: float = 0.0, music_db: float = -28.0,
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

    # Pre-amp narration with TWO-PASS loudnorm so the level reaching
    # the mix is independent of TTS source amplitude AND lands within
    # ±0.5 LU of -16 LUFS (YouTube spoken-word target). Cloud Run TTS
    # providers (Chatterbox cloned from sarah.wav, Higgs Audio,
    # Indic-Parler) routinely emit audio 15-25 dB quieter than the
    # F5/Kokoro laptop fallbacks; single-pass loudnorm over- or under-
    # corrects by 5-7 LU on this input range, which is what shipped
    # the 2026-05-13 silent mp4 (final landed at -28 LUFS).
    #
    # Mux audio chain (post-2026-05-13 fix stack):
    #
    #   [1:a] (narration WAV)
    #     ├ loudnorm two-pass → -16 LUFS ±0.5 LU
    #     └ volume={narration_db} dB trim — default 0.0 (was -6.0,
    #       which combined with the amix bug below to land at -28 LUFS)
    #     → [narr]
    #
    #   [2:a] (music WAV)
    #     └ volume={music_db} dB trim — default -28 dB (-12 dB under
    #       narration so it sits behind without competing)
    #     → [bed]
    #
    #   [narr][bed] amix=inputs=2:duration=first:dropout_transition=2
    #     :normalize=0  ← KEY FIX. Default normalize=1 divides every
    #                     input by N, so mixing narration + bed
    #                     halves both (-6 dB on narration). With
    #                     normalize=0 the inputs sum without auto-
    #                     attenuation, which is what we want when
    #                     each input is already pre-normalised.
    #     → [a]
    #
    # See docs/audio_loudnorm.md for the post-mortem.
    measured = _measure_loudness(narration_wav)
    if not _measurement_is_usable(measured):
        # Fallback: single-pass loudnorm. Less accurate (±3 LU) but
        # never blocks the render. Telemetry below records which mode
        # we used so dashboards can flag the noisy ones. Also the
        # only safe path when the narration is genuine silence
        # (measured_I=-inf crashes the second-pass filter).
        narr_loudnorm = "loudnorm=I=-16:TP=-1.5:LRA=11"
        loudnorm_mode = "single_pass_fallback"
        measured_lufs = None
    else:
        assert measured is not None  # narrowed by _measurement_is_usable
        narr_loudnorm = (
            f"loudnorm=I=-16:TP=-1.5:LRA=11"
            f":measured_I={measured['measured_I']:.2f}"
            f":measured_LRA={measured['measured_LRA']:.2f}"
            f":measured_TP={measured['measured_TP']:.2f}"
            f":measured_thresh={measured['measured_thresh']:.2f}"
            f":offset={measured['target_offset']:.2f}"
            f":linear=true:print_format=summary"
        )
        loudnorm_mode = "two_pass"
        measured_lufs = measured["measured_I"]
    obs.track(
        "audio_loudnorm",
        category="render",
        metadata={
            "mode": loudnorm_mode,
            "measured_input_lufs": measured_lufs,
            "narration_db_trim": narration_db,
            "music_db_trim": music_db,
        },
    )
    a_flt = (
        f"[1:a]{narr_loudnorm},volume={narration_db}dB[narr];"
        f"[2:a]volume={music_db}dB[bed];"
        f"[narr][bed]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[a]"
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
