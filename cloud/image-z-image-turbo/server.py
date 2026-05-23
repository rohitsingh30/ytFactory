"""ytFactory Image — Z-Image-Turbo 6B server (Cloud Run GPU L4).

Apache 2.0. Sole production image-gen lane (the prior FLUX.2 klein 4B
canary lane, 2026-05-07 research per docs/research/image_gen_2026.md,
was retired 2026-05-23 — Z-Image-Turbo won on prompt adherence,
positive-construction discipline, and per-render token economy).

Loads weights from `gs://ytfactory-prod-v3-model-weights/flat/Tongyi-MAI/
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
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

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
    _otel_init("image-z-image-turbo")
    _otel_instrument_outbound()
    _OTEL_OK = True
except Exception:
    _OTEL_OK = False

try:
    from _tel_track_io import track_io as _tel_track_io
except Exception:  # noqa: BLE001
    def _tel_track_io(*_args, **_kwargs) -> None:
        return None

app = FastAPI(title="ytfactory-image-z-image-turbo", version="1")
if _OTEL_OK:
    _otel_instrument_fastapi(app)


def _trace_id_from_request(request: Request) -> str | None:
    try:
        traceparent = request.headers.get("traceparent")
        parts = (traceparent or "").split("-")
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
        rec = _logs_api.LogRecord(
            timestamp=time.time_ns(),
            observed_timestamp=time.time_ns(),
            severity_number=_logs_api.SeverityNumber.INFO,
            severity_text="INFO",
            body={
                "event": event,
                "category": "image",
                "success": True,
                "duration_ms": None,
                "job_id": None,
                "metadata": meta,
            },
            attributes={},
        )
        _logs_api.get_logger("ytfactory.event").emit(rec)
    except Exception:  # noqa: BLE001
        pass


_PIPE = None
_PIPE_LOCK = threading.Lock()
_BOOT_T0 = time.monotonic()


def _pipe():
    """Lazy-load the ZImagePipeline from baked-in image weights.

    The Dockerfile copies the model into the image at build time
    (`/opt/model/Z-Image-Turbo`), so this reads from the image's
    layered filesystem on the host's local SSD — no GCS Fuse round
    trip, ~30 s cold load instead of 1-2 hours.

    VRAM strategy (2026-05-16 fix): L4 has 22 GiB; ZImagePipeline at
    bf16 needs transformer (~12 GiB) + text encoder (~8 GiB) + VAE
    (~1 GiB) ≈ 21 GiB resident if everything's on cuda, leaving
    < 1 GiB for activations → OOM on the 2nd /generate call. We
    enable_model_cpu_offload so only the active stage occupies VRAM
    (text encoder runs first, swaps out, then transformer for
    sampling, then VAE for decode). Costs ~2-3 s per call from the
    CPU↔GPU transfer but guarantees ~10 GiB activation headroom
    for any prompt/size combo within the 22 GiB envelope.

    Also enable VAE slicing + attention slicing — both reduce peak
    activation memory at no perceptible quality cost.

    Thread-safety (2026-05-17, cost-audit fix): wrap the load in
    _PIPE_LOCK using double-checked locking. With concurrency=1 the
    race is unlikely, but Cloud Run's startup probe can call /readyz
    while a user /generate arrives — both would try to load and
    double-allocate VRAM → OOM. Lock guarantees one loader; readers
    fast-path past the lock once the pipe is set."""
    global _PIPE
    if _PIPE is not None:
        return _PIPE
    with _PIPE_LOCK:
        # Re-check under the lock: another thread may have completed
        # the load while we were waiting.
        if _PIPE is not None:
            return _PIPE
        import torch
        from diffusers import ZImagePipeline
        t0 = time.monotonic()
        if not WEIGHTS_DIR.exists():
            raise RuntimeError(
                f"weights dir {WEIGHTS_DIR} not found — image was not "
                f"built with the model baked in, or WEIGHTS_DIR env is wrong"
            )
        logger.info("loading ZImagePipeline from %s …", WEIGHTS_DIR)
        # Don't .to("cuda") here — enable_model_cpu_offload manages
        # device placement itself and conflicts with a manual .to.
        pipe = ZImagePipeline.from_pretrained(
            str(WEIGHTS_DIR),
            torch_dtype=torch.bfloat16,
            low_cpu_mem_usage=True,
            local_files_only=True,
        )
        pipe.enable_model_cpu_offload()
        try:
            pipe.enable_vae_slicing()
        except Exception:
            pass
        try:
            pipe.enable_attention_slicing()
        except Exception:
            pass
        logger.info(
            "ZImagePipeline loaded in %.2fs (boot+%.2fs) with model_cpu_offload",
            time.monotonic() - t0,
            time.monotonic() - _BOOT_T0,
        )
        # Publish the fully-initialised pipe LAST so other threads
        # that fast-path past the lock only see a complete object.
        _PIPE = pipe
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
    # classifier-free guidance entirely. Audit Q2.67 — pre-fix this
    # used ``Field(0.0, ge=0.0, le=0.0)`` which REJECTED (422) any
    # value other than exactly 0.0; floats round-tripping through
    # JSON could fail (e.g. 0.0000001 != 0.0). Now soft-clamp via
    # field_validator: any inbound value is silently coerced to 0.0.
    guidance_scale: float = Field(0.0)  # clamped via validator below
    seed: int | None = None
    output: str = "inline"
    gcs_object_prefix: str | None = None

    @field_validator("guidance_scale", mode="before")
    @classmethod
    def _clamp_guidance(cls, v):
        # Coerce any inbound value to Z-Image-Turbo's locked 0.0.
        return 0.0


@app.get("/readyz")
def readyz(request: Request) -> dict:
    _track_request_event("image.readyz", request, {"endpoint": "/readyz"})
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
def generate(req: GenerateIn, request: Request) -> JSONResponse:
    _track_request_event(
        "image.server",
        request,
        {"endpoint": "/generate", "prompt_chars": len(req.prompt or "")},
    )
    if not req.prompt.strip():
        raise HTTPException(400, "empty prompt")
    traceparent = request.headers.get("traceparent") if request else None

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
    import gc
    generator = (
        torch.Generator(device="cuda").manual_seed(req.seed)
        if req.seed is not None else None
    )

    t0 = time.monotonic()
    try:
        with torch.inference_mode():
            result = pipe(
                prompt=req.prompt,
                height=h, width=w,
                guidance_scale=req.guidance_scale,
                num_inference_steps=req.steps,
                generator=generator,
            )
    except Exception as e:
        wall_s = time.monotonic() - t0
        logger.exception("z-image-turbo generate failed")
        try:
            _tel_track_io(
                "image.server.gen",
                category="image",
                success=False,
                duration_ms=int(wall_s * 1000),
                input_text=req.prompt,
                output_text=f"{type(e).__name__}: {e}",
                input_meta={
                    "negative_prompt": None,
                    "seed": req.seed,
                    "width": w,
                    "height": h,
                    "steps": req.steps,
                    "cfg": req.guidance_scale,
                    "model": "z-image-turbo",
                    "traceparent": traceparent,
                },
                output_meta={"gpu_seconds": round(wall_s, 3), "bytes": 0},
            )
        except Exception:
            pass
        # If OOM, free what we can so the NEXT call has a chance.
        try:
            torch.cuda.empty_cache()
            gc.collect()
        except Exception:
            pass
        raise HTTPException(500, f"generate error: {e}")
    wall_s = time.monotonic() - t0

    image = result.images[0]
    # Free intermediate tensors before encoding the PNG — PIL.save
    # is a pure-CPU op and we want VRAM back for the next call.
    del result
    try:
        torch.cuda.empty_cache()
        gc.collect()
    except Exception:
        pass
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
    try:
        _tel_track_io(
            "image.server.gen",
            category="image",
            success=True,
            duration_ms=int(wall_s * 1000),
            input_text=req.prompt,
            output_text=None,
            input_meta={
                "negative_prompt": None,
                "seed": req.seed,
                "width": w,
                "height": h,
                "steps": req.steps,
                "cfg": req.guidance_scale,
                "model": "z-image-turbo",
                "traceparent": traceparent,
            },
            output_meta={"gpu_seconds": round(wall_s, 3), "bytes": len(png)},
        )
    except Exception:
        pass
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
