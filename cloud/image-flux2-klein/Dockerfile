# ytFactory Image — FLUX.2 [klein] 4B Cloud Run GPU service
#
# Model: black-forest-labs/FLUX.2-klein-4B (Apache 2.0, Jan 2026)
# 4 B rectified flow transformer + 8 B Qwen3 text embedder.
# Distilled to 4 inference steps. ~13 GB VRAM at bf16 → comfortable
# headroom on L4 24 GB.
# Repo: 24 GB / 52 files staged to gs://ytfactory-model-weights/flat/.
# Cold-start expected ~30-60 s (read 24 GB from GCS Fuse mount, load
# bf16, single .to("cuda") of ~13 GB model). Warm /generate ~0.5-0.8 s
# at 1024² with 4 steps.
#
# Image size: ~5 GB compressed (no weights baked in, weights live in
# the GCS Fuse-mounted bucket at /models/hf/flat/black-forest-labs/...).

FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf \
    HF_HUB_DISABLE_XET=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-pip python3-venv \
        ffmpeg curl ca-certificates \
        build-essential git \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/local/bin/python

# Torch first (cu124 wheels match the base image's CUDA 12.4 runtime).
# torch 2.5.1 is required: torch 2.4.x's _library/infer_schema chokes
# on diffusers' FlashAttention-3 schema decorator at import time
# ("infer_schema(func): Parameter q has unsupported type torch.Tensor").
# Validated pin combo via dep-probe Cloud Build 2026-05-07.
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install \
        torch==2.5.1+cu124 \
        torchvision==0.20.1+cu124 \
        --index-url https://download.pytorch.org/whl/cu124

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# Sanity-check at build time: Flux2KleinPipeline must be importable
# from the pinned diffusers. Catches a wheel-resolution surprise
# before we waste a cold-start. (Diffusers 0.38.0+ ships this class
# in stable; no need for git+main.)
RUN python -c "from diffusers import Flux2KleinPipeline; print('Flux2KleinPipeline import OK')"

WORKDIR /app
COPY server.py ./

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "300", "--log-level", "info"]
