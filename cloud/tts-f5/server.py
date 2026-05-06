"""ytFactory TTS — Cloud Run GPU FastAPI server.

Endpoints:
  GET  /healthz          → liveness (no GPU touch)
  GET  /readyz           → readiness (loads F5 if not yet loaded; takes
                           ~5-8s on cold-start, instant after)
  POST /synth            → main entry. JSON in, WAV out (inline ≤5MB) or
                           GCS URL out (>5MB). Body schema in `SynthIn`.

Auth: protected by Cloud Run's built-in IAM. Caller side
(`pipeline/tts/cloudrun.py`) attaches a Google ID token; Cloud Run
verifies signature + audience before this code ever sees the request.
We don't re-validate here.

Run locally for testing (no GPU): `uvicorn server:app --reload`
Container entrypoint: same uvicorn invocation, see Dockerfile CMD.
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
logger = logging.getLogger("ytfactory.tts.server")

# Inline-vs-GCS threshold. Cloud Run has a 32 MiB response cap; HTTP/2
# also gets unhappy past ~10 MB. 5 MB ≈ 52 s of 24 kHz mono PCM16 (or
# ~26 s if upstream returns float32 — F5 returns int16 via soundfile).
# A typical TTS chunk is 15-30 s, all comfortably inline. Anything
# larger → upload to GCS, return gs:// URI (caller reads via storage SDK).
INLINE_LIMIT_BYTES = 5 * 1024 * 1024

GCS_BUCKET = os.environ.get("GCS_BUCKET", "ytfactory-tts-io")

app = FastAPI(
    title="ytfactory-tts",
    version="1",
    description="TTS inference for ytFactory channels (F5 + Phase-3 models).",
)


# --------------------------------------------------------------------- schema


class SynthIn(BaseModel):
    """Request body for /synth.

    `ref_audio_b64` is the base64 of a 24 kHz mono WAV (5-15 s). The
    container writes it to a tempfile so the F5 ref-cache (keyed by
    path) hits across calls — caller should send the SAME ref bytes for
    repeated voice; we hash them and reuse the tempfile across calls.
    """

    model: str = Field(..., description="Model name; one of SUPPORTED_MODELS.")
    text: str = Field(..., description="Text to synthesise.")
    ref_audio_b64: str = Field(..., description="Base64 of 24 kHz mono WAV.")
    ref_text: str | None = Field(
        None, description="Transcript of ref_audio. REQUIRED for f5."
    )
    speed: float = Field(1.0, ge=0.5, le=2.0)
    seed: int | None = Field(None)
    output: str = Field(
        "inline",
        description="'inline' → WAV bytes b64 in response. 'gcs' → upload "
                    "and return gs:// URI (caller reads via "
                    "google-cloud-storage). Auto-promoted to gcs if WAV "
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


_REF_DIR = Path(tempfile.gettempdir()) / "ytfactory-tts-refs"
_REF_DIR.mkdir(parents=True, exist_ok=True)


def _ref_audio_to_path(ref_b64: str) -> Path:
    """Decode base64 ref WAV and write to a content-addressed tempfile.

    NOTE: This is dedupe-of-uploaded-bytes only — it does NOT make F5's
    inference any faster. F5TTS.infer() preprocesses the reference WAV
    (RMS normalise → mel) on every call regardless of file path; the
    only "cache hit" we save here is a tempfile write when the same
    laptop client posts the same ref WAV twice in a row. Keeping it
    because the saving on disk-IO across a 200-chunk render is non-zero
    and the code is trivial.
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
    """Readiness — warms the F5 singleton if cold. After first hit,
    subsequent /synth calls skip the model-load latency."""
    from models.f5 import _f5_get_model

    t0 = time.time()
    _f5_get_model()
    return {"status": "ready", "warm_s": round(time.time() - t0, 2)}


@app.post("/synth", response_model=SynthOut)
def synth(req: SynthIn) -> JSONResponse:
    if req.model not in SUPPORTED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown model={req.model!r}; supported: {SUPPORTED_MODELS}",
        )
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="empty text")

    try:
        ref_path = _ref_audio_to_path(req.ref_audio_b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad ref_audio_b64: {e}")

    # Each request writes to a fresh tempfile so concurrent calls (if
    # ever; concurrency=1 today) don't collide.
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
        # Caller-side errors (bad input, unsupported model)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("synth failed")
        raise HTTPException(status_code=500, detail=f"synth error: {e}")
    wall_s = time.time() - t0

    wav_bytes = out_path.read_bytes()
    sha256 = hashlib.sha256(wav_bytes).hexdigest()

    # Compute audio duration via header inspection (cheap; no soundfile reload)
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
    """Cheap WAV duration: parse the header, divide. Avoids soundfile
    decode (which would re-allocate the whole sample array)."""
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
