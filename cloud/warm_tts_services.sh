#!/usr/bin/env bash
# Warm one or more ytfactory-tts-* Cloud Run services BEFORE a render.
# Pays the Chatterbox / IndicF5 cold-load up-front so the render's
# stage-1 TTS hits a warm container at sub-second per chunk.
#
# Designed to run from:
#   - laptop manually before a batch:    `./cloud/warm_tts_services.sh chatterbox`
#   - render entry point fire-and-forget (pipeline/render/footage_only.py
#     calls this for cloudrun_* providers before the synth call)
#   - GitHub Actions / Cloud Scheduler 5-10 min before a queued render
#
# Idempotent: hitting /readyz on an already-warm container returns
# instantly with `warm_s` near 0.
#
# Usage:
#   ./warm_tts_services.sh                      # warms chatterbox only (default — English Shorts)
#   ./warm_tts_services.sh chatterbox           # explicit
#   ./warm_tts_services.sh indicf5              # warms IndicF5 (Hindi)
#   ./warm_tts_services.sh chatterbox indicf5   # warms both in parallel

set -euo pipefail

PROJECT="${GCP_PROJECT:-ytfactory-prod-v3}"
REGION="${GCP_REGION:-asia-southeast1}"

# bash 3.2 (macOS default) doesn't have associative arrays; use case.
# URLs derived from .env CLOUDRUN_TTS_<MODEL>_URL — single source of truth.
url_for() {
  local key="$1"
  local var
  case "$key" in
    chatterbox|indicf5)
      var="CLOUDRUN_TTS_$(echo "$key" | tr 'a-z' 'A-Z')_URL"
      printenv "$var" 2>/dev/null && return 0
      # Fallback to .env file lookup if not in shell env.
      local env_file
      env_file="$(dirname "$0")/../.env"
      if [[ -f "$env_file" ]]; then
        grep "^${var}=" "$env_file" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'"
        return 0
      fi
      ;;
    *) echo "" ;;
  esac
}

if [[ $# -eq 0 ]]; then
  TARGETS=(chatterbox)
else
  TARGETS=("$@")
fi

warm_one() {
  local key="$1"
  local url
  url="$(url_for "$key")"
  if [[ -z "$url" ]]; then
    echo "warm[$key]: no URL (set CLOUDRUN_TTS_$(echo "$key" | tr 'a-z' 'A-Z')_URL in .env)" >&2
    return 1
  fi
  url="${url%/}"
  local token
  token="$(gcloud auth print-identity-token 2>/dev/null)"
  if [[ -z "$token" ]]; then
    echo "warm[$key]: no gcloud identity token (gcloud auth login needed)" >&2
    return 1
  fi
  # /readyz blocks until the model is loaded, then returns
  # {"status":"ready","warm_s":<float>}. Idempotent — instant on warm.
  # 600s ceiling (Chatterbox post-fix cold-start ~264s; F5 ~120s;
  # Higgs ~180s; IndicF5 ~150s — all well within 600s).
  echo "warm[$key]: GET ${url}/readyz (≤10-min cold-load timeout)…"
  local t0=$SECONDS
  if curl -sS -m 600 -H "Authorization: Bearer $token" "${url}/readyz" \
       -o "/tmp/warm-tts-${key}.json" \
       -w "warm[$key]: http_code=%{http_code} dt=%{time_total}s\n"; then
    if [[ -f "/tmp/warm-tts-${key}.json" ]]; then
      python3 -c "
import json
try:
    r = json.load(open('/tmp/warm-tts-${key}.json'))
    print(f'  warm[$key]: status={r.get(\"status\",\"?\")} warm_s={r.get(\"warm_s\")}')
except Exception as e:
    print(f'  warm[$key]: response parse failed — {e}')
" 2>/dev/null || true
    fi
  else
    echo "warm[$key]: FAILED in $((SECONDS - t0))s — render will pay cold-load on first /synth"
    return 1
  fi
}

# Run all targets in parallel (each takes 2-5 min on cold-load,
# would be silly to do sequentially).
pids=()
for key in "${TARGETS[@]}"; do
  warm_one "$key" &
  pids+=($!)
done
status=0
for p in "${pids[@]}"; do
  wait "$p" || status=1
done
exit $status
