# ytFactory stats-refresh — Cloud Run JOB image.
#
# Periodically refreshes the per-account YouTube stats cache that lives
# at gs://$YTFACTORY_STATE_BUCKET/data/research/youtube/<account>.json.
# Used by:
#   - pipeline.research.youtube.load_account / load_all_cached
#   - pipeline.research.aggregator.build_videos / build_channels
#
# Triggered by Cloud Scheduler ytfactory-stats-refresh-cron (hourly).
# See cloud/stats-refresh/deploy.sh + docs/youtube_stats_refresh.md.
#
# This image is intentionally TINY (no torch, no faster-whisper, no
# diffusers — it just calls youtube.googleapis.com via the Google API
# Python client and writes JSON to GCS). Build context = repo root.

FROM python:3.12-slim AS base

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY cloud/stats-refresh/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Just the pipeline code path that fetch_account needs. NO web/, NO
# control/, NO scripts/ — keep the image small.
COPY pipeline/ /workspace/pipeline/

ENV PYTHONPATH=/workspace
ENV PYTHONUNBUFFERED=1
WORKDIR /workspace

# Default to refreshing every channel discovered via
# pipeline/channels/*.yaml. Override at job-execute time to refresh a
# single account by passing --account=<slug>.
ENTRYPOINT ["python", "-m", "pipeline.research.youtube"]

COPY cloud/stats-refresh/otel_init.py /workspace/otel_init.py
