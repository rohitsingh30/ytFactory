"""Cloud Run clone-video worker — CPU-only service that takes a video
URL and returns either:

  POST /analyze    — download + extract frames + Whisper transcript +
                     Azure GPT-vision niche fingerprint (all-in-one,
                     used by the website's clone-video flow).
  POST /download   — bare yt-dlp download. Streams the resulting file
                     back. Pipeline call sites (footage / voice_clone /
                     sports_doc / x_screenshot / youtube_video source)
                     use this so no laptop yt-dlp is needed in
                     production. Supports per-call format strings,
                     audio-only, sections (yt-dlp `--download-sections`),
                     and arbitrary extra yt-dlp args via `extra_args`.

  GET  /healthz    — readiness probe.

All three reuse the same yt-dlp invocation core which already has:
  * bgutil PO-token provider sidecar (Node, port 4416)
  * yt-jsc-youtubei JS challenge solver plugin
  * incognito-frozen cookies mounted from Secret Manager
  * read-only-secret workaround (copy to writable workspace)
  * subprocess.DEVNULL stdin (daemonized uvicorn parent has closed stdin)

Stateless: every request gets its own TempDir that's nuked at the end.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("clone-video-worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── OTel SDK boot ───────────────────────────────────────────────────
# Per CLAUDE.md "every new Cloud Run service MUST init OTel". The
# helper is COPY'd into the image by cloud/_shared/sync.sh +
# add_otel_copy.sh; importing it lights up Cloud Trace + Cloud
# Monitoring + Cloud Logging structured spans for every request +
# every outbound HTTP call this service makes.
try:
    from otel_init import (  # type: ignore[import-not-found]
        init as _otel_init,
        instrument_fastapi as _otel_instrument_fastapi,
        instrument_outbound_http as _otel_instrument_outbound,
    )
    _otel_init("clone-video-worker")
    _otel_instrument_outbound()
    _OTEL_OK = True
except Exception:
    _OTEL_OK = False

app = FastAPI(title="ytfactory-clone-video-worker", version="2")


if _OTEL_OK:
    _otel_instrument_fastapi(app)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "clone-video-worker"}


class AnalyzeRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2000)
    notes: str = Field("", max_length=2000)


# ---------------------------------------------------------------------------
# Subprocess wrappers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _ydl_download(video_url: str, target_dir: Path) -> Path:
    """Original `/analyze` flow — downloads video.<ext> to target_dir.

    Thin wrapper around ``_ydl_invoke`` for backward compat.
    """
    out_template = str(target_dir / "video.%(ext)s")
    _ydl_invoke(
        video_url,
        target_dir=target_dir,
        out_template=out_template,
        format_string="bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best",
        merge_to="mp4",
    )
    candidates = list(target_dir.glob("video.*"))
    mp4s = [p for p in candidates if p.suffix.lower() in (".mp4", ".mkv", ".webm")]
    if not mp4s:
        raise RuntimeError(f"yt-dlp produced no video file (candidates: {candidates})")
    return mp4s[0]


def _ydl_invoke(
    video_url: str,
    *,
    target_dir: Path,
    out_template: str,
    format_string: Optional[str] = None,
    merge_to: Optional[str] = None,
    audio_only: bool = False,
    extract_audio_format: str = "m4a",
    sections: Optional[list[str]] = None,
    extra_args: Optional[list[str]] = None,
    timeout_s: int = 240,
) -> None:
    """Run yt-dlp with the standard YouTube-bypass setup (bgutil PO,
    JSC plugin, incognito cookies). All callers go through here so the
    bot-evasion knobs stay consistent.

    Raises ``RuntimeError`` on failure with stderr tail attached.
    """
    # Cookies file dance — Secret Manager mounts read-only, yt-dlp
    # tries to update the cookie jar on success → OSError 30. Copy to
    # writable workspace; updates are nuked with the tempdir.
    cookies_src = os.environ.get("YT_DLP_COOKIES_FILE", "/secrets/cookies.txt")
    cookies_use: Optional[Path] = None
    if cookies_src and Path(cookies_src).is_file():
        cookies_use = target_dir / "cookies.txt"
        cookies_use.write_bytes(Path(cookies_src).read_bytes())

    cmd: list[str] = [
        "yt-dlp",
        "--no-playlist", "--no-warnings",
        "--verbose",
        "-o", out_template,
        # YouTube bot-detection workaround — see module docstring.
        "--extractor-args",
        f"youtubepot-bgutilhttp:base_url=http://127.0.0.1:{os.environ.get('PORT_BGUTIL', '4416')}",
        "--extractor-args",
        "youtube:player_client=default,tv,ios,mweb,android",
    ]
    if format_string:
        cmd += ["-f", format_string]
    if merge_to:
        cmd += ["--merge-output-format", merge_to]
    if audio_only:
        # -x ⇒ extract audio. Pair with --audio-format to control container.
        cmd += ["-x", "--audio-format", extract_audio_format]
    if sections:
        for s in sections:
            cmd += ["--download-sections", s]
    if extra_args:
        cmd += list(extra_args)
    if cookies_use is not None:
        cmd += ["--cookies", str(cookies_use)]
    cmd += [video_url]

    proc = _run(cmd, timeout=timeout_s)
    if proc.stderr:
        for line in proc.stderr.splitlines():
            print(f"[ytdlp] {line}", flush=True)
    if proc.returncode != 0:
        # Retry once with a permissive format if YouTube refused our
        # specific ladder (common on Shorts that only offer DASH).
        if (
            format_string
            and "Requested format is not available" in (proc.stderr or "")
        ):
            print("[ytdlp] retrying with permissive format=worst", flush=True)
            new_cmd: list[str] = []
            skip_next = False
            for c in cmd:
                if skip_next:
                    skip_next = False
                    continue
                if c == "-f":
                    skip_next = True
                    continue
                new_cmd.append(c)
            new_cmd.insert(-1, "-f")
            new_cmd.insert(-1, "worst[ext=mp4]/worst")
            proc = _run(new_cmd, timeout=timeout_s)
            if proc.stderr:
                for line in proc.stderr.splitlines():
                    print(f"[ytdlp-retry] {line}", flush=True)
        if proc.returncode != 0:
            err = (proc.stderr or "").strip() or (proc.stdout or "").strip()
            raise RuntimeError(f"yt-dlp failed: {err[:1000]}")


def _ffprobe_duration(path: Path) -> float:
    proc = _run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        timeout=30,
    )
    try:
        return float(proc.stdout.strip())
    except Exception:  # noqa: BLE001
        return 0.0


def _extract_frames(video: Path, target_dir: Path, count: int = 6) -> list[Path]:
    duration = _ffprobe_duration(video) or max(count, 10)
    out: list[Path] = []
    for i in range(count):
        t = duration * (0.05 + 0.9 * (i / max(count - 1, 1)))
        frame_path = target_dir / f"frame_{i + 1:02d}.jpg"
        proc = _run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{t:.2f}", "-i", str(video),
                "-frames:v", "1", "-q:v", "3",
                "-vf", "scale='min(1280,iw)':-2",
                str(frame_path),
            ],
            timeout=30,
        )
        if proc.returncode == 0 and frame_path.exists():
            out.append(frame_path)
    return out


def _extract_audio(video: Path, target_dir: Path) -> Optional[Path]:
    audio = target_dir / "audio.wav"
    proc = _run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video),
            "-ac", "1", "-ar", "16000", "-vn",
            str(audio),
        ],
        timeout=180,
    )
    if proc.returncode != 0 or not audio.exists():
        return None
    return audio


# ---------------------------------------------------------------------------
# Azure Whisper + GPT-vision
# ---------------------------------------------------------------------------


def _azure_whisper(audio: Path) -> Optional[str]:
    """Optional — only runs if AZURE_OPENAI_WHISPER_DEPLOYMENT (or
    short alias AZURE_OPENAI_WHISPER) is set. Honours optional separate
    Whisper endpoint (Whisper is in fewer Azure regions than chat
    completions)."""
    deployment = (
        os.environ.get("AZURE_OPENAI_WHISPER_DEPLOYMENT", "").strip()
        or os.environ.get("AZURE_OPENAI_WHISPER", "").strip()
    )
    if not deployment:
        return None
    endpoint = (
        os.environ.get("AZURE_OPENAI_WHISPER_ENDPOINT", "").strip()
        or os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    )
    api_key = (
        os.environ.get("AZURE_OPENAI_WHISPER_API_KEY", "").strip()
        or os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
    )
    api_version = (
        os.environ.get("AZURE_OPENAI_WHISPER_API_VERSION", "").strip()
        or os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")
    )
    if not endpoint or not api_key:
        return None

    try:
        from openai import AzureOpenAI

        client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
        with audio.open("rb") as fh:
            resp = client.audio.transcriptions.create(
                model=deployment,
                file=fh,
                response_format="text",
            )
        if isinstance(resp, str):
            return resp
        return getattr(resp, "text", None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Azure Whisper transcription failed: %s", exc)
        return None


_FINGERPRINT_SYSTEM_PROMPT = """You are a video-format reverse-engineer.
Given keyframes, audio transcript (when available), runtime, and user
notes, you produce a CONCRETE, REPRODUCIBLE niche spec the renderer
can consume.

Return ONE JSON object (no prose, no code fences) with EXACTLY these
fields:

  label                     short title for the niche, 2-60 chars
  description               2-sentence what-this-format-is, 80-280 chars
  prompt_style_guide        3-5 sentence voice/tone/structure guide
                            for the LLM that writes future scripts
  format                    one of: animated, text, cooking, footage,
                            split_screen, rhyme, footage_only, long_form,
                            sports_doc
  length_kind               "short" if total runtime <= 90s, else "long"
  source_kind               one of: reddit, wikipedia, manual, x_twitter,
                            youtube, rss   (best guess from content)
  source_ref                if you can identify a specific subreddit /
                            wiki page / channel, return it; else null
  hook_template             one-line hook pattern with {placeholders}
  closer_template           one-line closer / CTA pattern observed
  image_style               short visual aesthetic guide (<=200 chars)
  music_bed                 short descriptor or null

OUTPUT THE JSON OBJECT ONLY.
"""


def _azure_gpt_analyze(
    frames: list[Path],
    transcript: Optional[str],
    duration_s: float,
    user_notes: str,
) -> dict[str, Any]:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")
    model = os.environ.get("AZURE_OPENAI_MODEL", "gpt-4o-mini")
    if not endpoint or not api_key:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY not configured")

    from openai import AzureOpenAI

    client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)

    user_content: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            f"Runtime: {duration_s:.1f}s.\n"
            f"User notes: {user_notes or '(none)'}\n\n"
            + (f"Transcript:\n{transcript[:8000]}\n\n" if transcript else "Transcript: (unavailable)\n\n")
            + f"{len(frames)} keyframes follow."
        ),
    }]
    for f in frames:
        b64 = base64.b64encode(f.read_bytes()).decode("ascii")
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _FINGERPRINT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_completion_tokens=900,
        response_format={"type": "json_object"},
    )
    text = (resp.choices[0].message.content or "").strip()
    try:
        obj = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"LLM returned non-JSON: {text[:300]}") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"LLM JSON not an object: {type(obj).__name__}")
    return obj


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@app.post("/analyze")
def analyze(req: AnalyzeRequest) -> dict:
    log: list[str] = []

    def _log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S', time.gmtime())}] {msg}"
        log.append(line)
        logger.info(msg)

    with tempfile.TemporaryDirectory(prefix=f"clone-{uuid.uuid4().hex[:8]}-") as tmp:
        tmp_dir = Path(tmp)
        try:
            _log(f"Downloading {req.url}")
            video = _ydl_download(req.url, tmp_dir)
            duration = _ffprobe_duration(video)
            _log(f"Downloaded {video.name}, duration={duration:.1f}s")

            _log("Extracting keyframes + audio")
            frames = _extract_frames(video, tmp_dir, count=6)
            audio = _extract_audio(video, tmp_dir)
            _log(f"Extracted {len(frames)} frames, audio={'yes' if audio else 'no'}")

            transcript: Optional[str] = None
            if audio:
                _log("Transcribing via Azure Whisper (if configured)")
                transcript = _azure_whisper(audio)
                _log(f"Transcript length: {len(transcript) if transcript else 0} chars")

            _log(f"Analyzing via Azure GPT ({len(frames)} frames + {'transcript' if transcript else 'no transcript'})")
            fingerprint = _azure_gpt_analyze(frames, transcript, duration, req.notes)
            _log("Analysis complete")

            frames_b64 = [base64.b64encode(p.read_bytes()).decode("ascii") for p in frames]

            return {
                "fingerprint": fingerprint,
                "frames_b64": frames_b64,
                "duration_s": duration,
                "frames_count": len(frames),
                "has_transcript": bool(transcript),
                "transcript_chars": len(transcript) if transcript else 0,
                "log": log,
            }
        except subprocess.TimeoutExpired as exc:
            _log(f"Subprocess timeout: {exc.cmd[:1] if exc.cmd else exc}")
            raise HTTPException(504, f"timeout: {exc}")
        except RuntimeError as exc:
            _log(f"Pipeline error: {exc}")
            raise HTTPException(400, f"pipeline error: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("analyze failed")
            _log(f"Server error: {exc}")
            raise HTTPException(500, f"internal: {exc}")


# ---------------------------------------------------------------------------
# /download — bare yt-dlp file download
# ---------------------------------------------------------------------------


class DownloadRequest(BaseModel):
    """One yt-dlp invocation. The fields cover the 80% case so callers
    don't need to think about ``extra_args`` for typical work; the
    escape hatch is there for sports_doc + voice-clone edge cases.
    """

    url: str = Field(..., min_length=4, max_length=2000)
    # Format selector — yt-dlp ``-f`` value. Defaults to a sensible
    # ≤720p mp4 ladder. Pass ``"bestaudio"`` for audio-only or any
    # other valid yt-dlp format string for full control.
    format: Optional[str] = Field(
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best",
        max_length=400,
    )
    # When True yt-dlp is invoked with ``-x --audio-format <ext>`` and
    # the response container is whatever ``audio_ext`` says.
    audio_only: bool = False
    audio_ext: str = Field("m4a", pattern=r"^[a-z0-9]{2,5}$")
    merge_to: Optional[str] = Field("mp4", pattern=r"^[a-z0-9]{2,5}$")
    # yt-dlp ``--download-sections`` clauses, e.g. ``"*0:00-1:30"`` for
    # a 90s window. One entry per ``--download-sections`` flag.
    sections: Optional[list[str]] = None
    # Escape hatch — appended verbatim to the yt-dlp command line. Use
    # for ``--postprocessor-args``, ``--match-filter``, etc.
    extra_args: Optional[list[str]] = None
    timeout_s: int = Field(240, ge=30, le=900)


@app.post("/download")
def download(req: DownloadRequest) -> StreamingResponse:
    """Run yt-dlp and stream the resulting file back.

    The container guarantees:
      * bgutil PO-token provider is up (sidecar)
      * yt-jsc-youtubei JS challenge solver is installed
      * Cookies (incognito-frozen) are mounted and copied to writable
      * stdin DEVNULL'd to avoid the daemonized-uvicorn fd-9 trap

    Response: raw file bytes (single file). The ``Content-Disposition``
    header carries the filename.
    """
    # tempfile.TemporaryDirectory cleans up after the response finishes
    # streaming via FastAPI's BackgroundTask shim — but StreamingResponse
    # closes the iterator on completion so we use FileResponse via a
    # context manager pattern instead.
    tmp = tempfile.mkdtemp(prefix=f"ytdl-{uuid.uuid4().hex[:8]}-")
    target_dir = Path(tmp)
    out_template = str(target_dir / "out.%(ext)s")

    try:
        try:
            _ydl_invoke(
                req.url,
                target_dir=target_dir,
                out_template=out_template,
                format_string=req.format,
                merge_to=None if req.audio_only else req.merge_to,
                audio_only=req.audio_only,
                extract_audio_format=req.audio_ext,
                sections=req.sections,
                extra_args=req.extra_args,
                timeout_s=req.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, f"timeout: {exc}") from exc
        except RuntimeError as exc:
            raise HTTPException(400, f"yt-dlp error: {exc}") from exc

        # Pick the produced file. yt-dlp writes "out.<ext>" — exact ext
        # depends on format/merge_to/audio_only.
        produced = sorted(target_dir.glob("out.*"))
        if not produced:
            raise HTTPException(500, f"yt-dlp produced no file in {target_dir}")
        # Prefer the largest non-info file (skip .info.json sidecars).
        produced = [p for p in produced if not p.name.endswith(".info.json")]
        if not produced:
            raise HTTPException(500, "yt-dlp produced only sidecars")
        out_file = max(produced, key=lambda p: p.stat().st_size)
        size = out_file.stat().st_size
        if size == 0:
            raise HTTPException(500, f"yt-dlp produced empty file {out_file.name}")

        # Stream + clean up tempdir AFTER the body is sent. We capture
        # the path + tempdir into a generator so the OS file handle
        # outlives the function return.
        def iterfile():
            try:
                with open(out_file, "rb") as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                # Best-effort cleanup; if it fails Cloud Run wipes the
                # ephemeral disk on container shutdown anyway.
                import shutil
                try:
                    shutil.rmtree(target_dir, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass

        ext = out_file.suffix.lstrip(".") or "bin"
        media_type = {
            "mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska",
            "m4a": "audio/mp4", "mp3": "audio/mpeg", "wav": "audio/wav",
            "opus": "audio/opus", "ogg": "audio/ogg",
        }.get(ext, "application/octet-stream")
        return StreamingResponse(
            iterfile(),
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{out_file.name}"',
                "Content-Length": str(size),
                "X-Ytdlp-Bytes": str(size),
                "X-Ytdlp-Filename": out_file.name,
            },
        )
    except HTTPException:
        # Failed before streaming started — clean tempdir now.
        import shutil as _shutil
        _shutil.rmtree(target_dir, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001
        import shutil as _shutil
        _shutil.rmtree(target_dir, ignore_errors=True)
        logger.exception("download failed")
        raise HTTPException(500, f"internal: {exc}")
