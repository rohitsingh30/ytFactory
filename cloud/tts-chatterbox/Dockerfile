# ytFactory TTS — Chatterbox only Cloud Run GPU service
#
# Single-purpose: Resemble Chatterbox emotional voice cloning on
# NVIDIA L4. ~6× faster than M2 Max MPS path.
#
# Image size: ~5 GB compressed.

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

# Audit D3.50 — pre-fix the comment said "use cu126" but the install
# index was actually `cu124`. Comment-vs-code drift. The cu124 wheels
# DO exist for torch==2.6.0 (verified at
# https://download.pytorch.org/whl/cu124/torch/ on 2026-05-13), so
# the install URL is correct; the comment was out of date.
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install \
        torch==2.6.0 \
        torchaudio==2.6.0 \
        --index-url https://download.pytorch.org/whl/cu124

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# Chatterbox itself — --no-deps so its torch==2.6.0 strict pin doesn't
# fight (we already installed compatible cu124 wheels above).
RUN python -m pip install --no-deps chatterbox-tts==0.1.7

# Audit D3.40 — pre-fix this had no smoke-test step (Higgs and IndicF5
# Dockerfiles do have one). When --no-deps drops a critical transitive
# dep, the failure only surfaces at FIRST request — minutes after a
# successful build that rolled out into prod. Smoke-importing the
# model module here makes such a regression a build-time failure
# instead of a runtime one. Note: weights download is best-effort
# (|| true) below, so the smoke-test only checks the import surface,
# not the actual inference.
RUN python -c "from chatterbox.tts import ChatterboxTTS; print('chatterbox import OK')"

# Pre-pull Chatterbox weights
RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('ResembleAI/chatterbox')" || true

WORKDIR /app
COPY server.py ./
COPY cloud_run_json_exporter.py ./
COPY otel_init.py ./

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "300", "--log-level", "info"]
