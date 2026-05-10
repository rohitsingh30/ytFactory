#!/usr/bin/env bash
# Boot order:
#   1. bgutil PO-token provider (Node HTTP server on $PORT_BGUTIL).
#      yt-dlp's Python plugin talks to it via http://127.0.0.1:$PORT_BGUTIL.
#   2. uvicorn FastAPI app on $PORT — the public Cloud Run entrypoint.
#
# tini (set as ENTRYPOINT) reaps zombies + forwards SIGTERM cleanly.

set -euo pipefail

PORT_BGUTIL="${PORT_BGUTIL:-4416}"
PORT="${PORT:-8080}"

echo "[entrypoint] node version: $(node --version 2>&1 || echo MISSING)"
echo "[entrypoint] node path: $(which node 2>&1 || echo MISSING)"

echo "[entrypoint] starting bgutil PO-token provider on :${PORT_BGUTIL}"
# Run the compiled JS server. The Brainicism/bgutil-ytdlp-pot-provider
# build emits build/main.js; it picks up its bind port from the
# PORT env var.
( cd /opt/bgutil/server && PORT="${PORT_BGUTIL}" node build/main.js ) &
BGUTIL_PID=$!

# Wait briefly for bgutil to bind. yt-dlp tolerates a slow first call.
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf "http://127.0.0.1:${PORT_BGUTIL}/ping" > /dev/null 2>&1; then
    echo "[entrypoint] bgutil ready (tick ${i})"
    break
  fi
  sleep 0.5
done

echo "[entrypoint] starting uvicorn on :${PORT}"
exec uvicorn server:app --host 0.0.0.0 --port "${PORT}" --workers 1
