# ytFactory TTS — CosyVoice 2 standalone Cloud Run GPU service
#
# WHY A SEPARATE IMAGE FROM cloud/tts/Dockerfile:
#   CosyVoice 2 (FunAudioLLM) pins:
#     - torch 2.3.1 (cu121)
#     - openai-whisper from source
#     - deepspeed, lightning, gradio
#     - tensorrt (optional, we disable load_trt at runtime)
#   The main ytfactory-tts image runs torch 2.4.1+cu124 for F5/Higgs/
#   Chatterbox. After 5 build iterations trying to coexist, we gave up
#   and isolated CosyVoice in its own container with its own native deps.
#
# Image size target: ~9-11 GB compressed (CUDA devel + torch cu121 +
# CosyVoice2-0.5B weights ~3 GB + tokenizers/CAM++/whisper assets).
# Cold-start image-pull on Cloud Run: ~2-3 min.
# Subsequent /synth on a warm instance: ~5-8 s for a 15-30 s clip.

FROM nvidia/cuda:12.1.0-devel-ubuntu22.04

# --- system deps ---
# `devel` (not `runtime`) because CosyVoice's deepspeed + a couple of
# tokenizer wheels build native extensions that need nvcc + headers at
# install time. Final container is bigger but reliably installable;
# main session burned 5 iterations trying to make `runtime` work.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf \
    TORCH_HOME=/models/torch \
    HF_HUB_ENABLE_HF_TRANSFER=0

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-pip python3-venv \
        ffmpeg curl ca-certificates \
        build-essential \
        git git-lfs \
        sox libsox-dev \
        unzip \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/local/bin/python \
    && git lfs install

# --- python deps ---
# Install torch 2.3.1+cu121 FIRST (matches CosyVoice's pin) so the
# ensuing requirements.txt resolve doesn't downgrade or ABI-break it.
#
# setuptools is pinned <81: openai-whisper==20231117 (CosyVoice's pin)
# has a setup.py that imports `pkg_resources`, which setuptools 81+
# stopped shipping in the default install. See the openai-whisper
# install step below — we use --no-build-isolation so its build env
# inherits this older setuptools.
RUN python -m pip install --upgrade pip wheel 'setuptools<81' && \
    python -m pip install \
        torch==2.3.1+cu121 \
        torchaudio==2.3.1+cu121 \
        --index-url https://download.pytorch.org/whl/cu121

# --- CosyVoice repo (provides cosyvoice/ python package + Matcha-TTS
#     submodule) ---
# Pinned to a known-good commit so subsequent rebuilds are reproducible.
# CosyVoice publishes itself as a git-installed package (no PyPI), so
# we clone into /opt/CosyVoice and put it on sys.path at runtime
# (see models/cosyvoice.py).
ARG COSYVOICE_REF=main
RUN git clone --depth 1 --recursive --shallow-submodules \
        --branch ${COSYVOICE_REF} \
        https://github.com/FunAudioLLM/CosyVoice.git /opt/CosyVoice

# --- our requirements.txt (FastAPI + GCS + a couple of shared utils)
#     installed BEFORE CosyVoice's so our pins win where they overlap. ---
COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# --- openai-whisper (CosyVoice's pinned 20231117 has a setup.py that
#     imports pkg_resources at build time). With pip's default build
#     isolation, pip would install latest setuptools (>=81) into the
#     build env, which dropped pkg_resources. --no-build-isolation
#     forces pip to use our parent env's setuptools<81 (pinned above)
#     for the build. ---
RUN python -m pip install --no-build-isolation openai-whisper==20231117

# --- CosyVoice's own requirements.txt ---
# We strip a few hostile pins:
#   - torch / torchaudio: already installed at the cu121 wheel above.
#     If we let pip see the bare `torch==2.3.1` here, it can pick the
#     CPU build off PyPI even with the extra-index-url present.
#   - tensorrt-cu12* (3 packages): optional, disabled at runtime via
#     load_trt=False. The wheels are big and brittle on Ubuntu 22.04.
#   - openai-whisper: installed separately above with --no-build-isolation.
# We KEEP onnxruntime-gpu==1.18.0 — CosyVoice's requirements.txt has
# the matching `--extra-index-url` line at the top so pip can resolve
# the cu12-flavoured wheel (PyPI's onnxruntime-gpu stops at 1.17.x).
# Anything that fails to install in this layer should be added to the
# strip list, NOT pinned in our requirements.txt — we don't want those
# pins leaking into the main ytfactory-tts service.
RUN cd /opt/CosyVoice && \
    cp requirements.txt requirements.original.txt && \
    grep -viE '^(torch|torchaudio|tensorrt-cu12|openai-whisper)([=<>!~ -]|$)' \
        requirements.original.txt > requirements.cleaned.txt && \
    python -m pip install -r requirements.cleaned.txt

# --- pre-pull CosyVoice2-0.5B weights into the image ---
# Without this, first cold-start /synth on a fresh instance pays
# ~3 GB of HF download (~30-60 s on Cloud Run). Pre-pulling moves that
# cost into the image build (~once per deploy).
RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('FunAudioLLM/CosyVoice2-0.5B', \
        local_dir='/models/cosyvoice/CosyVoice2-0.5B')"

# --- app code ---
WORKDIR /app
COPY server.py handler.py ./
COPY otel_init.py ./
COPY models/ ./models/

# --- runtime ---
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "120", "--log-level", "info"]
