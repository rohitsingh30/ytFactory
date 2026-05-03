#!/usr/bin/env bash
# Canonical launcher for the ytFactory web UI.
#
# Why this script exists:
#   `uvicorn web.server:app --reload` (no --reload-dir) watches the entire
#   project root, including data/. Every file the pipeline writes (cache,
#   intermediate, critiques, shorts) triggers a reload, which wipes the
#   in-memory JOBS dict and breaks the live UI trail mid-job. Always
#   launch via this script so the right reload scope is used.
#
# Usage:
#   ./scripts/serve.sh              # foreground
#   ./scripts/serve.sh --no-reload  # production-ish, never reloads
#
# Token: if /tmp/ytfactory_token exists, it's used as YTFACTORY_TOKEN
# (matches the convention in NEXT_SESSION.md / web/README.md).

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "error: .venv not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [ -f /tmp/ytfactory_token ]; then
  export YTFACTORY_TOKEN="$(cat /tmp/ytfactory_token)"
fi

# Stop any existing instance so this is idempotent.
pkill -f "uvicorn web.server" 2>/dev/null || true
sleep 0.5

# What needs reload vs what doesn't:
#
#   Imported by server.py at startup → MUST trigger uvicorn reload:
#     web/server.py, web/static/*  (--reload-dir web covers these)
#     sources/base.py              (RawStory dataclass)
#     pipeline/telemetry.py        (tlm.track wrapper used in routes)
#
#   Run as subprocesses (asyncio.create_subprocess_exec) → fresh
#   Python interpreter on every button-click, picks up edits
#   automatically without reload, NEVER add to --reload-dir:
#     pull_stories.py, make_shorts.py, and the bulk of pipeline/*
#     and sources/* (rewrite, cast, critic, images, beats, compose,
#     captions, reddit_api, drama, …).
#
# Don't broaden the reload scope to include those subprocess paths —
# uvicorn reload restarts the server process, which wipes the
# in-memory JOBS dict and breaks live UI trails mid-job. The two
# server-imported files outside web/ (sources/base.py,
# pipeline/telemetry.py) are stable enough that requiring a manual
# restart on those rare edits is the right tradeoff.
RELOAD_FLAGS=(--reload --reload-dir web)
if [ "${1:-}" = "--no-reload" ]; then
  RELOAD_FLAGS=()
fi

exec .venv/bin/uvicorn web.server:app \
  --host 127.0.0.1 --port 8765 \
  "${RELOAD_FLAGS[@]}"
