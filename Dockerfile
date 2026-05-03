# ytFactory control plane — slim Cloud Run image.
# The control plane is a stateless FastAPI service. Heavy ML deps live
# on the laptop only; this image stays under ~200 MB.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements-control.txt ./
RUN pip install -r requirements-control.txt

# Copy only what the control plane needs.
# (shared/schema.py was merged into control/schema.py in the 2026-05-03
# reorg so there's no more `shared/` to copy.)
COPY control/ ./control/
COPY web/static/ ./web/static/

# Per-channel config + upload records — required by the live dashboard
# (control/dashboard_routes.py). Each channel's config.yaml + uploads/
# subtree get baked in. Cache/scratch/footage/etc. are gitignored so
# only the small curated bits land.
COPY historyrecapped/      ./historyrecapped/
COPY hindutavaanimated/    ./hindutavaanimated/
COPY mystoriesanimated/    ./mystoriesanimated/
COPY rhymetimejunction/    ./rhymetimejunction/
COPY sportstoriesanimated/ ./sportstoriesanimated/

# Cloud Run sets PORT; default to 8080 for local docker run.
ENV PORT=8080
EXPOSE 8080

# server_dev.py is the prod entry point (one app, dev/prod parity).
# Use 1 worker — multiple workers would split the in-memory rate-limit
# state. Concurrency comes from async, not multi-process.
CMD exec uvicorn control.server_dev:app --host 0.0.0.0 --port ${PORT} --workers 1
