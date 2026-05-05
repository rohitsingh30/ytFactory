"""Clone a protagonist's voice from a YouTube video for F5-TTS narration.

Pipeline (per slug):

    1. yt-dlp: pull the video's bestaudio into
       ``data/cache/voice_clones/<videoid>.<ext>`` (cached on videoid).
    2. ffmpeg: trim ``[start, start+duration]``, mono, 24 kHz, loudnorm,
       light denoise → ``data/intermediate/<channel>/voices/<slug>.wav``.
    3. ASR: transcribe that clip via ``pipeline.asr`` to produce the
       ``ref_text`` F5-TTS-MLX requires alongside the ref WAV.
    4. Persist ``data/intermediate/<channel>/voices/<slug>.json`` with
       ``{ref_wav, ref_text, source_url, start, duration}``.

Once that JSON exists, ``make_shorts.make_short`` picks it up and forces
``tts_provider=f5_tts`` for that slug — see _find_voice_path there.

The clip should be 5–15s of clean, single-speaker speech. yt-dlp + ffmpeg
do no speaker isolation; if the chosen window has music or a second voice
the cloned timbre will smear. Pick the window with --start/--duration.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL


from pipeline.paths import MODEL_CACHE_DIR


# Voice-clone WAVs are cross-channel (the same source video can produce
# a voice clone consumed by multiple channel renders). Lives under the
# cross-channel ML model cache.
CACHE_DIR = MODEL_CACHE_DIR / "voice_clones"


@dataclass
class ClonedVoice:
    ref_wav: Path
    ref_text: str
    voice_json: Path


def _download_audio(url: str) -> tuple[Path, str]:
    """Download bestaudio for ``url`` into CACHE_DIR. Returns (path, video_id)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    probe_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with YoutubeDL(probe_opts) as ydl:
        meta = ydl.extract_info(url, download=False)
    vid = meta["id"]

    existing = list(CACHE_DIR.glob(f"{vid}.*"))
    if existing:
        print(f"[cache] {existing[0].name} (skip download)")
        return existing[0], vid

    dl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": str(CACHE_DIR / "%(id)s.%(ext)s"),
    }
    print(f"[download] {meta.get('title','?')} ({vid})")
    with YoutubeDL(dl_opts) as ydl:
        ydl.download([url])
    files = list(CACHE_DIR.glob(f"{vid}.*"))
    if not files:
        raise RuntimeError(f"yt-dlp produced no audio file for {vid}")
    return files[0], vid


def _trim_and_clean(
    src: Path, dst: Path, start: float, duration: float
) -> None:
    """ffmpeg: trim, mono, 24kHz, loudnorm, mild denoise → dst (WAV)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    # afftdn = light spectral denoise; loudnorm = EBU R128 to -16 LUFS
    # which is what F5-TTS-MLX seems happiest with.
    af = "afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start}", "-t", f"{duration}",
        "-i", str(src),
        "-ac", "1", "-ar", "24000",
        "-af", af,
        str(dst),
    ]
    subprocess.run(cmd, check=True)


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
        data/intermediate/<channel>/voices/<slug>.{wav,json}
    so make_shorts auto-picks it up.

    Override (out_wav + out_json): writes to caller-chosen paths. Used by
    the web server, which keeps clones in data/cache/voice_clones/web/<id>/
    until a job binds them to a real channel/slug.
    """
    if not (5.0 <= duration <= 15.0):
        raise ValueError(
            f"duration={duration} outside F5-TTS's 5–15s sweet spot"
        )

    if out_wav is not None and out_json is not None:
        ref_wav = out_wav
        voice_json = out_json
    elif channel and slug:
        # Per the 2026-05-03 reorg, channel state lives at <repo_root>/<channel>/
        # directly (no more data/intermediate/ wrapper). `channel` may itself be
        # a compound path like "mystoriesanimated/reddit_amitheasshole".
        out_dir = Path(channel) / "voices"
        ref_wav = out_dir / f"{slug}.wav"
        voice_json = out_dir / f"{slug}.json"
    else:
        raise ValueError("pass either (channel, slug) or (out_wav, out_json)")

    src, vid = _download_audio(url)
    print(f"[trim] {src.name} [{start:.1f}..{start+duration:.1f}s] → {ref_wav}")
    _trim_and_clean(src, ref_wav, start=start, duration=duration)

    print(f"[asr] transcribing ref clip via {asr_provider}…")
    from pipeline import asr  # lazy: heavy mlx import
    result = asr.transcribe(ref_wav, provider=asr_provider)
    ref_text = (result.get("text") or "").strip()
    if not ref_text:
        raise RuntimeError(
            "ASR produced empty transcript — pick a clearer window with "
            "--start/--duration."
        )
    print(f"[asr] ref_text: {ref_text!r}")

    voice_json.write_text(json.dumps({
        "ref_wav": str(ref_wav),
        "ref_text": ref_text,
        "source_url": url,
        "source_video_id": vid,
        "start": start,
        "duration": duration,
    }, indent=2))
    print(f"[done] wrote {voice_json}")
    return ClonedVoice(ref_wav=ref_wav, ref_text=ref_text, voice_json=voice_json)
