#!/usr/bin/env bash
# Local launcher for the cloud control plane (FastAPI app at control/).
#
# Production runs at the Cloud Run URL — this script is only for
# iterating on `control/` code locally before redeploying.
# The legacy `web.server:app` still has its own launcher at
# `scripts/serve.sh` for research-dashboard work.
#
# Usage:
#   ./scripts/serve_cloud.sh                   # foreground, with reload
#   ./scripts/serve_cloud.sh --no-reload       # never reload
#
# Pre-reqs:
#   - .venv with requirements installed
#   - .env with AZURE_OPENAI_*, YTFACTORY_AGENT_TOKEN
#   - YTFACTORY_QUEUE_BACKEND=memory  (default in dev; firestore in prod)

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "error: .venv not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [ -f .env ]; then
  set -a; source .env; set +a
fi

pkill -f "uvicorn control.server_dev" 2>/dev/null || true
sleep 0.5

RELOAD_FLAGS=(--reload --reload-dir control --reload-dir web)
# Audit D3.26 — pre-fix this listed `--reload-dir shared` but the
# repo has no `shared/` dir, so uvicorn warned at startup and
# silently dropped the entry. Watch the dirs that actually exist.
if [ "${1:-}" = "--no-reload" ]; then
  RELOAD_FLAGS=()
fi

exec .venv/bin/uvicorn control.server_dev:app \
  --host 127.0.0.1 --port 8765 \
  "${RELOAD_FLAGS[@]}"
