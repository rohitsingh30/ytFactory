# ytFactory render-worker v2 — Cloud Run JOB image (Firestore-driven).
#
# Reads YTFACTORY_JOB_ID from env, fetches the job doc from Firestore,
# runs the render pipeline using the existing Cloud Run TTS + image
# services, writes timeline events back to Firestore so the UI's poll
# loop sees live progress, uploads the mp4 + thumbnail to GCS, and
# marks the job done.
#
# This image replaces the laptop agent for production renders. The
# laptop agent + sim worker both still exist as fallbacks (see
# docs/cloudrun_render_worker.md).
#
# Build context: the ytFactory repo root.
#
# Build (from repo root):
#   docker build -f cloud/render-worker-v2/Dockerfile -t ytfactory-render-worker-v2:test .
# Or via cloud/render-worker-v2/deploy.sh (Cloud Build).
#
# Cloud Run JOB shape: CPU 4, mem 8Gi, no GPU. The renderer's heavy
# compute (TTS + image-gen) is delegated to the existing Cloud Run
# services (ytfactory-tts-chatterbox, ytfactory-tts-indicparler,
# ytfactory-image-flux2-klein). This worker just orchestrates +
# ffmpeg-composes + faster-whisper-aligns.

FROM python:3.12-slim AS base

# System deps. ffmpeg for compose; build-essential for the few packages
# that need to compile; curl for health probes.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        curl \
        ca-certificates \
        build-essential \
        && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Python deps. CPU-only torch + faster-whisper for ASR (Apple-Silicon
# whisper-mlx isn't usable on Linux); HTTP clients for the GPU
# services; Anthropic SDK for rewrite/cast (replaces the claude CLI
# the laptop uses).
WORKDIR /workspace
COPY cloud/render-worker-v2/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r /tmp/requirements.txt

# Copy code. Channel configs change less often than the pipeline, but
# both are baked in. Per-render state lives in Firestore + GCS.
#
# As of the 2026-05-05 channel-layout reorg, every channel YAML lives
# under pipeline/channels/<slug>.yaml (and per-channel variants live
# under pipeline/variants/<slug>/), so the wholesale `COPY pipeline/`
# below already brings every config into the image — no per-channel
# COPY lines needed. (Pre-reorg the configs lived at
# <slug>/config.yaml and we had a stack of fragile per-channel COPYs
# here that broke when those files moved.)
COPY pipeline/      /workspace/pipeline/
COPY control/       /workspace/control/
COPY scripts/       /workspace/scripts/

COPY cloud/render-worker-v2/entrypoint.py /workspace/cloud/render-worker-v2/entrypoint.py
COPY cloud/render-worker-v2/otel_init.py /workspace/otel_init.py

ENV PYTHONPATH=/workspace
ENV PYTHONUNBUFFERED=1
WORKDIR /workspace

ENTRYPOINT ["python", "/workspace/cloud/render-worker-v2/entrypoint.py"]
