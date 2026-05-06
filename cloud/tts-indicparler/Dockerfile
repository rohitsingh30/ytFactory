# ytFactory TTS — Indic Parler-TTS only Cloud Run GPU service
#
# Single-purpose: AI4Bharat / HuggingFace Indic Parler-TTS for 21
# Indian languages incl Hindi (Apache 2.0). Description-based voice
# control + automatic language detection from prompt.
#
# Image size: ~7 GB compressed (CUDA + torch + ~2.6 GB Parler weights).
# Cold-start ~50-70 s. Warm RTF ~0.4-0.6 on L4.
#
# IMPORTANT: model is auto-gated — once you've accepted ToS at
# https://huggingface.co/ai4bharat/indic-parler-tts, your HF read
# token can pull it. Pass HF_TOKEN as a build-arg.

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
        torchaudio==2.4.1+cu124 \
        --index-url https://download.pytorch.org/whl/cu124

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# Parler-TTS is GitHub-only (no PyPI). Install latest main.
RUN python -m pip install "git+https://github.com/huggingface/parler-tts.git"

# Pre-pull Indic Parler weights (~2.6 GB) — gated, needs HF token.
ARG HF_TOKEN=""
RUN if [ -n "$HF_TOKEN" ]; then \
      echo "Pulling Indic Parler weights with HF token…"; \
      python -c "import os; os.environ['HF_TOKEN']='${HF_TOKEN}'; \
        from huggingface_hub import snapshot_download; \
        snapshot_download('ai4bharat/indic-parler-tts', token=os.environ['HF_TOKEN'])"; \
    else \
      echo "WARN: HF_TOKEN not provided; weights will download on first /readyz call"; \
    fi

WORKDIR /app
COPY server.py ./

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "300", "--log-level", "info"]
