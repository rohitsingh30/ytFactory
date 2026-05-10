"""ytFactory Image — FLUX.2 [klein] 4B server (Cloud Run GPU L4).

Apache 2.0. New default for stylized + photoreal Shorts (2026-05-07
research, see docs/research/image_gen_2026.md). Sub-second warm
inference at 1024x1024 with 4 steps on L4 bf16.

Loads weights from `gs://ytfactory-model-weights/flat/black-forest-labs/
FLUX.2-klein-4B/` mounted at `/models/hf/flat/...` via GCS Fuse. The
flat layout (one real file per repo entry, no symlinks) is required
for `from_pretrained(local_files_only=True)` to work — see
`cloud/weights-staging/stage.py::_stage_one_flat` for why.

API: POST /generate {prompt, ...} → JSON with PNG inline-base64 (if
<5 MB) or GCS URI. GET /readyz lazy-loads the pipeline so cold-start
work happens at /readyz time (laptop client warms via a background
GET before the render starts).

VRAM accounting (post-OOM debug 2026-05-10):
  - 4B transformer + 8B Qwen3 text encoder = 12B params @ bf16 = 24 GB
  - L4 has 22 GiB usable VRAM (~2 GB driver-reserved on the 24 GB card)
  - With naive `.to("cuda")` the FIRST /generate call OOMs allocating
    activations during prompt encoding (the smoking-gun trace logged
    21.94 GB / 21.96 GB used before any inference)
  - Fix: ``enable_model_cpu_offload()`` keeps each submodule on CPU
    until it's about to run; peak VRAM drops to ~12-14 GB. Per-call
    latency +1-2 s warm. See _pipe() for the trade-off math.

Allocator hint (PYTORCH_CUDA_ALLOC_CONF) is set at module import
time so it takes effect before the first `import torch`. Without it
the post-OOM error suggests
``expandable_segments:True`` to reduce fragmentation; we set it
unconditionally.
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

# Set BEFORE importing torch — the allocator reads this on init.
# Reduces fragmentation-induced OOMs that the post-OOM CUDA error
# message itself recommends in PyTorch 2.5+.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("ytfactory.image.flux2klein")

INLINE_LIMIT_BYTES = 5 * 1024 * 1024
GCS_BUCKET = os.environ.get("GCS_BUCKET", "ytfactory-tts-io")

# Weights live in the GCS-Fuse-mounted bucket at this exact path.
# stage.py wrote them as `gs://<bucket>/flat/<repo>/...` and Cloud Run
# mounts the bucket root at /models/hf, so the in-container path is:
WEIGHTS_DIR = Path(
    os.environ.get(
        "WEIGHTS_DIR",
        "/models/hf/flat/black-forest-labs/FLUX.2-klein-4B",
    )
)
MODEL_REPO = "black-forest-labs/FLUX.2-klein-4B"

app = FastAPI(title="ytfactory-image-flux2-klein", version="1")
_PIPE = None
_BOOT_T0 = time.monotonic()


def _pipe():
    """Lazy-load the Flux2KleinPipeline.

    Memory accounting (from a real OOM trace 2026-05-10):
      - Transformer (4B params, bf16): ~8 GB
      - Qwen3 text encoder (8B params, bf16): ~16 GB
      - VAE + scheduler scratch: ~1 GB
      - Activations during prompt encoding: ~0.5-2 GB
      Total: ~25-27 GB peak — does NOT fit in the L4's 22 GiB usable
      VRAM. The Dockerfile's "~13 GB at bf16" estimate ignored Qwen3.

    Fix: ``enable_model_cpu_offload()`` keeps each submodule on CPU
    until it's about to run, then moves it to GPU just-in-time and
    moves it back when done. Diffusers handles the orchestration. Net
    effect: peak VRAM ~12-14 GB instead of 25 GB; per-call latency
    +1-2 s on warm calls (the back-and-forth) but actually completes.

    The alternative (sequential CPU offload, more aggressive) would
    drop us to ~6 GB peak but adds another 2-4 s per call. Model-level
    is the right balance for our workload.
    """
    global _PIPE
    if _PIPE is None:
        import torch
        from diffusers import Flux2KleinPipeline
        t0 = time.monotonic()
        if not WEIGHTS_DIR.exists():
            raise RuntimeError(
                f"weights dir {WEIGHTS_DIR} not found — bucket mount "
                f"misconfigured? Expected GCS Fuse to mount "
                f"gs://ytfactory-model-weights at /models/hf"
            )
        logger.info("loading Flux2KleinPipeline from %s …", WEIGHTS_DIR)
        pipe = Flux2KleinPipeline.from_pretrained(
            str(WEIGHTS_DIR),
            torch_dtype=torch.bfloat16,
            local_files_only=True,
        )
        # Critical for L4 24 GB: total model weights are ~24 GB at
        # bf16 (4B transformer + 8B Qwen3 text encoder = 12B params).
        # Without offload we OOM on the very first /generate call. See
        # docstring above for the trade-off math. NEVER replace this
        # with `.to("cuda")` — that worked in dev tests because the
        # tests didn't actually run inference; the OOM only fires when
        # the pipeline tries to allocate activations during diffusion.
        pipe.enable_model_cpu_offload()
        _PIPE = pipe
        logger.info(
            "Flux2KleinPipeline loaded with model_cpu_offload in %.2fs "
            "(boot+%.2fs)",
            time.monotonic() - t0,
            time.monotonic() - _BOOT_T0,
        )
    return _PIPE


# Aspect → (width, height) in multiples of 16 (FLUX needs that).
# Vertical 9:16 for Shorts and 16:9 for long-form are the only two
# we ship; 1:1 is fallback for thumbnails.
_ASPECT_DIMS = {
    "9:16": (768, 1344),
    "16:9": (1344, 768),
    "1:1":  (1024, 1024),
    "4:3":  (1152, 896),
    "3:4":  (896, 1152),
}


class GenerateIn(BaseModel):
    prompt: str
    # FLUX.2 klein is guidance-distilled; guidance_scale is fixed at
    # ~1.0 by training (the BFL model card uses 1.0 in its example).
    # We accept guidance_scale to keep the API stable across providers
    # but clamp it to [1.0, 1.0] server-side. Negative prompts have no
    # effect on guidance-distilled pipelines so we don't expose one.
    aspect: str = Field("9:16")
    width: int | None = None
    height: int | None = None
    # 4 = the BFL model-card distilled step count. Allow 2-8 for
    # experimentation (lower = faster but lower quality, higher =
    # marginal quality gain at 2× cost).
    steps: int = Field(4, ge=2, le=8)
    guidance_scale: float = Field(1.0, ge=1.0, le=1.0)  # locked
    seed: int | None = None
    output: str = "inline"  # "inline" | "gcs"
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
        # Caller-specified dims, round to multiples of 16.
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
        logger.exception("flux2-klein generate failed")
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
        "diffusers_class": "Flux2KleinPipeline",
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
            object_name=f"{req.gcs_object_prefix or 'flux2-klein/'}"
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
