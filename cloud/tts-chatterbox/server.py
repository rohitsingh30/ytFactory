"""ytFactory TTS — Chatterbox server (Cloud Run GPU L4).
Single-model. Same /synth contract as F5 + Higgs services.
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

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("ytfactory.tts.chatterbox")

INLINE_LIMIT_BYTES = 5 * 1024 * 1024
GCS_BUCKET = os.environ.get("GCS_BUCKET", "ytfactory-tts-io")

app = FastAPI(title="ytfactory-tts-chatterbox", version="1")
_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        from chatterbox.tts import ChatterboxTTS
        logger.info("loading Chatterbox onto cuda…")
        _MODEL = ChatterboxTTS.from_pretrained(device="cuda")
        logger.info("Chatterbox loaded.")
    return _MODEL


class SynthIn(BaseModel):
    model: str = Field("chatterbox")
    text: str
    ref_audio_b64: str
    ref_text: str | None = None
    speed: float = Field(1.0, ge=0.5, le=2.0)
    seed: int | None = None
    output: str = "inline"
    gcs_object_prefix: str | None = None
    exaggeration: float = 0.5
    cfg_weight: float = 0.5


_REF_DIR = Path(tempfile.gettempdir()) / "ytfactory-tts-refs"
_REF_DIR.mkdir(parents=True, exist_ok=True)


def _ref_audio_to_path(ref_b64: str) -> Path:
    raw = base64.b64decode(ref_b64)
    sha = hashlib.sha256(raw).hexdigest()[:16]
    path = _REF_DIR / f"{sha}.wav"
    if not path.exists():
        path.write_bytes(raw)
    return path


@app.get("/readyz")
def readyz() -> dict:
    t0 = time.time()
    _model()
    return {"status": "ready", "warm_s": round(time.time() - t0, 2)}


@app.post("/synth")
def synth(req: SynthIn) -> JSONResponse:
    if not req.text.strip():
        raise HTTPException(400, "empty text")
    try:
        ref_path = _ref_audio_to_path(req.ref_audio_b64)
    except Exception as e:
        raise HTTPException(400, f"bad ref_audio_b64: {e}")

    out_path = Path(tempfile.gettempdir()) / f"out-{uuid.uuid4().hex[:12]}.wav"
    t0 = time.time()
    try:
        import soundfile as sf
        model = _model()
        wav = model.generate(
            req.text, audio_prompt_path=str(ref_path),
            exaggeration=req.exaggeration, cfg_weight=req.cfg_weight,
        )
        sr = int(model.sr)
        audio_np = wav.detach().cpu().numpy().squeeze()
        if abs(req.speed - 1.0) > 0.01:
            raw = out_path.with_suffix(".raw.wav")
            sf.write(str(raw), audio_np, sr)
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(raw), "-filter:a",
                 f"atempo={req.speed:.4f}", "-ar", str(sr), "-ac", "1",
                 str(out_path)],
                check=True, capture_output=True,
            )
            raw.unlink(missing_ok=True)
        else:
            sf.write(str(out_path), audio_np, sr)
    except Exception as e:
        logger.exception("chatterbox synth failed")
        raise HTTPException(500, f"synth error: {e}")
    wall_s = time.time() - t0

    wav_bytes = out_path.read_bytes()
    sha256 = hashlib.sha256(wav_bytes).hexdigest()
    duration_s = _wav_duration_s(wav_bytes)
    payload = {
        "model": "chatterbox",
        "duration_s": round(duration_s, 3),
        "wall_s": round(wall_s, 3),
        "rtf": round(wall_s / max(duration_s, 1e-3), 3),
        "sha256": sha256,
    }
    if req.output == "gcs" or len(wav_bytes) > INLINE_LIMIT_BYTES:
        payload["output_gcs"] = _upload_to_gcs(
            wav_bytes,
            object_name=f"{req.gcs_object_prefix or ''}{uuid.uuid4().hex}.wav",
        )
    else:
        payload["output_inline"] = base64.b64encode(wav_bytes).decode("ascii")
    out_path.unlink(missing_ok=True)
    return JSONResponse(payload)


def _wav_duration_s(b: bytes) -> float:
    if len(b) < 44 or b[:4] != b"RIFF":
        return 0.0
    sr = int.from_bytes(b[24:28], "little")
    bits = int.from_bytes(b[34:36], "little")
    chans = int.from_bytes(b[22:24], "little")
    data_size = int.from_bytes(b[40:44], "little")
    if sr == 0 or bits == 0 or chans == 0:
        return 0.0
    return data_size / (sr * (bits // 8) * chans)


_GCS_CLIENT = None


def _upload_to_gcs(wav_bytes: bytes, *, object_name: str) -> str:
    global _GCS_CLIENT
    if _GCS_CLIENT is None:
        from google.cloud import storage
        _GCS_CLIENT = storage.Client()
    bucket = _GCS_CLIENT.bucket(GCS_BUCKET)
    blob = bucket.blob(object_name)
    blob.upload_from_string(wav_bytes, content_type="audio/wav")
    return f"gs://{GCS_BUCKET}/{object_name}"
