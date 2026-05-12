# ytFactory TTS — Higgs Audio v2 only Cloud Run GPU service
#
# Single-purpose: Higgs Audio v2 3B emotional + multilingual TTS on
# NVIDIA L4. Voice clone from a 5-15s reference WAV.
#
# Image size: ~14 GB compressed (CUDA + torch + 12 GB Higgs weights).
# Cold-start ~60 s (image pull + uvicorn + Higgs load).
# Warm: ~3-8 s for a 15 s chunk.

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
        build-essential git \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/local/bin/python

# Torch first (cu124) — pin BEFORE Higgs to prevent its requirements
# from pulling a different version.
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install \
        torch==2.4.1+cu124 \
        torchaudio==2.4.1+cu124 \
        --index-url https://download.pytorch.org/whl/cu124

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# Higgs Audio v2 — install from GitHub. Use --no-deps so its own
# requirements (which include their pinned transformers/accelerate
# versions that conflict with ours) don't override our pins.
RUN git clone --depth 1 https://github.com/boson-ai/higgs-audio.git /opt/higgs-audio && \
    cd /opt/higgs-audio && \
    python -m pip install -e . --no-deps

# --- Audiotools + DAC chain ---
#
# **VALIDATED LOCALLY 2026-05-05** — the import chain
# `from boson_multimodal.serve.serve_engine import HiggsAudioServeEngine`
# was confirmed working in a Python 3.10 venv with these exact packages.
# (See docs/cloud_service_dep_playbook.md for the validation pattern.)
#
# Higgs's vendored DAC at boson_multimodal/audio_processing/descriptaudiocodec/
# does TWO non-trivial imports:
#   1. `from audiotools import AudioSignal` → needs descript-audiotools
#   2. `from dac.nn.layers import Snake1d`  → needs descript-audio-codec
# Both packages drag in tensorboard / pesq / markdown-it-py / rich / etc
# transitively. We let pip resolve them WITH deps (no --no-deps cherry-pick)
# because that's what the local validation used.
#
# `descript-audiotools` pins protobuf<3.20 in its install_requires which
# theoretically conflicts with google-cloud-storage's protobuf>=3.19.5.
# In practice, pip's resolver picks a version satisfying both bounds
# (3.19.5..3.19.x) and the build succeeds. If it ever hard-fails, fall
# back to `--no-deps` + manual transitive install (commit history v6/v7
# documents the cherry-pick set if needed).
RUN python -m pip install \
        descript-audiotools==0.7.2 \
        descript-audio-codec==1.0.0

# --- BUILD-TIME IMPORT SMOKE TEST ---
# Catches missing-module bugs at BUILD time (15s feedback) instead of
# at /readyz time (~30 min build + 13 min image pull + crash). Walks the
# same import chain that serve_engine.py executes on first /synth call.
#
# Pattern from docs/cloud_service_dep_playbook.md (the 6-step rule).
RUN python -c "import boson_multimodal; from boson_multimodal.dataset import chatml_dataset; from boson_multimodal.audio_processing import higgs_audio_tokenizer; from boson_multimodal.model import higgs_audio; from boson_multimodal.data_collator import higgs_audio_collator; from boson_multimodal.serve.serve_engine import HiggsAudioServeEngine; from boson_multimodal.data_types import ChatMLSample, Message, AudioContent; print('IMPORT_SMOKE_OK')"

# --- HF weight pre-pull SKIPPED (per 2026-05-06 decision) ---
# Runtime download on first /readyz instead of bake-in. Reasons:
#   * v9 with weights baked → 14 GB image, manifest-commit hung in Cloud
#     Build push for 70+ min before being killed.
#   * Smaller image (~5 GB) builds + pushes in ~10 min cleanly.
#   * Cold-start /readyz ~5-8 min (one-time), warm calls normal.
# If we ever migrate back to in-image weights:
#   * Use --machine-type=e2-highcpu-32 + --disk-size=200 build worker
#   * Use cloudbuild.yaml with --build-arg HF_TOKEN substitution
#   * Watch for manifest-commit hangs on >10 GB images

WORKDIR /app
COPY server.py ./
COPY otel_init.py ./

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "300", "--log-level", "info"]
