#!/usr/bin/env bash
# Warm one or more ytfactory-image-* Cloud Run services BEFORE a
# render window. Pays the ~5-7 min FLUX (or ~15-25 min Z-Image)
# cold-load up-front so the actual render's stage-3 hits a warm
# container at sub-second per image.
#
# Designed to run from:
#   - laptop manually right before a render: `./cloud/warm_image_services.sh`
#   - GitHub Actions cron / Cloud Scheduler 5-10 min before a queued render
#   - the web server when a user clicks "Generate" (fire-and-forget bg)
#
# Idempotent: hitting /readyz on an already-warm container returns
# instantly with `cold_loaded: false`.
#
# Usage:
#   ./warm_image_services.sh                 # warms FLUX only (default)
#   ./warm_image_services.sh flux            # explicit
#   ./warm_image_services.sh zimage          # warms Z-Image only
#   ./warm_image_services.sh flux zimage     # warms both in parallel

set -euo pipefail

PROJECT="${GCP_PROJECT:-ytfactory-prod}"
REGION="${GCP_REGION:-asia-southeast1}"

# bash 3.2 (macOS default) doesn't have associative arrays; use case.
url_for() {
  case "$1" in
    flux)   echo "https://ytfactory-image-flux2-klein-767262167641.${REGION}.run.app" ;;
    zimage) echo "https://ytfactory-image-z-image-turbo-767262167641.${REGION}.run.app" ;;
    *)      echo "" ;;
  esac
}

if [[ $# -eq 0 ]]; then
  TARGETS=(flux)
else
  TARGETS=("$@")
fi

warm_one() {
  local key="$1"
  local url
  url="$(url_for "$key")"
  if [[ -z "$url" ]]; then
    echo "warm: unknown key $key (valid: flux zimage)" >&2
    return 1
  fi
  local token
  token="$(gcloud auth print-identity-token 2>/dev/null)"
  if [[ -z "$token" ]]; then
    echo "warm[$key]: no gcloud identity token (gcloud auth login needed)" >&2
    return 1
  fi
  echo "warm[$key]: GET ${url}/readyz (15-min timeout for cold-load)…"
  local t0=$SECONDS
  if curl -sS -m 1500 -H "Authorization: Bearer $token" "${url}/readyz" \
       -o "/tmp/warm-${key}.json" -w "warm[$key]: http_code=%{http_code} dt=%{time_total}s\n"; then
    if [[ -f "/tmp/warm-${key}.json" ]]; then
      python3 -c "
import json, sys
try:
    r = json.load(open('/tmp/warm-${key}.json'))
    print(f'  warm[$key]: model={r.get(\"model_repo\",\"?\")} cold={r.get(\"cold_loaded\")} warm_s={r.get(\"warm_s\")} boot_s={r.get(\"boot_uptime_s\")}')
except Exception as e:
    print(f'  warm[$key]: response parse failed — {e}')
" 2>/dev/null || true
    fi
  else
    echo "warm[$key]: FAILED in $((SECONDS - t0))s — render will pay cold-load on first /generate"
    return 1
  fi
}

# Run all targets in parallel (each takes 5-25 min on cold-load,
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
