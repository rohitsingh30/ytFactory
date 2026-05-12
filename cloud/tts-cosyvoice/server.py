"""ytFactory TTS-CosyVoice — Cloud Run GPU FastAPI server.

Single-model variant of cloud/tts/server.py — only "cosyvoice".
Same /healthz, /readyz, /synth contract so the laptop-side dispatcher
(`pipeline/tts/cloudrun.py`) talks to this service the same way it
talks to the main ytfactory-tts service; only the URL env var differs
(`CLOUDRUN_TTS_COSYVOICE_URL` vs `CLOUDRUN_TTS_URL`).

WHY A SEPARATE SERVICE: see the Dockerfile preamble.

Auth: protected by Cloud Run's built-in IAM. Caller side attaches a
Google ID token; Cloud Run verifies signature + audience before this
code ever sees the request.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from handler import SUPPORTED_MODELS, synthesize

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("ytfactory.tts-cosyvoice.server")

# Same inline-vs-GCS threshold as the main service. Cloud Run has a
# 32 MiB response cap; HTTP/2 also gets unhappy past ~10 MB. 5 MB ≈
# 52 s of 24 kHz mono PCM16. Hindi kathaa chunks rarely exceed 30 s.
INLINE_LIMIT_BYTES = 5 * 1024 * 1024

GCS_BUCKET = os.environ.get("GCS_BUCKET", "ytfactory-tts-io")

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
    _otel_init("tts-cosyvoice")
    _otel_instrument_outbound()
    _OTEL_OK = True
except Exception:
    _OTEL_OK = False

app = FastAPI(
    title="ytfactory-tts-cosyvoice",
    version="1",
    description="CosyVoice 2 (multilingual incl Hindi) TTS for ytFactory.",
)


if _OTEL_OK:
    _otel_instrument_fastapi(app)


# --------------------------------------------------------------------- schema


class SynthIn(BaseModel):
    """Request body for /synth.

    Mirrors the main ytfactory-tts service's SynthIn so callers can
    swap URLs without touching payload shape.
    """

    model: str = Field(..., description="Model name; only 'cosyvoice' supported.")
    text: str = Field(..., description="Text to synthesise.")
    ref_audio_b64: str = Field(..., description="Base64 of mono WAV (16/24 kHz).")
    ref_text: str | None = Field(
        None, description="Transcript of ref_audio. REQUIRED for cosyvoice."
    )
    speed: float = Field(1.0, ge=0.5, le=2.0)
    seed: int | None = Field(None)
    output: str = Field(
        "inline",
        description="'inline' → WAV bytes b64 in response. 'gcs' → upload "
                    "and return gs:// URI. Auto-promoted to gcs if WAV "
                    f">{INLINE_LIMIT_BYTES} bytes.",
    )
    gcs_object_prefix: str | None = Field(
        None,
        description="Optional GCS path prefix (e.g. '<channel>/<slug>/'). "
                    "Final object is <prefix><uuid>.wav.",
    )


class SynthOut(BaseModel):
    model: str
    duration_s: float
    wall_s: float
    rtf: float
    sha256: str
    output_inline: str | None = None
    output_gcs: str | None = None


# ---------------------------------------------------------- ref-audio caching


_REF_DIR = Path(tempfile.gettempdir()) / "ytfactory-tts-cosyvoice-refs"
_REF_DIR.mkdir(parents=True, exist_ok=True)


def _ref_audio_to_path(ref_b64: str) -> Path:
    """Decode base64 ref WAV and write to a content-addressed tempfile.

    Dedupe-of-uploaded-bytes only — saves one disk write per repeat ref.
    """
    raw = base64.b64decode(ref_b64)
    sha = hashlib.sha256(raw).hexdigest()[:16]
    path = _REF_DIR / f"{sha}.wav"
    if not path.exists():
        path.write_bytes(raw)
    return path


# ----------------------------------------------------------------- endpoints


@app.get("/healthz")
def healthz() -> dict:
    """Liveness — does NOT touch the GPU. Used by Cloud Run probes."""
    return {"status": "ok", "models": list(SUPPORTED_MODELS)}


@app.get("/readyz")
def readyz() -> dict:
    """Readiness — warms the CosyVoice singleton. First call is the
    expensive one (~5-8 s on L4); subsequent /synth calls skip load."""
    from models.cosyvoice import _cosyvoice_model

    t0 = time.time()
    _cosyvoice_model()
    return {"status": "ready", "warm_s": round(time.time() - t0, 2)}


@app.post("/synth", response_model=SynthOut)
def synth(req: SynthIn) -> JSONResponse:
    if req.model not in SUPPORTED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown model={req.model!r}; this service supports "
                   f"{SUPPORTED_MODELS}. F5/Higgs/Chatterbox live on the "
                   f"main ytfactory-tts service.",
        )
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="empty text")

    try:
        ref_path = _ref_audio_to_path(req.ref_audio_b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad ref_audio_b64: {e}")

    out_path = Path(tempfile.gettempdir()) / f"out-{uuid.uuid4().hex[:12]}.wav"

    t0 = time.time()
    try:
        synthesize(
            model=req.model,
            text=req.text,
            ref_audio_path=str(ref_path),
            ref_audio_text=req.ref_text,
            out_path=out_path,
            speed=req.speed,
            seed=req.seed,
        )
    except (ValueError, NotImplementedError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("synth failed")
        raise HTTPException(status_code=500, detail=f"synth error: {e}")
    wall_s = time.time() - t0

    wav_bytes = out_path.read_bytes()
    sha256 = hashlib.sha256(wav_bytes).hexdigest()
    duration_s = _wav_duration_s(wav_bytes)

    promote_to_gcs = (
        req.output == "gcs" or len(wav_bytes) > INLINE_LIMIT_BYTES
    )

    payload: dict = {
        "model": req.model,
        "duration_s": round(duration_s, 3),
        "wall_s": round(wall_s, 3),
        "rtf": round(wall_s / max(duration_s, 1e-3), 3),
        "sha256": sha256,
    }

    if promote_to_gcs:
        gcs_uri = _upload_to_gcs(
            wav_bytes,
            object_name=f"{req.gcs_object_prefix or ''}{uuid.uuid4().hex}.wav",
        )
        payload["output_gcs"] = gcs_uri
    else:
        payload["output_inline"] = base64.b64encode(wav_bytes).decode("ascii")

    out_path.unlink(missing_ok=True)
    return JSONResponse(payload)


# ---------------------------------------------------------------- helpers


def _wav_duration_s(wav_bytes: bytes) -> float:
    """Cheap WAV duration: parse the header, divide."""
    if len(wav_bytes) < 44 or wav_bytes[:4] != b"RIFF":
        return 0.0
    sr = int.from_bytes(wav_bytes[24:28], "little")
    bits = int.from_bytes(wav_bytes[34:36], "little")
    chans = int.from_bytes(wav_bytes[22:24], "little")
    data_size = int.from_bytes(wav_bytes[40:44], "little")
    if sr == 0 or bits == 0 or chans == 0:
        return 0.0
    return data_size / (sr * (bits // 8) * chans)


_GCS_CLIENT = None


def _upload_to_gcs(wav_bytes: bytes, *, object_name: str) -> str:
    """Upload to gs://GCS_BUCKET/<object_name>; return gs:// URI."""
    global _GCS_CLIENT
    if _GCS_CLIENT is None:
        from google.cloud import storage  # type: ignore

        _GCS_CLIENT = storage.Client()
    bucket = _GCS_CLIENT.bucket(GCS_BUCKET)
    blob = bucket.blob(object_name)
    blob.upload_from_string(wav_bytes, content_type="audio/wav")
    uri = f"gs://{GCS_BUCKET}/{object_name}"
    logger.info("uploaded %d bytes → %s", len(wav_bytes), uri)
    return uri
