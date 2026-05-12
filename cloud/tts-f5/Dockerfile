# ytFactory TTS — F5-only Cloud Run GPU service
#
# Single-purpose: F5-TTS English voice cloning on NVIDIA L4.
# Replicates the local pipeline/tts/f5.py output character (same
# checkpoint, same flow-matching params).
#
# Image size: ~6 GB compressed. Cold-start ~40 s. Warm RTF ~0.21-0.24.

FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04

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

RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install \
        torch==2.4.1+cu124 \
        torchaudio==2.4.1+cu124 \
        --index-url https://download.pytorch.org/whl/cu124

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# Pre-pull F5TTS_Base + Vocos vocoder
RUN python -c "from cached_path import cached_path; \
    cached_path('hf://SWivid/F5-TTS/F5TTS_Base/model_1200000.safetensors'); \
    cached_path('hf://SWivid/F5-TTS/F5TTS_Base/vocab.txt')" && \
    python -c "from f5_tts.infer.utils_infer import load_vocoder; \
    load_vocoder('vocos', is_local=False, local_path=None, device='cpu', hf_cache_dir=None)"

WORKDIR /app
COPY server.py handler.py ./
COPY cloud_run_json_exporter.py ./
COPY otel_init.py ./
COPY models/ ./models/

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "120", "--log-level", "info"]
