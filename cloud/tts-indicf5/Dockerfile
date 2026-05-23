# ytFactory TTS — IndicF5 only Cloud Run GPU service
#
# Single-purpose: AI4Bharat IndicF5 — F5-TTS architecture fine-tuned
# on 1417h of curated Indian speech (Rasa, IndicTTS, LIMMITS24,
# IndicVoices-R) supporting 11 Indic languages including Hindi.
#
# Voice clone from a 5-15s reference WAV. MIT licensed.
#
# Image size: ~5 GB compressed (CUDA + torch + IndicF5 fork code, no
# weights baked in). Cold-start ~30-60s (image pull + uvicorn + 1.4 GB
# weight download from HF + first-call torch.compile JIT).
# Warm: ~3-6s for a 15s chunk (RTF ~0.20-0.30 expected on L4).
#
# Installs IndicF5's own `f5_tts` package (fork from
# github.com/ai4bharat/IndicF5). Base: nvidia/cuda:12.4.0-runtime-
# ubuntu22.04 + torch 2.4.1+cu124.

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

# Torch FIRST (cu124) — pin BEFORE the IndicF5 fork to prevent its
# `torch>=2.0.0` spec from resolving a different version.
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install \
        torch==2.4.1+cu124 \
        torchaudio==2.4.1+cu124 \
        --index-url https://download.pytorch.org/whl/cu124

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# Re-pin torch to cu124 — `accelerate>=0.33.0` and a few other deps in
# requirements.txt list `torch>=2.0.0` as a hard dep, and pip's resolver
# happily upgraded our torch to 2.11.0 (and pulled nvidia-cudnn-cu13
# alongside) above. Re-install the cu124 wheel here to overwrite, AND
# force-reinstall the cu12 nvidia transitives because torch 2.11 left
# behind the cu13 cudnn which is ABI-incompatible with our 2.4.1.
#
# Without re-installing the nvidia-* libs, /synth crashes at first
# F.conv1d call with `cuDNN error: CUDNN_STATUS_NOT_INITIALIZED`
# (caught on v6, 2026-05-06).
RUN python -m pip install --force-reinstall --no-deps \
        torch==2.4.1+cu124 \
        torchaudio==2.4.1+cu124 \
        --index-url https://download.pytorch.org/whl/cu124 && \
    python -m pip install --force-reinstall --no-deps \
        nvidia-cudnn-cu12==9.1.0.70 \
        nvidia-cublas-cu12==12.4.2.65 \
        nvidia-cuda-runtime-cu12==12.4.99 \
        nvidia-cuda-cupti-cu12==12.4.99 \
        nvidia-cuda-nvrtc-cu12==12.4.99 \
        nvidia-cufft-cu12==11.2.0.44 \
        nvidia-curand-cu12==10.3.5.119 \
        nvidia-cusolver-cu12==11.6.0.99 \
        nvidia-cusparse-cu12==12.3.0.142 \
        nvidia-nccl-cu12==2.20.5 \
        nvidia-nvjitlink-cu12==12.4.99 \
        nvidia-nvtx-cu12==12.4.99 \
        triton==3.0.0

# Install IndicF5's own `f5_tts` fork from github with --no-deps.
# Their requirements.txt has wandb / bitsandbytes / matplotlib that
# we already pinned in our requirements.txt (or skipped). --no-deps
# avoids pulling in conflicting versions.
RUN python -m pip install --no-deps \
        "git+https://github.com/ai4bharat/IndicF5.git@main"

# --- BUILD-TIME IMPORT SMOKE TEST ---
# Catches missing-module bugs at BUILD time (15s feedback) instead of
# at /readyz time (~10 min build + ~13 min image pull + crash). Walks
# the same chain server.py executes on first /synth call.
#
# Pattern from docs/cloud_service_dep_playbook.md (the 6-step rule).
RUN python -c "import f5_tts; from f5_tts.infer.utils_infer import infer_process, load_model, load_vocoder, preprocess_ref_audio_text; from f5_tts.model import DiT; from transformers import PreTrainedModel, PretrainedConfig, AutoConfig, AutoModel; import vocos; import pydub; import soundfile; import torchdiffeq; import x_transformers; print('IMPORT_SMOKE_OK')"

# --- HF weight pre-pull SKIPPED ---
# Runtime download on first /readyz instead of bake-in. Smaller image
# (~5 GB) builds + pushes in ~10 min cleanly; first /readyz pays a
# one-time ~30-60s weight download (1.4 GB).

WORKDIR /app
COPY server.py ./
COPY cloud_run_json_exporter.py ./
COPY otel_init.py ./

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "300", "--log-level", "info"]
