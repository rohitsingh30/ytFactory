# ytFactory Image — Z-Image-Turbo 6B Cloud Run GPU service
#
# Model: Tongyi-MAI/Z-Image-Turbo (Apache 2.0, Nov 2025)
# 6 B Single-Stream Diffusion Transformer, distilled to 8 NFEs.
# RL-tuned for aesthetic quality. ~12 GB VRAM at bf16.
# Repo: 32.9 GB / 32 files staged to gs://ytfactory-model-weights-v2/flat/.
# Cold-start expected ~60-120 s (32 GB to read from GCS Fuse mount,
# load bf16, push 12 GB to CUDA). Warm /generate ~2-3 s at 1024² with
# 8 NFEs (= 9 inference steps in the diffusers pipeline).
#
# Image size: ~5 GB compressed (no weights baked in).

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

# Torch 2.5.1+cu124: required for diffusers 0.38 FlashAttention-3
# schema decorator. Validated via dep-probe Cloud Build 2026-05-07.
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install \
        torch==2.5.1+cu124 \
        torchvision==0.20.1+cu124 \
        --index-url https://download.pytorch.org/whl/cu124

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# Sanity-check at build time: ZImagePipeline must be importable from
# the pinned diffusers. Catches a wheel-resolution surprise before
# we waste a cold-start.
RUN python -c "from diffusers import ZImagePipeline; print('ZImagePipeline import OK')"

WORKDIR /app
COPY server.py ./

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "300", "--log-level", "info"]
