"""ytFactory TTS — Indic Parler-TTS server (Cloud Run GPU L4).

Apache 2.0. 21 Indian languages + English. Description-based voice
control (NOT WAV clone) — caller supplies a `description` like
"Leela narrates calmly in a soothing devotional Indian female voice".
Language is auto-detected from the prompt text.

Same /synth contract as the other TTS services for caller parity, but:
  - `ref_audio_b64` is IGNORED (description-only, no clone)
  - `description` is the new field — describes the voice
  - `text` is the prompt to synthesise (in any of 21 Indic langs + EN)
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import subprocess
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
logger = logging.getLogger("ytfactory.tts.indicparler")

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
    _otel_init("tts-indicparler")
    _otel_instrument_outbound()
    _OTEL_OK = True
except Exception:
    _OTEL_OK = False

app = FastAPI(title="ytfactory-tts-indicparler", version="1")

if _OTEL_OK:
    _otel_instrument_fastapi(app)


_MODEL = None
_PROMPT_TOKENIZER = None
_DESC_TOKENIZER = None
_SR = None


def _load():
    """Lazy-load Indic Parler (~2.6 GB weights, ~4 GB VRAM).

    Returns (model, prompt_tokenizer, description_tokenizer, sample_rate).
    """
    global _MODEL, _PROMPT_TOKENIZER, _DESC_TOKENIZER, _SR
    if _MODEL is None:
        import torch
        from parler_tts import ParlerTTSForConditionalGeneration
        from transformers import AutoTokenizer

        logger.info("loading ai4bharat/indic-parler-tts onto cuda…")
        _MODEL = ParlerTTSForConditionalGeneration.from_pretrained(
            "ai4bharat/indic-parler-tts",
        ).to("cuda")
        _PROMPT_TOKENIZER = AutoTokenizer.from_pretrained(
            "ai4bharat/indic-parler-tts",
        )
        _DESC_TOKENIZER = AutoTokenizer.from_pretrained(
            _MODEL.config.text_encoder._name_or_path,
        )
        _SR = _MODEL.config.sampling_rate
        logger.info("Indic Parler loaded (sample_rate=%s).", _SR)
    return _MODEL, _PROMPT_TOKENIZER, _DESC_TOKENIZER, _SR


# Default voice description tuned for kathaa narration (calm, devotional).
# Caller can override per-request.
_DEFAULT_DESCRIPTION = (
    "A clear, calm, devotional Indian female voice narrating at a moderate "
    "pace with high expressivity. The recording is of very high quality "
    "with no background noise."
)


class SynthIn(BaseModel):
    model: str = Field("indicparler")
    text: str
    # ref_audio_b64 is accepted for API parity but IGNORED — Parler is
    # description-driven, not WAV-clone.
    ref_audio_b64: str | None = None
    ref_text: str | None = None
    # The "voice description" — shape and quality of the speech.
    description: str = Field(_DEFAULT_DESCRIPTION)
    speed: float = Field(1.0, ge=0.5, le=2.0)
    seed: int | None = None
    output: str = "inline"
    gcs_object_prefix: str | None = None


@app.get("/readyz")
def readyz() -> dict:
    t0 = time.time()
    _load()
    return {"status": "ready", "warm_s": round(time.time() - t0, 2)}


@app.post("/synth")
def synth(req: SynthIn) -> JSONResponse:
    if not req.text.strip():
        raise HTTPException(400, "empty text")

    model, prompt_tok, desc_tok, sr = _load()
    import torch
    import soundfile as sf

    if req.seed is not None:
        torch.manual_seed(req.seed)

    desc_inputs = desc_tok(req.description, return_tensors="pt").to("cuda")
    prompt_inputs = prompt_tok(req.text, return_tensors="pt").to("cuda")

    out_path = Path(tempfile.gettempdir()) / f"out-{uuid.uuid4().hex[:12]}.wav"
    t0 = time.time()
    try:
        generation = model.generate(
            input_ids=desc_inputs.input_ids,
            attention_mask=desc_inputs.attention_mask,
            prompt_input_ids=prompt_inputs.input_ids,
            prompt_attention_mask=prompt_inputs.attention_mask,
        )
        audio = generation.cpu().numpy().squeeze()
        sf.write(str(out_path), audio, sr)
    except Exception as e:
        logger.exception("indicparler synth failed")
        raise HTTPException(500, f"synth error: {e}")
    # Audit Q2.18 — apply requested speed via post-process atempo.
    # IndicParler doesn't expose a tempo/speed knob (the
    # description-driven model derives prosody from `description`
    # alone). Post-process via ffmpeg's `atempo` (preserves pitch).
    if abs(req.speed - 1.0) > 1e-3:  # coverage: requires loaded IndicParler model + cuda to call synth handler
        _apply_speed_post_process(out_path, req.speed)  # coverage: helper itself fully unit-tested in test_cloud_tts_indicparler.py
        # WAV bytes + duration must reflect the speed-adjusted file.
        wav_bytes = out_path.read_bytes()  # coverage: branch only reachable via end-to-end synth
        adjusted = _wav_duration_s(wav_bytes)  # coverage: branch only reachable via end-to-end synth
        if adjusted > 0:  # coverage: branch only reachable via end-to-end synth
            duration_s = adjusted  # coverage: branch only reachable via end-to-end synth
    wall_s = time.time() - t0

    if abs(req.speed - 1.0) <= 1e-3:  # coverage: branch only reachable via end-to-end synth
        wav_bytes = out_path.read_bytes()  # coverage: branch only reachable via end-to-end synth
        duration_s = len(audio) / sr if sr else 0.0  # coverage: branch only reachable via end-to-end synth
    sha = hashlib.sha256(wav_bytes).hexdigest()

    payload = {
        "model": "indicparler",
        "duration_s": round(float(duration_s), 3),
        "wall_s": round(wall_s, 3),
        "rtf": round(wall_s / max(float(duration_s), 1e-3), 3),
        "sha256": sha,
        "sample_rate": sr,
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


_GCS_CLIENT = None


def _wav_duration_s(wav_bytes: bytes) -> float:
    """Audit Q2.19 — minimal WAV header parser used both for the
    response payload `duration_s` (post speed adjust) and for the
    short-write guard. Returns 0.0 on malformed/short input rather
    than raising so callers can branch."""
    if len(wav_bytes) < 44 or wav_bytes[:4] != b"RIFF":
        return 0.0
    sr = int.from_bytes(wav_bytes[24:28], "little")
    bits = int.from_bytes(wav_bytes[34:36], "little")
    chans = int.from_bytes(wav_bytes[22:24], "little")
    data_size = int.from_bytes(wav_bytes[40:44], "little")
    if sr == 0 or bits == 0 or chans == 0:
        return 0.0
    bytes_per_s = sr * chans * (bits // 8)
    return data_size / bytes_per_s if bytes_per_s else 0.0


def _apply_speed_post_process(wav_path: Path, speed: float) -> None:
    """Audit Q2.18 — apply the requested ``speed`` factor via ffmpeg's
    `atempo` filter (preserves pitch). Validator bounds speed to
    [0.5, 2.0]; a single atempo invocation covers the whole range
    (atempo per-instance accepts [0.5, 100])."""
    if abs(speed - 1.0) <= 1e-3:
        return
    tmp_out = wav_path.with_suffix(".sped.wav")
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(wav_path),
            "-filter:a", f"atempo={speed:.4f}",
            str(tmp_out),
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        tmp_out.unlink(missing_ok=True)
        raise RuntimeError(
            f"ffmpeg atempo={speed} exited {proc.returncode}: "
            f"{(proc.stderr or b'').decode('utf-8', 'replace')[:500]}"
        )
    tmp_out.replace(wav_path)


def _upload_to_gcs(wav_bytes: bytes, *, object_name: str) -> str:
    global _GCS_CLIENT
    if _GCS_CLIENT is None:
        from google.cloud import storage
        _GCS_CLIENT = storage.Client()
    bucket = _GCS_CLIENT.bucket(GCS_BUCKET)
    blob = bucket.blob(object_name)
    blob.upload_from_string(wav_bytes, content_type="audio/wav")
    return f"gs://{GCS_BUCKET}/{object_name}"
