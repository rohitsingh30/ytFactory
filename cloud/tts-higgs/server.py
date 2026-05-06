"""ytFactory TTS — Higgs Audio v2 server (Cloud Run GPU L4).

Single-model service. Same /synth contract as the F5 service.
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
logger = logging.getLogger("ytfactory.tts.higgs")

INLINE_LIMIT_BYTES = 5 * 1024 * 1024
GCS_BUCKET = os.environ.get("GCS_BUCKET", "ytfactory-tts-io")

app = FastAPI(title="ytfactory-tts-higgs", version="1")


_HIGGS_ENGINE = None


# --- Higgs v2 checkpoint pair ---
# We use the PierrunoYT community mirrors (NOT the official bosonai/ ones)
# because Boson refactored the bosonai/* configs in late 2026 to a new
# schema (text_config=None, model_type='higgs_audio_v2', tokenizer
# acoustic_model_config=...) WITHOUT updating the github boson_multimodal
# code. The installed code at /opt/higgs-audio is on the OLD schema
# (model_type='higgs_audio', text_config populated, tokenizer
# n_filters/target_bandwidths). PierrunoYT preserves both old configs
# and is the only working pair until upstream fixes itself.
#
# When you bump the python wheel (`pip install -e .` in Dockerfile),
# also re-check whether bosonai/* loads cleanly. If yes, switch back
# (the official mirror is canonical).
HIGGS_MODEL_REPO = os.environ.get(
    "HIGGS_MODEL_REPO",
    "PierrunoYT/higgs-audio-v2-generation-3B-base",
)
HIGGS_TOKENIZER_REPO = os.environ.get(
    "HIGGS_TOKENIZER_REPO",
    "PierrunoYT/higgs-audio-v2-tokenizer",
)


def _engine():
    """Lazy-load Higgs Audio v2 (3B + 2.2B audio adapter, ~12 GB VRAM)."""
    global _HIGGS_ENGINE
    if _HIGGS_ENGINE is None:
        from boson_multimodal.serve.serve_engine import HiggsAudioServeEngine

        logger.info(
            "loading Higgs Audio v2 onto cuda… model=%s tokenizer=%s",
            HIGGS_MODEL_REPO, HIGGS_TOKENIZER_REPO,
        )
        _HIGGS_ENGINE = HiggsAudioServeEngine(
            HIGGS_MODEL_REPO,
            HIGGS_TOKENIZER_REPO,
            device="cuda",
        )
        logger.info("Higgs Audio v2 loaded.")
    return _HIGGS_ENGINE


class SynthIn(BaseModel):
    model: str = Field("higgs")
    text: str
    ref_audio_b64: str
    ref_text: str | None = None
    speed: float = Field(1.0, ge=0.5, le=2.0)
    seed: int | None = None
    output: str = "inline"
    gcs_object_prefix: str | None = None


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
    _engine()
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
        _synth_higgs(
            text=req.text, ref_audio_path=str(ref_path),
            ref_audio_text=req.ref_text, out_path=out_path,
            speed=req.speed, seed=req.seed,
        )
    except Exception as e:
        logger.exception("higgs synth failed")
        raise HTTPException(500, f"synth error: {e}")
    wall_s = time.time() - t0

    wav_bytes = out_path.read_bytes()
    sha256 = hashlib.sha256(wav_bytes).hexdigest()
    duration_s = _wav_duration_s(wav_bytes)

    payload = {
        "model": "higgs",
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


def _synth_higgs(*, text, ref_audio_path, ref_audio_text, out_path,
                 speed=1.0, seed=None):
    """Higgs Audio v2 zero-shot voice clone."""
    import torch
    import torchaudio
    from boson_multimodal.data_types import (
        ChatMLSample, Message, AudioContent,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    engine = _engine()

    system_prompt = (
        "Generate audio following instruction.\n\n"
        "<|scene_desc_start|>\n"
        "Audio is recorded from a quiet room.\n"
        "<|scene_desc_end|>"
    )
    messages = [Message(role="system", content=system_prompt)]
    if ref_audio_path:
        messages.append(Message(
            role="user",
            content=ref_audio_text or "Reference voice sample.",
        ))
        messages.append(Message(
            role="assistant",
            content=AudioContent(audio_url=str(ref_audio_path)),
        ))
    messages.append(Message(role="user", content=text))

    output = engine.generate(
        chat_ml_sample=ChatMLSample(messages=messages),
        max_new_tokens=4096,
        temperature=0.3,
        top_p=0.95,
        top_k=50,
        stop_strings=["<|end_of_text|>", "<|eot_id|>"],
        seed=seed,
    )
    audio = torch.from_numpy(output.audio)[None, :]
    torchaudio.save(str(out_path), audio, output.sampling_rate)
    logger.info(
        "higgs synth done: chars=%d audio_s=%.2f sr=%d",
        len(text), audio.shape[-1] / output.sampling_rate,
        output.sampling_rate,
    )
    return out_path


def _wav_duration_s(wav_bytes: bytes) -> float:
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
    global _GCS_CLIENT
    if _GCS_CLIENT is None:
        from google.cloud import storage
        _GCS_CLIENT = storage.Client()
    bucket = _GCS_CLIENT.bucket(GCS_BUCKET)
    blob = bucket.blob(object_name)
    blob.upload_from_string(wav_bytes, content_type="audio/wav")
    return f"gs://{GCS_BUCKET}/{object_name}"
