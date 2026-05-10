"""Clone-a-video backend — deep-analyses a video URL and emits a niche
fingerprint the user can review and turn into a new niche.

Pipeline (all cloud-driven, no GPU on the laptop):

  1. yt-dlp downloads the video to a per-request workspace.
  2. ffmpeg extracts ~6 evenly-spaced keyframes + an audio.wav.
  3. Azure OpenAI Whisper deployment (when configured) transcribes
     audio.wav. Optional — gracefully skipped if the deployment env
     vars aren't set.
  4. Azure OpenAI GPT (vision-capable) analyses {frames + transcript +
     duration + user notes} and emits a structured fingerprint JSON
     ready to feed `pipeline.niche_specs.NicheDoc`.

State + artifacts persist under
``data/clone_video_requests/<request_id>/`` so the user can re-fetch
the result later, see the source frames, and the JSON survives a
server restart.

Endpoints:
  POST /api/clone_video                       → {request_id}
  GET  /api/clone_video/{request_id}          → {state, fingerprint?, error?, log_tail}
  GET  /api/clone_video/{request_id}/frames/{i}.jpg
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WORKSPACE = PROJECT_ROOT / "data" / "clone_video_requests"
WORKSPACE.mkdir(parents=True, exist_ok=True)

# Single shared executor — clone analysis is bursty (1-2 at a time
# typically), no need for many workers. 2 keeps the laptop responsive.
_EXEC = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clone-video")

router = APIRouter(prefix="/api/clone_video")


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class CloneRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2000)
    notes: str = Field("", max_length=2000)


class CloneState(BaseModel):
    state: str  # queued | downloading | extracting | transcribing | analyzing | done | failed
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    fingerprint: Optional[dict[str, Any]] = None
    frames_count: int = 0
    duration_s: Optional[float] = None
    has_transcript: bool = False
    log_tail: Optional[str] = None


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _req_dir(request_id: str) -> Path:
    if not request_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "invalid request id")
    return WORKSPACE / request_id


def _write_state(req_dir: Path, **patch: Any) -> dict[str, Any]:
    state_path = req_dir / "state.json"
    cur: dict[str, Any] = {}
    if state_path.exists():
        try:
            cur = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cur = {}
    cur.update({k: v for k, v in patch.items() if v is not None or k in cur})
    state_path.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return cur


def _read_state(req_dir: Path) -> dict[str, Any]:
    state_path = req_dir / "state.json"
    if not state_path.exists():
        return {"state": "missing"}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"state": "corrupt"}


def _log(req_dir: Path, msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}\n"
    (req_dir / "log.txt").open("a", encoding="utf-8").write(line)


def _read_log_tail(req_dir: Path, n: int = 80) -> str:
    log_path = req_dir / "log.txt"
    if not log_path.exists():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-n:])
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------


def _run_subproc(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    """Wrapper around ``subprocess.run`` that detaches from the parent's
    stdin (the daemonized uvicorn closes its stdin → child Python
    processes crash with ``OSError: [Errno 9]`` trying to init sys
    streams). Captures stdout/stderr as text, like the original calls.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _ydl_download(url: str, target_dir: Path) -> Path:
    """Use yt-dlp to fetch the video. Returns path to the mp4."""
    out_template = str(target_dir / "video.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        # Cap to ~720p to keep the download small (we only need frames).
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", out_template,
        url,
    ]
    proc = _run_subproc(cmd, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {proc.stderr.strip() or proc.stdout.strip()}")
    candidates = list(target_dir.glob("video.*"))
    mp4s = [p for p in candidates if p.suffix.lower() in (".mp4", ".mkv", ".webm")]
    if not mp4s:
        raise RuntimeError(f"yt-dlp produced no video file (candidates: {candidates})")
    return mp4s[0]


def _ffprobe_duration(path: Path) -> float:
    proc = _run_subproc(
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
    """Pull `count` evenly-spaced keyframes."""
    duration = _ffprobe_duration(video)
    if duration <= 0:
        # Fallback — grab `count` frames at 1 fps from the start.
        duration = max(count, 10)
    out = []
    for i in range(count):
        # Distribute timestamps evenly across the middle 90% so we miss
        # title cards / black frames at the very ends.
        t = duration * (0.05 + 0.9 * (i / max(count - 1, 1)))
        frame_path = target_dir / f"frame_{i + 1:02d}.jpg"
        proc = _run_subproc(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{t:.2f}", "-i", str(video),
                "-frames:v", "1", "-q:v", "3",
                # Resize to ~720px on the long edge to keep payload small for the LLM.
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
    proc = _run_subproc(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video),
            "-ac", "1", "-ar", "16000", "-vn",
            str(audio),
        ],
        timeout=120,
    )
    if proc.returncode != 0 or not audio.exists():
        return None
    return audio


def _azure_whisper_transcribe(audio: Path) -> Optional[str]:
    """Optional — only runs if AZURE_OPENAI_WHISPER_DEPLOYMENT (or
    its short alias AZURE_OPENAI_WHISPER) is set.

    Falls back to None on any error. The caller treats no-transcript as
    a normal case (the LLM can still read the frames).
    """
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
        from openai import AzureOpenAI  # noqa: PLC0415

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
  length_kind               "short" if total runtime ≤ 90s, else "long"
  source_kind               one of: reddit, wikipedia, manual, x_twitter,
                            youtube, rss   (best guess from content)
  source_ref                if you can identify a specific subreddit /
                            wiki page / channel, return it; else null
  hook_template             one-line hook pattern with {placeholders}
  closer_template           one-line closer / CTA pattern observed
  image_style               short visual aesthetic guide (≤200 chars)
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

    from openai import AzureOpenAI  # noqa: PLC0415

    client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)

    user_content: list[dict[str, Any]] = []
    user_content.append({
        "type": "text",
        "text": (
            f"Runtime: {duration_s:.1f}s.\n"
            f"User notes: {user_notes or '(none)'}\n\n"
            + (f"Transcript:\n{transcript[:8000]}\n\n" if transcript else "Transcript: (unavailable)\n\n")
            + f"{len(frames)} keyframes follow."
        ),
    })
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
# Cloud Run delegation (preferred when CLOUDRUN_CLONE_VIDEO_URL is set)
# ---------------------------------------------------------------------------


def _run_via_cloudrun(request_id: str, url: str, notes: str) -> None:
    """Hand the whole pipeline off to the Cloud Run worker.

    The cloud service runs yt-dlp + ffmpeg + Whisper + GPT in one shot
    and returns {fingerprint, frames_b64, duration_s, ...}. We persist
    everything locally so the existing UI (which expects per-frame JPGs
    + state.json) keeps working without changes.
    """
    import requests  # noqa: PLC0415 — lazy

    base_url = os.environ.get("CLOUDRUN_CLONE_VIDEO_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("CLOUDRUN_CLONE_VIDEO_URL not set")

    req_dir = WORKSPACE / request_id

    headers = {"Content-Type": "application/json"}
    # Bearer ID token for Cloud Run --no-allow-unauthenticated.
    try:
        from pipeline.cloud.cloudrun_auth import get_id_token  # noqa: PLC0415

        token = get_id_token(base_url)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    except Exception as exc:  # noqa: BLE001
        _log(req_dir, f"WARN: could not mint ID token: {exc} (will try anyway)")

    _write_state(req_dir, state="downloading",
                 started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    _log(req_dir, f"Delegating to Cloud Run: {base_url}/analyze")

    timeout_s = int(os.environ.get("CLOUDRUN_CLONE_VIDEO_TIMEOUT", "540"))
    resp = requests.post(
        f"{base_url}/analyze",
        json={"url": url, "notes": notes},
        headers=headers,
        timeout=timeout_s,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Cloud Run analyze HTTP {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    fingerprint = body.get("fingerprint")
    frames_b64 = body.get("frames_b64") or []
    duration = float(body.get("duration_s") or 0.0)
    has_transcript = bool(body.get("has_transcript"))

    # Persist frames locally so the existing /frames/{i}.jpg UI route works.
    for i, b64_str in enumerate(frames_b64, start=1):
        try:
            (req_dir / f"frame_{i:02d}.jpg").write_bytes(base64.b64decode(b64_str))
        except Exception as exc:  # noqa: BLE001
            _log(req_dir, f"WARN: failed to decode frame {i}: {exc}")

    if fingerprint:
        (req_dir / "fingerprint.json").write_text(
            json.dumps(fingerprint, indent=2), encoding="utf-8",
        )

    # Stash the cloud-side log inline for the UI's "live log" panel.
    for line in (body.get("log") or []):
        _log(req_dir, f"[cloud] {line}")

    _write_state(
        req_dir,
        state="done",
        duration_s=duration,
        frames_count=len(frames_b64),
        has_transcript=has_transcript,
        fingerprint=fingerprint,
        finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        error=None,
    )
    _log(req_dir, "Cloud Run pipeline done.")


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _run_pipeline(request_id: str) -> None:
    """Cloud-only pipeline: delegates everything to the Cloud Run worker.

    The legacy local fallback (yt-dlp + ffmpeg + Azure Whisper + Azure
    GPT all on the laptop) was retired 2026-05-09 once the cloud worker
    proved reliable end-to-end. Setting ``CLOUDRUN_CLONE_VIDEO_URL`` is
    now mandatory.
    """
    req_dir = WORKSPACE / request_id
    req_path = req_dir / "request.json"
    if not req_path.exists():
        logger.error("clone_video pipeline: request.json missing for %s", request_id)
        return
    req = json.loads(req_path.read_text(encoding="utf-8"))
    url = req["url"]
    notes = req.get("notes", "")

    if not os.environ.get("CLOUDRUN_CLONE_VIDEO_URL", "").strip():
        msg = (
            "CLOUDRUN_CLONE_VIDEO_URL not set — clone-video pipeline now "
            "requires the Cloud Run worker (laptop fallback retired 2026-05-09). "
            "Set CLOUDRUN_CLONE_VIDEO_URL in .env."
        )
        _log(req_dir, f"FAILED: {msg}")
        _write_state(
            req_dir,
            state="failed",
            error=msg,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        return

    try:
        _run_via_cloudrun(request_id, url, notes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("clone_video pipeline failed for %s", request_id)
        _log(req_dir, f"FAILED: {exc}")
        _write_state(
            req_dir,
            state="failed",
            error=str(exc)[:500],
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("")
async def create_clone(req: CloneRequest) -> dict:
    request_id = uuid.uuid4().hex[:12]
    req_dir = WORKSPACE / request_id
    req_dir.mkdir(parents=True, exist_ok=True)
    (req_dir / "request.json").write_text(
        json.dumps({"url": req.url, "notes": req.notes,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                   indent=2),
        encoding="utf-8",
    )
    _write_state(req_dir, state="queued")
    _log(req_dir, f"Queued. url={req.url!r}")
    _EXEC.submit(_run_pipeline, request_id)
    return {"request_id": request_id, "state": "queued"}


@router.get("/{request_id}")
async def get_clone(request_id: str) -> dict:
    req_dir = _req_dir(request_id)
    if not req_dir.exists():
        raise HTTPException(404, f"unknown request_id: {request_id}")
    state = _read_state(req_dir)
    return {
        "request_id": request_id,
        **state,
        "log_tail": _read_log_tail(req_dir, n=40),
    }


@router.get("/{request_id}/frames/{idx}.jpg")
async def get_clone_frame(request_id: str, idx: int) -> FileResponse:
    if idx < 1 or idx > 30:
        raise HTTPException(400, "frame index out of range")
    req_dir = _req_dir(request_id)
    p = req_dir / f"frame_{idx:02d}.jpg"
    if not p.exists():
        raise HTTPException(404, "frame not found")
    return FileResponse(p, media_type="image/jpeg")
