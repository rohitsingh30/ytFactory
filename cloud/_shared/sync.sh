#!/usr/bin/env bash
# Sync the canonical cloud/_shared/otel_init.py into every cloud
# service directory.
#
# Why this exists: each cloud/<service>/ directory is its own
# Cloud Build context (deploy.sh runs ``gcloud builds submit .`` from
# inside the service dir), so files in ``cloud/_shared/`` aren't
# visible to the container build. The cleanest workaround is to keep
# a copy of ``otel_init.py`` next to each service's ``server.py`` and
# re-sync the copies whenever the canonical version changes.
#
# Run after editing cloud/_shared/otel_init.py:
#
#     bash cloud/_shared/sync.sh
#
# CI / pre-commit hook can call this same script with --check to
# fail when copies have drifted.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="${ROOT}/_shared/otel_init.py"

if [[ ! -f "${SOURCE}" ]]; then
    echo "Source file missing: ${SOURCE}" >&2
    exit 1
fi

CHECK_MODE=0
if [[ "${1:-}" == "--check" ]]; then
    CHECK_MODE=1
fi

# Every directory containing a server.py or entrypoint.py needs a copy.
SERVICES=()
for f in "${ROOT}"/*/server.py "${ROOT}"/*/entrypoint.py; do
    [[ -f "$f" ]] || continue
    SERVICES+=("$(dirname "$f")")
done

# De-dup (entrypoint.py + server.py in same dir).
SERVICES=($(printf '%s\n' "${SERVICES[@]}" | sort -u))

drift=0
for dir in "${SERVICES[@]}"; do
    target="${dir}/otel_init.py"
    if [[ ${CHECK_MODE} -eq 1 ]]; then
        if [[ ! -f "${target}" ]] || ! cmp -s "${SOURCE}" "${target}"; then
            echo "DRIFT: ${target}" >&2
            drift=$((drift + 1))
        fi
    else
        cp "${SOURCE}" "${target}"
        echo "synced: ${target#${ROOT}/}"
    fi
done

if [[ ${CHECK_MODE} -eq 1 ]]; then
    if [[ ${drift} -gt 0 ]]; then
        echo "" >&2
        echo "${drift} service(s) have drifted from cloud/_shared/otel_init.py." >&2
        echo "Run: bash cloud/_shared/sync.sh" >&2
        exit 1
    fi
    echo "all ${#SERVICES[@]} services in sync"
fi
