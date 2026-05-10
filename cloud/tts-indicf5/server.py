"""ytFactory TTS — AI4Bharat IndicF5 server (Cloud Run GPU L4).

Single-model service. Same /synth contract as the F5 / Higgs services:
  POST /synth  → JSON body, WAV out (inline ≤5MB or GCS)

IndicF5 specifics:
  * 11 Indic languages: Assamese, Bengali, Gujarati, Hindi, Kannada,
    Malayalam, Marathi, Odia, Punjabi, Tamil, Telugu
  * WAV-clone style — caller supplies a 5-15s reference WAV + ref text
  * MIT license (more permissive than Apache 2.0)
  * 1417h training data from Rasa, IndicTTS-IITM, LIMMITS24, IndicVoices-R
  * F5-TTS architecture (DiT flow-matching + Vocos vocoder)
  * VRAM ~2-3 GB (much smaller than Higgs's 12 GB)
"""
from __future__ import annotations

import base64
import hashlib
import io
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
logger = logging.getLogger("ytfactory.tts.indicf5")

INLINE_LIMIT_BYTES = 5 * 1024 * 1024
GCS_BUCKET = os.environ.get("GCS_BUCKET", "ytfactory-tts-io")

# Repo override path — switch via env var without rebuilding the image.
# Default is the AI4Bharat upstream. Community mirrors / forks can be
# pointed at via INDICF5_MODEL_REPO=other-org/their-fork at deploy time.
INDICF5_MODEL_REPO = os.environ.get(
    "INDICF5_MODEL_REPO", "ai4bharat/IndicF5",
)

app = FastAPI(title="ytfactory-tts-indicf5", version="1")


_INDICF5_MODEL = None


def _model():
    """Lazy-load IndicF5 (DiT 1024-dim 22-layer + Vocos vocoder, ~2-3 GB VRAM).

    First call:
      * downloads ~1.4 GB weights from HF Hub (cached at /models/hf)
      * AutoModel.from_pretrained loads the model architecture but
        FAILS to bind the checkpoint weights — see "checkpoint key
        mismatch" below.
      * we explicitly download model.safetensors, strip the
        `_orig_mod.` prefix from all keys (an artefact of the
        upstream save being done on a torch.compile()'d module),
        and load into the un-compiled model.

    Why we DON'T use torch.compile (despite IndicF5's model.py doing so):
      * torch.compile triggers Triton kernel compilation on first
        F.conv1d, which on Cloud Run L4 hits CUDNN_STATUS_NOT_INITIALIZED
        (Triton's CUDA runtime needs libs not in our base image).
      * Stripping _orig_mod. lets us run the model un-compiled, which
        works on Cloud Run with our nvidia-cudnn-cu12==9.1.0.70 wheel.
      * Inference is ~10-20% slower than compiled, acceptable trade-off.

    Found 2026-05-06 after v7 returned all-DC-offset audio (silent
    weight-load failure: load_state_dict(strict=False) matched 0/447
    keys, leaving the model with random init weights → conv1d garbage
    output → DC tone after silence-trim + dBFS normalisation).

    Subsequent calls return the cached singleton.
    """
    global _INDICF5_MODEL
    if _INDICF5_MODEL is None:
        from transformers import AutoModel
        from huggingface_hub import hf_hub_download
        from safetensors.torch import load_file
        import torch

        # Patch torch.compile to no-op so the model __init__ doesn't
        # JIT-compile (we want bare modules so checkpoint keys match
        # after stripping _orig_mod.).
        original_compile = torch.compile
        torch.compile = lambda model=None, *a, **kw: (
            (lambda f: f) if model is None else model
        )
        logger.info("disabled torch.compile (load uncompiled, see docstring)")

        try:
            logger.info(
                "loading IndicF5 onto cuda… repo=%s", INDICF5_MODEL_REPO,
            )
            _INDICF5_MODEL = AutoModel.from_pretrained(
                INDICF5_MODEL_REPO, trust_remote_code=True,
            )
        finally:
            torch.compile = original_compile

        # Now manually load + rebind the checkpoint with the
        # _orig_mod. prefix stripped.
        logger.info("downloading checkpoint safetensors…")
        sft_path = hf_hub_download(
            INDICF5_MODEL_REPO, "model.safetensors",
            token=os.environ.get("HF_TOKEN"),
        )
        logger.info("loading checkpoint into memory…")
        raw_state = load_file(sft_path, device="cpu")

        # Strip `._orig_mod.` from every key — this is a
        # torch.compile() artefact in the saved checkpoint.
        renamed = {}
        for k, v in raw_state.items():
            new_k = k.replace("._orig_mod.", ".")
            renamed[new_k] = v

        logger.info(
            "checkpoint: %d keys; after rename: %d unique keys",
            len(raw_state), len(renamed),
        )

        missing, unexpected = _INDICF5_MODEL.load_state_dict(
            renamed, strict=False,
        )
        if missing or unexpected:
            logger.warning(
                "load_state_dict: %d missing, %d unexpected. "
                "first missing: %r ; first unexpected: %r",
                len(missing), len(unexpected),
                (missing or [""])[0], (unexpected or [""])[0],
            )
        else:
            logger.info("load_state_dict: ALL KEYS MATCHED ✅")

        device = "cuda" if torch.cuda.is_available() else "cpu"
        _INDICF5_MODEL.ema_model.to(device)
        _INDICF5_MODEL.vocoder.to(device)
        logger.info("IndicF5 loaded on %s.", device)

    return _INDICF5_MODEL


class SynthIn(BaseModel):
    model: str = Field("indicf5")
    text: str
    ref_audio_b64: str
    ref_text: str = Field(
        ...,
        description="Transcript of the ref WAV (REQUIRED for IndicF5; "
                    "the model uses it for cross-lingual prosody anchoring).",
    )
    speed: float = Field(1.0, ge=0.5, le=2.0)
    seed: int | None = None
    output: str = "inline"
    gcs_object_prefix: str | None = None


_REF_DIR = Path(tempfile.gettempdir()) / "ytfactory-tts-refs"
_REF_DIR.mkdir(parents=True, exist_ok=True)


def _ref_audio_to_path(ref_b64: str) -> Path:
    if not ref_b64:
        raise ValueError(
            "empty ref_audio_b64 — IndicF5 is a voice-cloning model and "
            "requires a base64-encoded reference WAV. Pass ref_audio_path "
            "on the client side."
        )
    raw = base64.b64decode(ref_b64)
    if len(raw) < 44:  # WAV header is 44 bytes minimum
        raise ValueError(
            f"ref_audio_b64 decoded to {len(raw)} bytes — too small to be a "
            "valid WAV (header is 44 bytes)."
        )
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
    if not req.ref_text.strip():
        raise HTTPException(
            400,
            "indicf5 requires `ref_text` (transcript of the ref WAV)",
        )
    try:
        ref_path = _ref_audio_to_path(req.ref_audio_b64)
    except Exception as e:
        raise HTTPException(400, f"bad ref_audio_b64: {e}")

    out_path = Path(tempfile.gettempdir()) / f"out-{uuid.uuid4().hex[:12]}.wav"
    t0 = time.time()
    try:
        _synth_indicf5(
            text=req.text,
            ref_audio_path=str(ref_path),
            ref_audio_text=req.ref_text,
            out_path=out_path,
            speed=req.speed,
        )
    except Exception as e:
        logger.exception("indicf5 synth failed")
        raise HTTPException(500, f"synth error: {e}")
    wall_s = time.time() - t0

    wav_bytes = out_path.read_bytes()
    sha256 = hashlib.sha256(wav_bytes).hexdigest()
    duration_s = _wav_duration_s(wav_bytes)

    payload = {
        "model": "indicf5",
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


def _synth_indicf5(*, text, ref_audio_path, ref_audio_text, out_path, speed=1.0):
    """IndicF5 zero-shot voice clone.

    Calls model.forward(text, ref_audio_path, ref_text) which:
      1. Loads + normalises the ref WAV
      2. Runs DiT flow-matching to generate mel-spectrogram
      3. Runs Vocos vocoder to mel→waveform
      4. Trims silence (>1s gaps) and normalises to -20 dBFS
      5. Returns int16 numpy array @ 24 kHz
    """
    import numpy as np
    import soundfile as sf

    out_path.parent.mkdir(parents=True, exist_ok=True)
    model = _model()

    # Apply per-call speed override. The model reads self.config.speed
    # in its forward(); patch for this call only.
    prev_speed = model.config.speed
    try:
        model.config.speed = float(speed)
        audio = model(
            text,
            ref_audio_path=ref_audio_path,
            ref_text=ref_audio_text,
        )
    finally:
        model.config.speed = prev_speed

    # IndicF5 returns int16; F5 + Higgs return float32. Normalise to
    # float32 for consistency, then write as int16 PCM (matches our
    # other services' on-disk format).
    if audio.dtype == np.int16:
        audio_f32 = audio.astype(np.float32) / 32768.0
    else:
        audio_f32 = audio.astype(np.float32)

    sf.write(str(out_path), audio_f32, samplerate=24000, subtype="PCM_16")
    logger.info(
        "indicf5 synth done: chars=%d audio_s=%.2f sr=24000",
        len(text), len(audio_f32) / 24000.0,
    )
    return out_path


def _wav_duration_s(wav_bytes: bytes) -> float:
    """Cheap WAV duration via header parse. Robust to LIST-INFO chunks
    that pydub can add (we use SoundFile which writes minimal headers,
    but defensive in case post-processing inserts metadata)."""
    if len(wav_bytes) < 44 or wav_bytes[:4] != b"RIFF":
        return 0.0
    # Walk chunks to find the 'data' chunk (handles RIFF + LIST + data).
    sr = 0
    bits = 0
    chans = 0
    data_size = 0
    pos = 12
    while pos < len(wav_bytes) - 8:
        chunk_id = wav_bytes[pos:pos+4]
        chunk_size = int.from_bytes(wav_bytes[pos+4:pos+8], "little")
        if chunk_id == b"fmt ":
            chans = int.from_bytes(wav_bytes[pos+10:pos+12], "little")
            sr = int.from_bytes(wav_bytes[pos+12:pos+16], "little")
            bits = int.from_bytes(wav_bytes[pos+22:pos+24], "little")
        elif chunk_id == b"data":
            data_size = chunk_size
            break
        pos += 8 + chunk_size
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
