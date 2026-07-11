"""Clone a protagonist's voice from a YouTube video for voice-cloned TTS.

Pipeline (per slug):

    1. Cloud Run yt-dlp: pull the video's bestaudio into
       ``data/cache/voice_clones/<videoid>.<ext>`` (cached on videoid).
    2. ffmpeg: trim ``[start, start+duration]``, mono, 24 kHz, loudnorm,
       light denoise → ``<channel>/voices/<slug>.wav`` (or caller's
       ``out_wav``).
    3. ASR: transcribe that clip via ``pipeline.audio.asr`` to produce
       the ``ref_text`` the voice-clone TTS providers (IndicF5 cloud,
       Chatterbox) require alongside the ref WAV.
    4. Persist ``<channel>/voices/<slug>.json`` with
       ``{ref_wav, ref_text, source_url, start, duration}``.

Once that JSON exists, ``make_shorts.make_short`` picks it up for that
slug — see _find_voice_path there.

The clip should be 5–15s of clean, single-speaker speech. Cloud yt-dlp +
ffmpeg do no speaker isolation; if the chosen window has music or a
second voice the cloned timbre will smear. Pick the window with
--start/--duration.

**Cloud-cutover (2026-05-09):** yt-dlp no longer runs on the laptop —
``_download_audio`` proxies to ``pipeline.footage.yt_dlp_cloudrun``
which hits the ``ytfactory-clone-video-worker`` Cloud Run service.
The cache layout (``CACHE_DIR / "<vid>.<ext>"``) is preserved so a
cache hit short-circuits the cloud call.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse


from pipeline.paths import MODEL_CACHE_DIR


# Voice-clone WAVs are cross-channel (the same source video can produce
# a voice clone consumed by multiple channel renders). Lives under the
# cross-channel ML model cache.
CACHE_DIR = MODEL_CACHE_DIR / "voice_clones"



def _track(
    event: str,
    *,
    category: str = "pipeline",
    success: bool = True,
    duration_ms: int | None = None,
    metadata: dict | None = None,
) -> None:
    try:
        from pipeline import observability as obs  # noqa: PLC0415

        obs.track(
            event,
            category=category,
            success=success,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )
    except Exception:  # noqa: BLE001
        pass


@dataclass
class ClonedVoice:
    ref_wav: Path
    ref_text: str
    voice_json: Path


# YouTube ID is exactly 11 characters from [A-Za-z0-9_-]. Pinning the
# length keeps the regex from matching arbitrary path segments on
# unrelated URLs.
_YT_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
             "music.youtube.com", "youtu.be"}


def _extract_video_id(url: str) -> str:
    """Pure-function YouTube video-id extractor.

    Recognised forms:

      - ``https://www.youtube.com/watch?v=<ID>`` (incl. extra params)
      - ``https://www.youtube.com/watch/<ID>`` (less common)
      - ``https://www.youtube.com/shorts/<ID>``
      - ``https://www.youtube.com/embed/<ID>``
      - ``https://youtu.be/<ID>``

    For any non-YouTube URL we return a stable ``url_<sha12>`` token so
    the cache key is still deterministic per URL — no yt-dlp probe
    needed just to figure out where to write the file.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return _stable_url_key(url)

    host = (parsed.netloc or "").lower()
    if host not in _YT_HOSTS:
        return _stable_url_key(url)

    # ``?v=<ID>`` — the canonical desktop form.
    if parsed.query:
        v = parse_qs(parsed.query).get("v")
        if v and _YT_ID_RE.fullmatch(v[0]):
            return v[0]

    # Path-style forms: /shorts/<ID>, /embed/<ID>, /watch/<ID>,
    # or /<ID> on the youtu.be short-link host.
    parts = [p for p in parsed.path.split("/") if p]
    for i, part in enumerate(parts):
        if _YT_ID_RE.fullmatch(part):
            return part
        # ``watch/<ID>`` — accept the next segment.
        if part in ("shorts", "embed", "watch") and i + 1 < len(parts):
            cand = parts[i + 1]
            if _YT_ID_RE.fullmatch(cand):
                return cand

    return _stable_url_key(url)


def _stable_url_key(url: str) -> str:
    """Cache key for non-YouTube URLs — first 12 hex chars of sha256."""
    h = hashlib.sha256(url.encode()).hexdigest()[:12]
    return f"url_{h}"


def _download_audio(url: str) -> tuple[Path, str]:
    """Download bestaudio for ``url`` into CACHE_DIR. Returns (path, video_id).

    Cache layout: ``CACHE_DIR/<video_id>.<ext>``. A cache hit (any file
    with the matching ``<video_id>.*`` glob) skips the cloud call. Cache
    miss proxies to ``pipeline.footage.yt_dlp_cloudrun.download`` which
    runs yt-dlp on the ``ytfactory-clone-video-worker`` Cloud Run
    service and hands back a downloaded path.

    Cloud failures are wrapped with the prefix ``"Cloud Run yt-dlp
    failed"`` so callers (and the web UI) can distinguish them from
    local I/O failures.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    vid = _extract_video_id(url)

    existing = list(CACHE_DIR.glob(f"{vid}.*"))
    if existing:
        print(f"[cache] {existing[0].name} (skip download)")
        _track(
            "voice_clone.download",
            category="decision",
            metadata={
                "source_video_id": vid,
                "chosen": "cache_hit",
                "path": str(existing[0]),
            },
        )
        return existing[0], vid

    # Resolve via ``sys.modules`` so test mocks of
    # ``pipeline.footage.yt_dlp_cloudrun`` (via
    # ``patch.dict("sys.modules", ...)``) are honoured. The
    # ``from pipeline.footage import yt_dlp_cloudrun`` idiom would
    # otherwise read the package attr and skip sys.modules.
    import importlib  # noqa: PLC0415
    import sys  # noqa: PLC0415
    yt_dlp_cloudrun = sys.modules.get(
        "pipeline.footage.yt_dlp_cloudrun"
    ) or importlib.import_module("pipeline.footage.yt_dlp_cloudrun")

    print(f"[download] cloud-yt-dlp → {vid}")
    out_template = CACHE_DIR / f"{vid}.%(ext)s"
    t0 = time.time()
    try:
        path = yt_dlp_cloudrun.download(
            url, output_path=out_template, audio_only=True, audio_ext="m4a",
        )
        _track(
            "voice_clone.download",
            category="http",
            duration_ms=int((time.time() - t0) * 1000),
            metadata={
                "source_video_id": vid,
                "chosen": "cloud_yt_dlp",
                "path": str(path),
                "bytes": path.stat().st_size if path.exists() else 0,
            },
        )
    except Exception as e:  # noqa: BLE001
        _track(
            "voice_clone.download",
            category="http",
            success=False,
            duration_ms=int((time.time() - t0) * 1000),
            metadata={
                "source_video_id": vid,
                "chosen": "cloud_yt_dlp",
                "error": f"{type(e).__name__}: {e}"[:500],
            },
        )
        # The cloud module's own error class has CloudRunYtDlpFailed; we
        # also catch generic RuntimeError so the test-mock path (which
        # uses bare RuntimeError) is honoured.
        raise RuntimeError(f"Cloud Run yt-dlp failed for {vid}: {e}") from e
    return path, vid


def _trim_and_clean(
    src: Path, dst: Path, start: float, duration: float
) -> None:
    """ffmpeg: trim, mono, 24kHz, loudnorm, mild denoise → dst (WAV)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    # afftdn = light spectral denoise; loudnorm = EBU R128 to -16 LUFS
    # which is what the voice-clone TTS providers expect for a ref clip.
    af = "afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start}", "-t", f"{duration}",
        "-i", str(src),
        "-ac", "1", "-ar", "24000",
        "-af", af,
        str(dst),
    ]
    t0 = time.time()
    try:
        subprocess.run(cmd, check=True)
        _track(
            "ffmpeg.call",
            category="ffmpeg",
            duration_ms=int((time.time() - t0) * 1000),
            metadata={
                "purpose": "voice_clone.trim_clean",
                "args": cmd,
                "exit_code": 0,
                "output_bytes": dst.stat().st_size if dst.exists() else 0,
            },
        )
    except subprocess.CalledProcessError as exc:
        _track(
            "ffmpeg.call",
            category="ffmpeg",
            success=False,
            duration_ms=int((time.time() - t0) * 1000),
            metadata={
                "purpose": "voice_clone.trim_clean",
                "args": cmd,
                "exit_code": exc.returncode,
            },
        )
        raise
    except Exception as exc:  # noqa: BLE001
        _track(
            "ffmpeg.call",
            category="ffmpeg",
            success=False,
            duration_ms=int((time.time() - t0) * 1000),
            metadata={
                "purpose": "voice_clone.trim_clean",
                "args": cmd,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            },
        )
        raise


def clone_from_youtube(
    url: str,
    *,
    channel: str | None = None,
    slug: str | None = None,
    start: float = 0.0,
    duration: float = 10.0,
    asr_provider: str = "whisper_mlx",
    out_wav: Path | None = None,
    out_json: Path | None = None,
) -> ClonedVoice:
    """Produce a voice ref + transcript from a YouTube URL.

    Default (channel + slug): writes to
        <channel>/voices/<slug>.{wav,json}
    so ``make_shorts`` auto-picks it up. ``channel`` may be a compound
    path like ``"mystoriesanimated/reddit_amitheasshole"``.

    Override (out_wav + out_json): writes to caller-chosen paths. Used
    by the web server, which keeps clones in
    ``data/cache/voice_clones/web/<id>/`` until a job binds them to a
    real channel/slug.
    """
    if not (5.0 <= duration <= 15.0):
        raise ValueError(
            f"duration={duration} outside the 5–15s voice-clone ref window"
        )

    if out_wav is not None and out_json is not None:
        ref_wav = out_wav
        voice_json = out_json
    elif channel and slug:
        # Per the 2026-05-03 reorg, channel state lives at
        # <repo_root>/<channel>/ directly (no more data/intermediate/
        # wrapper). `channel` may itself be a compound path like
        # "mystoriesanimated/reddit_amitheasshole".
        out_dir = Path(channel) / "voices"
        ref_wav = out_dir / f"{slug}.wav"
        voice_json = out_dir / f"{slug}.json"
    else:
        raise ValueError("pass either (channel, slug) or (out_wav, out_json)")

    src, vid = _download_audio(url)
    _track(
        "voice_clone.chunk",
        metadata={
            "source_video_id": vid,
            "source_path": str(src),
            "start_s": start,
            "duration_s": duration,
            "ref_wav": str(ref_wav),
        },
    )
    print(f"[trim] {src.name} [{start:.1f}..{start+duration:.1f}s] → {ref_wav}")
    _trim_and_clean(src, ref_wav, start=start, duration=duration)

    print(f"[asr] transcribing ref clip via {asr_provider}…")
    # Resolve via ``sys.modules`` so test mocks of
    # ``pipeline.audio.asr`` (via ``patch.dict("sys.modules", ...)``)
    # are honoured even after the package's __dict__ has cached the
    # real submodule. ``from pipeline.audio import asr`` would read
    # the package attr and skip sys.modules.
    import importlib  # noqa: PLC0415
    import sys  # noqa: PLC0415
    asr = sys.modules.get("pipeline.audio.asr")
    if asr is None:
        asr = importlib.import_module("pipeline.audio.asr")

    t0 = time.time()
    try:
        result = asr.transcribe(ref_wav, provider=asr_provider)
    except Exception as exc:  # noqa: BLE001
        _track(
            "voice_clone.asr",
            category="asr",
            success=False,
            duration_ms=int((time.time() - t0) * 1000),
            metadata={
                "provider": asr_provider,
                "ref_wav": str(ref_wav),
                "error": f"{type(exc).__name__}: {exc}"[:500],
            },
        )
        raise
    ref_text = (result.get("text") or "").strip()
    _track(
        "voice_clone.asr",
        category="asr",
        success=bool(ref_text),
        duration_ms=int((time.time() - t0) * 1000),
        metadata={
            "provider": asr_provider,
            "ref_wav": str(ref_wav),
            "ref_text_chars": len(ref_text),
        },
    )
    if not ref_text:
        raise RuntimeError(
            "ASR produced empty transcript — pick a clearer window with "
            "--start/--duration."
        )
    print(f"[asr] ref_text: {ref_text!r}")

    voice_json.parent.mkdir(parents=True, exist_ok=True)
    voice_json.write_text(json.dumps({
        "ref_wav": str(ref_wav),
        "ref_text": ref_text,
        "source_url": url,
        "source_video_id": vid,
        "start": start,
        "duration": duration,
    }, indent=2))
    _track(
        "voice_clone.write_metadata",
        metadata={
            "voice_json": str(voice_json),
            "ref_wav": str(ref_wav),
            "source_video_id": vid,
        },
    )
    print(f"[done] wrote {voice_json}")
    return ClonedVoice(ref_wav=ref_wav, ref_text=ref_text, voice_json=voice_json)

