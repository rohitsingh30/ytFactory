# ytFactory — one-shot Cloud Run Job that pulls model snapshots from
# Hugging Face Hub directly into a GCS-Fuse-mounted bucket. Used to
# pre-populate gs://ytfactory-model-weights so every TTS / image
# Cloud Run service can mount the bucket at HF_HOME=/models/hf and
# skip its 5-8 min cold-start download. See docs/cloudrun_persistent_weights.md.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf \
    HF_HUB_ENABLE_HF_TRANSFER=1

RUN pip install --upgrade pip && \
    pip install \
        "huggingface_hub>=0.25,<2.0" \
        "hf_transfer>=0.1.8" \
        "google-cloud-storage>=2.18,<4.0"

WORKDIR /app
COPY stage.py ./

ENTRYPOINT ["python", "/app/stage.py"]
