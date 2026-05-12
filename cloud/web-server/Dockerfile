# ytFactory orchestrator — Cloud Run SERVICE image (slim).
#
# Hosts the FastAPI app at /, /renders, /dashboard, /api/*. Does NOT
# render videos itself; calls out to:
#   - existing GPU services (chatterbox / indicf5 / flux2-klein)
#     when YTFACTORY_RENDER_BACKEND=local (won't usually run in cloud)
#   - the render-worker Cloud Run JOB
#     (`gcloud run jobs execute ytfactory-render-worker`) when
#     YTFACTORY_RENDER_BACKEND=cloudrun (the cloud default)
#
# Slim = no torch/diffusers/mflux/onnx. Just FastAPI + GCP clients +
# whatever pipeline.* the orchestrator's endpoints actually import.
#
# Build (from repo root):
#   docker build -f cloud/web-server/Dockerfile -t ytfactory-web:test .
# Cloud Build via deploy.sh.

FROM python:3.12-slim AS base

# ffmpeg + curl. ffmpeg is here for the few endpoints that do
# server-side compose (auto-thumb, scene-thumb, custom-thumb). For
# actual rendering, the worker JOB has its own ffmpeg.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# gcloud CLI — orchestrator uses `gcloud run jobs execute` to trigger
# the render-worker. ~80 MB. Could replace with the REST API later.
RUN curl -sSL https://sdk.cloud.google.com | bash > /dev/null && \
    ln -s /root/google-cloud-sdk/bin/gcloud /usr/local/bin/gcloud && \
    ln -s /root/google-cloud-sdk/bin/gsutil /usr/local/bin/gsutil

WORKDIR /workspace

# Slim dep set — single source of truth for cloud + laptop shared deps.
# laptop's requirements.txt does `-r requirements-control.txt` + extras;
# this image installs only requirements-control.txt (no torch/diffusers,
# no playwright, no faster-whisper).
#
# *** Every google.cloud.X import on the cloud-deployed code path
# *** (control/, web/, pipeline/) MUST be pinned in requirements-
# *** control.txt. Enforced by tests/test_cloud_runtime_deps.py.
# *** See docs/cloud_runtime_deps.md for the test contract — adding a
# *** new GCP SDK call site? Read that doc first or pytest will fail.
COPY requirements-control.txt /tmp/requirements-control.txt
RUN pip install --no-cache-dir -r /tmp/requirements-control.txt

# Code
COPY web/             /workspace/web/
COPY pipeline/        /workspace/pipeline/
# Phase 4 (2026-05-10) — control/* is now mounted into web/server.py.
# Bake the package into the image so import works at startup.
COPY control/         /workspace/control/

# Mirrored YouTube channel assets (avatar, banner, account JSONs).
# Total < 1 MB. /api/channels/{ch}/avatar.jpg + /api/channels/{ch}/banner.jpg
# read these from disk via pipeline.research.channel_assets.asset_path; without
# them the studio UI shows monogram fallbacks. .gcloudignore + .dockerignore
# both whitelist these subdirs explicitly.
COPY data/research/channel_assets/ /workspace/data/research/channel_assets/

# data/research/youtube/ + data/research/analytics/ are NO LONGER baked
# in. Pre-2026-05-10 they were committed to git and copied here, which
# let a test-fixture leak silently corrupt prod for several deploys
# (see docs/youtube_stats_refresh.md). Now:
#   - Dashboard stats: control/routes/dashboard_routes.py reads YouTube
#     Data API v3 live with YOUTUBE_API_KEY (in-memory cache, no on-disk
#     dependency).
#   - Per-account research cache: pipeline/research/youtube.py writes to
#     gs://$YTFACTORY_STATE_BUCKET/data/research/youtube/<account>.json
#     in cloud (see B1 in the token-issue plan).

# Channel configs + variants are now centralised under pipeline/
# (pipeline/channels/<slug>.yaml + pipeline/variants/<slug>/*.yaml)
# per the 2026-05-10 hygiene pass — already copied via `COPY pipeline/`
# above. No channel-named root dirs to bake in any more. Per-render
# state lives in GCS (`gs://ytfactory-prod-v2-state/`) and is read via
# pipeline.state_client at runtime.

ENV PYTHONPATH=/workspace
ENV PYTHONUNBUFFERED=1
ENV YTFACTORY_RENDER_BACKEND=cloudrun
ENV YTFACTORY_CLOUDRUN_JOB=ytfactory-render-worker-v2
ENV YTFACTORY_CLOUDRUN_REGION=asia-southeast1
WORKDIR /workspace

# Cloud Run sets PORT; uvicorn binds to it.
CMD ["sh", "-c", "uvicorn web.server:app --host 0.0.0.0 --port ${PORT:-8080}"]

COPY cloud/web-server/otel_init.py /workspace/otel_init.py
