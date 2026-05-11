"""ytFactory Image — Z-Image-Turbo 6B server (Cloud Run GPU L4).

Apache 2.0. Parity / risk-insurance lane next to FLUX.2 klein 4B
(2026-05-07 research, see docs/research/image_gen_2026.md). Same
checkpoint we ship today via mflux on the laptop, so the
`force_positive` prompting trick + style range transfer byte-stably
between the two paths.

Loads weights from `gs://ytfactory-model-weights-v2/flat/Tongyi-MAI/
Z-Image-Turbo/` mounted at `/models/hf/flat/...` via GCS Fuse.

API: POST /generate {prompt, ...} → JSON with PNG inline-base64
(if <5 MB) or GCS URI. GET /readyz lazy-loads the pipeline so
cold-start work happens at /readyz time.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
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
logger = logging.getLogger("ytfactory.image.zimage")

INLINE_LIMIT_BYTES = 5 * 1024 * 1024
GCS_BUCKET = os.environ.get("GCS_BUCKET", "ytfactory-tts-io")

WEIGHTS_DIR = Path(
    os.environ.get(
        "WEIGHTS_DIR",
        "/models/hf/flat/Tongyi-MAI/Z-Image-Turbo",
    )
)
MODEL_REPO = "Tongyi-MAI/Z-Image-Turbo"

app = FastAPI(title="ytfactory-image-z-image-turbo", version="1")
_PIPE = None
_BOOT_T0 = time.monotonic()


def _pipe():
    """Lazy-load the ZImagePipeline. ~60-120 s on cold start (read
    32.9 GB from GCS Fuse, materialize bf16, push 12 GB to CUDA)."""
    global _PIPE
    if _PIPE is None:
        import torch
        from diffusers import ZImagePipeline
        t0 = time.monotonic()
        if not WEIGHTS_DIR.exists():
            raise RuntimeError(
                f"weights dir {WEIGHTS_DIR} not found — bucket mount "
                f"misconfigured? Expected GCS Fuse to mount "
                f"gs://ytfactory-model-weights-v2 at /models/hf"
            )
        logger.info("loading ZImagePipeline from %s …", WEIGHTS_DIR)
        # low_cpu_mem_usage=True: stream each tensor's bytes from disk
        # directly to GPU instead of materializing the full state dict
        # in CPU RAM first. The Tongyi model card recommends
        # low_cpu_mem_usage=False but that needs ~32 GB CPU RAM (the
        # whole safetensor footprint) before .to("cuda") even runs;
        # Cloud Run gen2 caps us at 32 GB total. With True, peak CPU
        # RAM stays bounded by the largest single tensor (~hundreds of
        # MB), and the trade-off is a slower load (~2-3× wall) which
        # we hide behind /readyz warmup from the laptop.
        _PIPE = ZImagePipeline.from_pretrained(
            str(WEIGHTS_DIR),
            torch_dtype=torch.bfloat16,
            low_cpu_mem_usage=True,
            local_files_only=True,
        ).to("cuda")
        logger.info(
            "ZImagePipeline loaded in %.2fs (boot+%.2fs)",
            time.monotonic() - t0,
            time.monotonic() - _BOOT_T0,
        )
    return _PIPE


# Z-Image native dim is 1024². Vertical 9:16 = 768x1344 (multiples of
# 16 keep the patch grid clean).
_ASPECT_DIMS = {
    "9:16": (768, 1344),
    "16:9": (1344, 768),
    "1:1":  (1024, 1024),
    "4:3":  (1152, 896),
    "3:4":  (896, 1152),
}


class GenerateIn(BaseModel):
    prompt: str
    aspect: str = Field("9:16")
    width: int | None = None
    height: int | None = None
    # Z-Image-Turbo is distilled to 8 NFEs (= 9 sampling steps in
    # diffusers parlance). Range 4-12 for experimentation.
    steps: int = Field(9, ge=4, le=12)
    # Z-Image-Turbo is CFG-distilled — guidance_scale=0.0 disables
    # classifier-free guidance entirely. Server-side clamp.
    guidance_scale: float = Field(0.0, ge=0.0, le=0.0)  # locked
    seed: int | None = None
    output: str = "inline"
    gcs_object_prefix: str | None = None


@app.get("/readyz")
def readyz() -> dict:
    t0 = time.monotonic()
    cold = _PIPE is None
    _pipe()
    return {
        "status": "ready",
        "model_repo": MODEL_REPO,
        "runtime": "diffusers",
        "cold_loaded": cold,
        "warm_s": round(time.monotonic() - t0, 2),
        "boot_uptime_s": round(time.monotonic() - _BOOT_T0, 2),
    }


@app.post("/generate")
def generate(req: GenerateIn) -> JSONResponse:
    if not req.prompt.strip():
        raise HTTPException(400, "empty prompt")

    if req.width and req.height:
        w = max(16, (req.width // 16) * 16)
        h = max(16, (req.height // 16) * 16)
    elif req.aspect in _ASPECT_DIMS:
        w, h = _ASPECT_DIMS[req.aspect]
    else:
        raise HTTPException(400, f"unknown aspect {req.aspect!r}")

    cold = _PIPE is None
    pipe = _pipe()
    import torch
    generator = (
        torch.Generator(device="cuda").manual_seed(req.seed)
        if req.seed is not None else None
    )

    t0 = time.monotonic()
    try:
        result = pipe(
            prompt=req.prompt,
            height=h, width=w,
            guidance_scale=req.guidance_scale,
            num_inference_steps=req.steps,
            generator=generator,
        )
    except Exception as e:
        logger.exception("z-image-turbo generate failed")
        raise HTTPException(500, f"generate error: {e}")
    wall_s = time.monotonic() - t0

    image = result.images[0]
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False)
    png = buf.getvalue()
    sha = hashlib.sha256(png).hexdigest()

    payload: dict = {
        "model_repo": MODEL_REPO,
        "runtime": "diffusers",
        "diffusers_class": "ZImagePipeline",
        "width": w,
        "height": h,
        "steps": req.steps,
        "guidance_scale": req.guidance_scale,
        "seed": req.seed,
        "wall_s": round(wall_s, 3),
        "cold_loaded": cold,
        "sha256": sha,
        "png_bytes": len(png),
    }
    if req.output == "gcs" or len(png) > INLINE_LIMIT_BYTES:
        payload["output_gcs"] = _upload_to_gcs(
            png,
            object_name=f"{req.gcs_object_prefix or 'z-image-turbo/'}"
                        f"{uuid.uuid4().hex}.png",
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
