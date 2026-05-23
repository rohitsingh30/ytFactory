# ytFactory ASR — faster-whisper Cloud Run GPU service
#
# Single-purpose: faster-whisper (CTranslate2) on NVIDIA L4. Used by
# both the short engine (timeline.asr_beats) and long engine
# (timeline.asr_anchors) for word-timestamped narration alignment.
#
# ~3× faster than openai-whisper at the same WER. ~2 GB compressed.

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
        build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/local/bin/python

# CTranslate2 wheels are CUDA-12-compatible out of the box; we don't
# need a separate torch install for faster-whisper (it doesn't depend
# on torch). cuDNN libraries come from the nvidia/cuda runtime base.

RUN python -m pip install --upgrade pip setuptools wheel

# faster-whisper / CTranslate2 needs cuDNN 8 at runtime. The
# nvidia/cuda:12.4.0-runtime-ubuntu22.04 base ships CUDA libs but
# NOT cuDNN — first /align request crashed with
# "Could not load library libcudnn_ops_infer.so.8".
#
# pip install the NVIDIA cuDNN 8 wheel for CUDA 12. Pin to the major
# version that ctranslate2 4.4 was built against. The wheel ships
# the .so files at site-packages/nvidia/cudnn/lib which need to be
# on LD_LIBRARY_PATH — set that explicitly below.
RUN python -m pip install nvidia-cudnn-cu12==8.9.7.29 nvidia-cublas-cu12

# Add NVIDIA pip wheels' .so dirs to LD_LIBRARY_PATH so the dynamic
# linker finds libcudnn_ops_infer.so.8 + libcublas.so at faster-whisper
# import + first-inference time.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib:${LD_LIBRARY_PATH}

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

# Smoke-import the model module — catches any missing transitive dep
# (e.g. ctranslate2 ↔ cuDNN ABI mismatch) at build time instead of
# at first request. Same pattern as cloud/tts-chatterbox D3.40.
RUN python -c "from faster_whisper import WhisperModel; print('faster_whisper import OK')"

# Pre-pull large-v3 model weights. Pinned to large-v3 for determinism;
# the bigbang PR can switch to ``--build-arg`` if a channel needs a
# different model. Best-effort (|| true) so the build doesn't fail
# when HuggingFace hub is rate-limited.
RUN python -c "from faster_whisper import WhisperModel; \
    WhisperModel('large-v3', device='cpu', compute_type='int8'); \
    print('large-v3 weights pre-pulled')" || true

WORKDIR /app
COPY server.py ./
COPY _tel_track_io.py ./
COPY cloud_run_json_exporter.py ./
COPY otel_init.py ./

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "300", "--log-level", "info"]
