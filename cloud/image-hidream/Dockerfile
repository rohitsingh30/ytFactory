# ytFactory Image — HiDream-I1 Full Cloud Run GPU service
#
# Single-purpose: HiDream-I1-Full 17B (MIT license) — best
# open-source for cartoon, mythological, kids' illustration. Uses
# Llama-3.1-8B as text encoder for strong prompt following.
#
# Image size: ~22 GB compressed. Cold-start ~90s. Warm ~10-25s/image.
#
# Note: HiDream has no PyPI package — install from GitHub.

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

# Torch first (cu124)
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install \
        torch==2.4.1+cu124 \
        torchvision==0.19.1+cu124 \
        --index-url https://download.pytorch.org/whl/cu124

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# HiDream installs from their GitHub. Their requirements.txt includes
# flash-attention which needs CUDA + nvcc — we use --no-deps to skip
# flash-attn (HiDream falls back to standard attention; slightly slower
# but works). If perf becomes an issue, switch base to devel image.
RUN git clone --depth 1 https://github.com/HiDream-ai/HiDream-I1.git /opt/HiDream-I1

# Pre-pull HiDream-I1-Full weights (~17 GB) + Llama-3.1-8B (~16 GB)
# text encoder. Total ~33 GB on disk.
#
# Audit S1.11 — uses BuildKit secret mount instead of ARG HF_TOKEN
# so the token isn't baked into the image layer history (visible
# via `docker history`). Llama-3.1-8B is gated on HF; the secret
# unlocks it. Without the secret the Llama download fails fast and
# the model loads on first inference instead.
RUN --mount=type=secret,id=hf_token \
    python -c "from huggingface_hub import snapshot_download; \
      snapshot_download('HiDream-ai/HiDream-I1-Full')" && \
    if [ -s /run/secrets/hf_token ]; then \
      HF_TOKEN_VALUE="$(cat /run/secrets/hf_token)" \
      python -c "import os; from huggingface_hub import snapshot_download; \
        snapshot_download('meta-llama/Meta-Llama-3.1-8B-Instruct', \
          token=os.environ['HF_TOKEN_VALUE'])" \
      || echo "Llama-3.1-8B gated download failed; will retry on first inference"; \
    else \
      echo "hf_token secret not provided; Llama-3.1-8B will load on first inference"; \
    fi

WORKDIR /app
COPY server.py ./
COPY cloud_run_json_exporter.py ./
COPY otel_init.py ./

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "300", "--log-level", "info"]
