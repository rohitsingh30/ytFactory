# ytFactory TTS — Cloud Run GPU service
#
# CUDA 12.4 + Python 3.12 + PyTorch 2.4 cu124 + F5-TTS upstream.
# Deployed to Cloud Run with NVIDIA L4 24GB in asia-southeast1.
#
# Stage 1 ships F5-TTS only (replicates current local quality, ~5-10×
# faster wall-clock vs M2 Max+MPS). Higgs Audio v2, CosyVoice 2, and
# Chatterbox are added in Stage 3 by extending models/ and bumping the
# image tag — no Dockerfile change required (lazy-imported).
#
# Image size target: ~6 GB compressed (CUDA runtime + torch + F5 weights).

FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04

# --- system deps ---
# Ubuntu 22.04 (Jammy) default apt provides Python 3.10. F5-TTS supports
# 3.10+ so we use the system Python rather than dragging in deadsnakes.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf \
    TORCH_HOME=/models/torch

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-pip python3-venv \
        ffmpeg curl ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/local/bin/python

# --- python deps ---
# Install torch FIRST (cu124 wheel) so f5-tts doesn't drag in CPU torch.
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install \
        torch==2.4.1+cu124 \
        torchaudio==2.4.1+cu124 \
        --index-url https://download.pytorch.org/whl/cu124

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# --- pre-pull F5-TTS weights + vocoder into the image ---
# Two HF artefacts get cold-loaded by f5_tts on first call:
#   1. F5TTS_Base checkpoint        (model_1200000.safetensors, ~1.4 GB)
#      F5TTS_Base specifically — matches the lucasnewman/f5-tts-mlx port
#      our local pipeline uses, so cloud and local outputs are
#      generated from the same weights.
#   2. Vocos vocoder                (charactr/vocos-mel-24khz, ~58 MB)
# Without pre-pulling #2, first synth on a cold container hits HF mid-call.
# Use cached_path (f5_tts's own helper) so the layout matches what
# load_vocoder/load_model expect at runtime.
RUN python -c "from cached_path import cached_path; \
    cached_path('hf://SWivid/F5-TTS/F5TTS_Base/model_1200000.safetensors'); \
    cached_path('hf://SWivid/F5-TTS/F5TTS_Base/vocab.txt')" && \
    python -c "from f5_tts.infer.utils_infer import load_vocoder; \
    load_vocoder('vocos', is_local=False, local_path=None, device='cpu', hf_cache_dir=None)"

# --- app code ---
WORKDIR /app
COPY server.py handler.py ./
COPY models/ ./models/

# --- runtime ---
# Cloud Run sets PORT=8080 by default. uvicorn binds 0.0.0.0:8080.
# 1 worker — matches Cloud Run concurrency=1 + 1-GPU-per-instance rule.
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "120", "--log-level", "info"]
