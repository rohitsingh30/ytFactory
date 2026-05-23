# ytFactory cinematic editor — Cloud Run SERVICE.
#
# Sibling of cloud/render-worker-v2 (which is a JOB), but this one is
# a SERVICE because each /edit request is bounded (~30s polish,
# ~2-5min assemble). HTTP API matches the laptop client in
# pipeline/editing/cloudrun.py.
#
# CPU-only (ffmpeg is the workload). 4 vCPU, 8 GiB is enough for
# 1080p Shorts; bump CPU for 4K/long-form.
#
# Build context = repo root (so we can COPY pipeline/editing/ and
# pipeline/cloud/ verbatim — same code as the laptop executor).
#
# Build:
#   gcloud builds submit . \
#     --config=cloud/editing-agent/cloudbuild.yaml \
#     --substitutions=_IMAGE=...
# Or via cloud/editing-agent/deploy.sh.

FROM python:3.12-slim AS base

# ffmpeg + PySceneDetect deps. ffmpeg is the heavy
# workload; PySceneDetect (in requirements.txt) is the scene-boundary
# detector for assemble-clips mode. build-essential for the few packages
# that need to compile.
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

WORKDIR /workspace
COPY cloud/editing-agent/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy the pipeline modules we share with the laptop executor.
# pipeline/editing/ has the EDL schema + executor (same code that runs
# locally); pipeline/cloud/ has the auth helper. pipeline/llm/ would
# be needed only if the cloud service ever invoked the planner itself
# (today the planner runs on the caller side and the service just
# executes a pre-validated EDL), so we keep the image lean.
COPY pipeline/editing/  /workspace/pipeline/editing/
COPY pipeline/cloud/    /workspace/pipeline/cloud/
COPY pipeline/__init__.py /workspace/pipeline/__init__.py
COPY cloud/editing-agent/server.py /workspace/server.py
COPY cloud/editing-agent/_tel_track_io.py /workspace/_tel_track_io.py
COPY cloud/editing-agent/otel_init.py /workspace/otel_init.py
COPY cloud/editing-agent/cloud_run_json_exporter.py /workspace/cloud_run_json_exporter.py

ENV PYTHONPATH=/workspace
ENV PYTHONUNBUFFERED=1
ENV PORT=8080
WORKDIR /workspace

# uvicorn binds to PORT (Cloud Run injects 8080).
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --timeout-keep-alive 120"]
