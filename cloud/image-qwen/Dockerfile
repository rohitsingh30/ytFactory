# ytFactory Image — Qwen-Image only Cloud Run GPU service
# Single-purpose: Qwen-Image (Apache 2.0) — best photoreal + faces + in-image text.
# Image ~16 GB compressed. Cold-start ~60-90s. Warm ~5-15s/image at 1328x1328.

FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-pip python3-venv \
        ffmpeg curl ca-certificates \
        build-essential git \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/local/bin/python

RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install \
        torch==2.4.1+cu124 \
        torchvision==0.19.1+cu124 \
        --index-url https://download.pytorch.org/whl/cu124

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# Qwen-Image requires the latest diffusers from main
RUN python -m pip install "git+https://github.com/huggingface/diffusers"

# DO NOT pre-pull Qwen weights into the image (~20 GB)
# Cloud Build's default worker has ~30 GB disk; weights + base layers
# overflow → "no space left on device". Instead, download on first
# cold-start (~5-8 min one-time cost). Subsequent calls warm instantly.
# If we ever switch to a larger build worker (e2-highcpu-32 needs an
# additional regional quota), uncomment to bake weights in.
# RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen-Image')"

WORKDIR /app
COPY server.py ./

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "300", "--log-level", "info"]
