"""ytFactory Image — Qwen-Image server (Cloud Run GPU L4).
Apache 2.0. Best for photoreal + faces + in-image text rendering.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("ytfactory.image.qwen")

INLINE_LIMIT_BYTES = 5 * 1024 * 1024
GCS_BUCKET = os.environ.get("GCS_BUCKET", "ytfactory-tts-io")

app = FastAPI(title="ytfactory-image-qwen", version="1")
_PIPE = None


def _pipe():
    global _PIPE
    if _PIPE is None:
        import torch
        from diffusers import DiffusionPipeline
        logger.info("loading Qwen/Qwen-Image onto cuda…")
        _PIPE = DiffusionPipeline.from_pretrained(
            "Qwen/Qwen-Image", torch_dtype=torch.bfloat16,
        ).to("cuda")
        logger.info("Qwen-Image loaded.")
    return _PIPE


_ASPECT_DIMS = {
    "1:1":  (1328, 1328),
    "16:9": (1664, 928),
    "9:16": (928, 1664),
    "4:3":  (1472, 1104),
    "3:4":  (1104, 1472),
}
_POSITIVE_MAGIC = ", Ultra HD, 4K, cinematic composition."


class GenerateIn(BaseModel):
    prompt: str
    negative_prompt: str = " "
    aspect: str = Field("9:16")
    width: int | None = None
    height: int | None = None
    steps: int = Field(50, ge=4, le=80)
    guidance_scale: float = Field(4.0, ge=1.0, le=20.0)
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

    if req.width and req.height:
        w, h = req.width, req.height
    elif req.aspect in _ASPECT_DIMS:
        w, h = _ASPECT_DIMS[req.aspect]
    else:
        raise HTTPException(400, f"unknown aspect {req.aspect!r}")

    pipe = _pipe()
    import torch
    generator = torch.Generator(device="cuda").manual_seed(req.seed) if req.seed is not None else None

    t0 = time.time()
    try:
        result = pipe(
            prompt=req.prompt + _POSITIVE_MAGIC,
            negative_prompt=req.negative_prompt or " ",
            width=w, height=h,
            num_inference_steps=req.steps,
            true_cfg_scale=req.guidance_scale,
            generator=generator,
        )
    except Exception as e:
        logger.exception("qwen generate failed")
        raise HTTPException(500, f"generate error: {e}")
    wall_s = time.time() - t0

    image = result.images[0]
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False)
    png = buf.getvalue()
    sha = hashlib.sha256(png).hexdigest()

    payload = {
        "model": "qwen-image", "width": w, "height": h,
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
