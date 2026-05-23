"""cloud/asr-whisper/server.py — faster-whisper /align endpoint.

API
---

::

    POST /align
      {
        "narration_wav_url": "gs://bucket/path.wav"  # OR
        "narration_wav_b64": "...",                   # inline (small files only)
        "anchors": ["Section title 1", "Section title 2", ...],  # optional
        "mode": "beats" | "anchors" | "words",
        "language": "en" | "hi" | ...                # optional, faster-whisper auto-detects
      }
    →
      {
        "duration_s": 123.4,
        "language": "en",
        "segments": [
          {"start_s": 0.0, "end_s": 1.5, "text": "Hello world",
           "anchor_id": "beat_000", "kind": "beat"},
          ...
        ]
      }

Mode semantics
--------------

- ``words`` — return raw word-level timestamps. Lowest level; clients
  can group beats/sections themselves.
- ``beats`` — group words into beats by punctuation + max-word
  threshold. Used by short engine.
- ``anchors`` — match each ``anchors[i]`` text to its location in the
  word stream; emit one Segment per anchor with ``kind="section"``
  and ``anchor_id=anchors[i]``. Used by long engine.

Determinism
-----------

faster-whisper with ``temperature=0`` + ``beam_size=1`` is deterministic
given the same audio + model version. Pinned to ``large-v3`` for
production stability — channels can override via the optional
``model`` request field.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# OTel boot — fire-and-forget; never blocks the service.
# ---------------------------------------------------------------------------

try:
    from otel_init import (
        init as _otel_init,
        instrument_fastapi as _otel_instrument_fastapi,
        instrument_outbound_http as _otel_instrument_outbound,
    )
    _otel_init("asr-whisper")
    _otel_instrument_outbound()
    _OTEL_OK = True
except Exception:  # noqa: BLE001
    _OTEL_OK = False


_logger = logging.getLogger(__name__)
_logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO")))
try:
    from _tel_track_io import track_io as _tel_track_io  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001
    _tel_track_io = None


def _trace_id_from_request(request: Request) -> str | None:
    try:
        parts = (request.headers.get("traceparent") or "").split("-")
        if len(parts) >= 4 and len(parts[1]) == 32:
            return parts[1]
    except Exception:  # noqa: BLE001
        pass
    return None


def _track_request_event(event: str, request: Request, metadata: dict | None = None) -> None:
    try:
        from opentelemetry import _logs as _logs_api  # noqa: PLC0415
        meta = dict(metadata or {})
        trace_id = _trace_id_from_request(request)
        if trace_id:
            meta["trace_id"] = trace_id
        _logs_api.get_logger("ytfactory.event").emit(_logs_api.LogRecord(
            timestamp=time.time_ns(),
            observed_timestamp=time.time_ns(),
            severity_number=_logs_api.SeverityNumber.INFO,
            severity_text="INFO",
            body={
                "event": event,
                "category": "asr",
                "success": True,
                "duration_ms": None,
                "job_id": None,
                "metadata": meta,
            },
            attributes={},
        ))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Model singleton — loaded at startup.
# ---------------------------------------------------------------------------

_MODEL = None
_MODEL_LOCK = threading.Lock()
_MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3")
_DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")
_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE", "float16")


def _get_model():
    """Lazy-load + cache the WhisperModel singleton.

    Thread-safety (2026-05-17, cost-audit fix): double-checked locking
    via _MODEL_LOCK. Cloud Run runs the lifespan warm-up AND any
    early request in parallel when concurrency=2; without the lock
    two WhisperModel(device=cuda) loads race → ~3 GiB × 2 → still
    safe on the 22 GiB L4, but doubles load time + risks CUDA
    init races on the same context. Lock makes it deterministic."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        from faster_whisper import WhisperModel  # noqa: PLC0415
        _logger.info("loading whisper model=%s device=%s compute=%s",
                     _MODEL_NAME, _DEVICE, _COMPUTE_TYPE)
        model = WhisperModel(_MODEL_NAME, device=_DEVICE, compute_type=_COMPUTE_TYPE)
        _logger.info("whisper model loaded")
        # Publish LAST so racing readers only see a complete object.
        _MODEL = model
        return _MODEL


# ---------------------------------------------------------------------------
# FastAPI lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the model on startup so the first request doesn't pay the
    # ~10-30s load cost. Same pattern as the other cloud GPU services.
    try:
        _get_model()
    except Exception as exc:  # noqa: BLE001
        # Don't crash if cold-load fails — first request will retry.
        _logger.warning("startup model load failed (will retry on request): %s", exc)
    yield


app = FastAPI(title="asr-whisper", lifespan=lifespan)
if _OTEL_OK:
    _otel_instrument_fastapi(app)


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class AlignRequest(BaseModel):
    narration_wav_url: str | None = Field(
        default=None,
        description="GCS URL (gs://bucket/path.wav) or http(s)://… URL. "
                    "One of narration_wav_url or narration_wav_b64 required.",
    )
    narration_wav_b64: str | None = Field(
        default=None,
        description="Base64-encoded wav bytes. Use only for small files "
                    "(< 5 MB) — for production prefer narration_wav_url.",
    )
    anchors: list[str] = Field(
        default_factory=list,
        description="Authored anchor texts (section/chapter titles) the "
                    "client wants matched to the narration. Used in "
                    "mode=anchors.",
    )
    mode: str = Field(
        default="beats",
        description="One of: 'words', 'beats', 'anchors'.",
    )
    language: str | None = Field(
        default=None,
        description="Force a language (en, hi, …). Default = auto-detect.",
    )
    model: str | None = Field(
        default=None,
        description="Override the whisper model id (default: WHISPER_MODEL env).",
    )
    max_words_per_beat: int = Field(
        default=8,
        description="In mode=beats, how many words to group per beat "
                    "before forcing a split.",
    )


class Segment(BaseModel):
    start_s: float
    end_s: float
    text: str
    anchor_id: str
    kind: str


class AlignResponse(BaseModel):
    duration_s: float
    language: str
    segments: list[Segment]
    word_count: int
    gpu_seconds: float | None = None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz(request: Request) -> dict[str, Any]:
    _track_request_event("asr.healthz", request, {"endpoint": "/healthz"})
    return {"ok": True, "model": _MODEL_NAME, "device": _DEVICE}


@app.post("/transcribe", response_model=AlignResponse)
def transcribe(req: AlignRequest, request: Request) -> AlignResponse:
    _track_request_event("asr.server", request, {"endpoint": "/transcribe", "mode": "words"})
    data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    data["mode"] = "words"
    data["anchors"] = []
    return align(AlignRequest(**data), request)


@app.post("/align", response_model=AlignResponse)
def align(req: AlignRequest, request: Request) -> AlignResponse:
    _track_request_event("asr.server", request, {"endpoint": "/align", "mode": req.mode})
    if not (req.narration_wav_url or req.narration_wav_b64):
        raise HTTPException(400, "one of narration_wav_url or narration_wav_b64 required")
    if req.mode not in {"words", "beats", "anchors"}:
        raise HTTPException(400, f"unknown mode: {req.mode!r}")
    if req.mode == "anchors" and not req.anchors:
        raise HTTPException(400, "mode=anchors requires non-empty anchors[]")

    wav_path = _resolve_wav(req)
    t0 = time.time()
    try:
        words, language = _transcribe_words(wav_path, language=req.language)
    except Exception as exc:  # noqa: BLE001
        _emit_asr_server_telemetry(
            req=req,
            request=request,
            audio_seconds=0.0,
            language=req.language or "unknown",
            word_count=0,
            gpu_seconds=time.time() - t0,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        if wav_path and wav_path.exists() and str(wav_path).startswith("/tmp/"):
            wav_path.unlink(missing_ok=True)

    gpu_seconds = time.time() - t0
    duration_s = words[-1]["end_s"] if words else 0.0

    if req.mode == "words":
        segments = [
            Segment(
                start_s=w["start_s"],
                end_s=w["end_s"],
                text=w["text"],
                anchor_id=f"word_{i:04d}",
                kind="word",
            )
            for i, w in enumerate(words)
        ]
    elif req.mode == "beats":
        segments = _group_beats(words, max_words_per_beat=req.max_words_per_beat)
    else:  # anchors
        segments = _match_anchors(words, anchors=req.anchors, total_s=duration_s)

    _emit_asr_server_telemetry(
        req=req,
        request=request,
        audio_seconds=duration_s,
        language=language,
        word_count=len(words),
        gpu_seconds=gpu_seconds,
        success=True,
    )
    return AlignResponse(
        duration_s=duration_s,
        language=language,
        segments=segments,
        word_count=len(words),
        gpu_seconds=gpu_seconds,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit_asr_server_telemetry(
    *,
    req: AlignRequest,
    request: Request,
    audio_seconds: float,
    language: str,
    word_count: int,
    gpu_seconds: float,
    success: bool,
    error: str | None = None,
) -> None:
    try:
        if _tel_track_io is None:
            return
        traceparent = request.headers.get("traceparent") if request else None
        metadata: dict[str, Any] = {}
        if traceparent:
            metadata["traceparent"] = traceparent
        if error:
            metadata["error"] = error[:500]
        _tel_track_io(
            "asr.server",
            category="asr",
            success=success,
            input_text=None,
            output_text=None,
            input_meta={
                "audio_seconds": audio_seconds,
                "model": req.model or _MODEL_NAME,
                "language": language,
            },
            output_meta={
                "word_count": word_count,
                "gpu_seconds": gpu_seconds,
            },
            metadata=metadata or None,
        )
    except Exception:  # noqa: BLE001
        pass



def _resolve_wav(req: AlignRequest) -> Path:
    if req.narration_wav_b64:
        data = base64.b64decode(req.narration_wav_b64)
        h = hashlib.sha256(data).hexdigest()[:12]
        out = Path(tempfile.gettempdir()) / f"asr_input_{h}.wav"
        out.write_bytes(data)
        return out

    url = req.narration_wav_url
    assert url is not None
    if url.startswith("gs://"):
        return _download_gcs(url)
    if url.startswith(("http://", "https://")):
        return _download_http(url)
    raise HTTPException(400, f"unsupported url scheme: {url}")


def _download_gcs(gs_url: str) -> Path:
    from google.cloud import storage  # noqa: PLC0415
    bucket_name, blob_name = gs_url[len("gs://"):].split("/", 1)
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(blob_name)
    h = hashlib.sha256(gs_url.encode()).hexdigest()[:12]
    out = Path(tempfile.gettempdir()) / f"asr_input_{h}.wav"
    blob.download_to_filename(str(out))
    return out


def _download_http(url: str) -> Path:
    import urllib.request  # noqa: PLC0415
    h = hashlib.sha256(url.encode()).hexdigest()[:12]
    out = Path(tempfile.gettempdir()) / f"asr_input_{h}.wav"
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
        out.write_bytes(resp.read())
    return out


def _transcribe_words(wav_path: Path, *, language: str | None) -> tuple[list[dict], str]:
    """Run faster-whisper with word-level timestamps. Returns
    ``(list[{text, start_s, end_s}], detected_language)``."""
    model = _get_model()
    segments, info = model.transcribe(
        str(wav_path),
        language=language,
        word_timestamps=True,
        beam_size=1,
        temperature=0.0,
        condition_on_previous_text=False,
        vad_filter=False,
    )
    out: list[dict] = []
    for seg in segments:
        if not seg.words:
            continue
        for w in seg.words:
            out.append({
                "text": (w.word or "").strip(),
                "start_s": float(w.start),
                "end_s": float(w.end),
            })
    return out, info.language or "unknown"


def _group_beats(words: list[dict], *, max_words_per_beat: int) -> list[Segment]:
    if not words:
        return []
    out: list[Segment] = []
    buf: list[dict] = []
    sentence_enders = (".", "!", "?")
    for w in words:
        buf.append(w)
        text = w["text"]
        if (text and text.endswith(sentence_enders)) or len(buf) >= max_words_per_beat:
            text_join = " ".join(b["text"] for b in buf).strip()
            out.append(Segment(
                start_s=buf[0]["start_s"],
                end_s=buf[-1]["end_s"],
                text=text_join,
                anchor_id=f"beat_{len(out):03d}",
                kind="beat",
            ))
            buf = []
    if buf:
        text_join = " ".join(b["text"] for b in buf).strip()
        out.append(Segment(
            start_s=buf[0]["start_s"],
            end_s=buf[-1]["end_s"],
            text=text_join,
            anchor_id=f"beat_{len(out):03d}",
            kind="beat",
        ))
    return out


def _match_anchors(words: list[dict], *, anchors: list[str], total_s: float) -> list[Segment]:
    if not anchors or not words:
        return []
    out: list[Segment] = []
    cursor_s = 0.0
    for i, anchor in enumerate(anchors):
        start_s = _find_anchor(anchor, words, after_s=cursor_s)
        if start_s is None:
            start_s = (i / len(anchors)) * total_s
        end_s = total_s
        for j in range(i + 1, len(anchors)):
            ns = _find_anchor(anchors[j], words, after_s=start_s)
            if ns is not None:
                end_s = ns
                break
        out.append(Segment(
            start_s=start_s,
            end_s=end_s,
            text=anchor,
            anchor_id=(anchor[:64] if anchor else f"sec_{i:03d}"),
            kind="section",
        ))
        cursor_s = start_s
    return out


def _find_anchor(anchor_text: str, words: list[dict], after_s: float) -> float | None:
    if not anchor_text:
        return None
    tokens = [t.lower().strip(".,!?;:") for t in anchor_text.split()[:6] if t.strip()]
    if not tokens:
        return None
    n = len(tokens)
    for i in range(len(words) - n + 1):
        window = [
            (words[i + k]["text"] or "").strip(".,!?;:").lower()
            for k in range(n)
        ]
        if window == tokens and float(words[i]["start_s"]) >= after_s:
            return float(words[i]["start_s"])
    return None
