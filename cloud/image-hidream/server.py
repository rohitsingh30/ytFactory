"""ytFactory Image — HiDream-I1 server (Cloud Run GPU L4).
MIT license. Best for cartoon, mythological, kids' illustration.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import sys
import time
import uuid

# HiDream installs as a git clone, not a package — add to sys.path
_HIDREAM_REPO = "/opt/HiDream-I1"
if os.path.isdir(_HIDREAM_REPO):
    sys.path.insert(0, _HIDREAM_REPO)

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("ytfactory.image.hidream")

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
    _otel_init("image-hidream")
    _otel_instrument_outbound()
    _OTEL_OK = True
except Exception:
    _OTEL_OK = False

app = FastAPI(title="ytfactory-image-hidream", version="1")
if _OTEL_OK:
    _otel_instrument_fastapi(app)


_PIPE = None


def _pipe():
    """Lazy-load HiDream-I1-Full pipeline (~17 GB weights, ~18 GB VRAM)."""
    global _PIPE
    if _PIPE is None:
        import torch
        from diffusers import HiDreamImagePipeline
        logger.info("loading HiDream-I1-Full onto cuda…")
        _PIPE = HiDreamImagePipeline.from_pretrained(
            "HiDream-ai/HiDream-I1-Full",
            torch_dtype=torch.bfloat16,
        ).to("cuda")
        logger.info("HiDream-I1-Full loaded.")
    return _PIPE


class GenerateIn(BaseModel):
    prompt: str
    negative_prompt: str = ""
    width: int = Field(1024, ge=512, le=2048)
    height: int = Field(1024, ge=512, le=2048)
    steps: int = Field(50, ge=8, le=80)
    guidance_scale: float = Field(5.0, ge=1.0, le=20.0)
    seed: int | None = None
    output: str = "inline"
    gcs_object_prefix: str | None = None


@app.get("/readyz")
def readyz() -> dict:
    t0 = time.time()
    _pipe()
    return {"status": "ready", "warm_s": round(time.time() - t0, 2)}


@app.post("/generate")
def generate(req: GenerateIn) -> JSONResponse:
    if not req.prompt.strip():
        raise HTTPException(400, "empty prompt")

    pipe = _pipe()
    import torch
    generator = torch.Generator(device="cuda").manual_seed(req.seed) if req.seed is not None else None

    t0 = time.time()
    try:
        result = pipe(
            prompt=req.prompt,
            negative_prompt=req.negative_prompt or None,
            width=req.width, height=req.height,
            num_inference_steps=req.steps,
            guidance_scale=req.guidance_scale,
            generator=generator,
        )
    except Exception as e:
        logger.exception("hidream generate failed")
        raise HTTPException(500, f"generate error: {e}")
    wall_s = time.time() - t0

    image = result.images[0]
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False)
    png = buf.getvalue()
    sha = hashlib.sha256(png).hexdigest()

    payload = {
        "model": "hidream-i1-full",
        "width": req.width, "height": req.height,
        "wall_s": round(wall_s, 3), "sha256": sha,
    }
    if req.output == "gcs" or len(png) > INLINE_LIMIT_BYTES:
        payload["output_gcs"] = _upload_to_gcs(
            png, object_name=f"{req.gcs_object_prefix or ''}{uuid.uuid4().hex}.png",
        )
    else:
        payload["output_inline"] = base64.b64encode(png).decode("ascii")
    return JSONResponse(payload)


_GCS_CLIENT = None


def _upload_to_gcs(png_bytes: bytes, *, object_name: str) -> str:
    global _GCS_CLIENT
    if _GCS_CLIENT is None:
        from google.cloud import storage
        _GCS_CLIENT = storage.Client()
    bucket = _GCS_CLIENT.bucket(GCS_BUCKET)
    blob = bucket.blob(object_name)
    blob.upload_from_string(png_bytes, content_type="image/png")
    return f"gs://{GCS_BUCKET}/{object_name}"
