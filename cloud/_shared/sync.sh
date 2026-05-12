#!/usr/bin/env bash
# Sync the canonical cloud/_shared/*.py helpers into every cloud
# service directory.
#
# Why this exists: each cloud/<service>/ directory is its own
# Cloud Build context (deploy.sh runs ``gcloud builds submit .`` from
# inside the service dir), so files in ``cloud/_shared/`` aren't
# visible to the container build. The cleanest workaround is to keep
# a copy of each helper next to each service's ``server.py`` and
# re-sync the copies whenever the canonical version changes.
#
# Synced files (in lockstep):
#   - otel_init.py                    OTel boot helper
#   - cloud_run_json_exporter.py      Cloud-Run-shaped JSON log exporter
#                                     (imported by otel_init.py)
#
# Run after editing any helper in cloud/_shared/:
#
#     bash cloud/_shared/sync.sh
#
# CI / pre-commit hook can call this same script with --check to
# fail when copies have drifted.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELPERS=(
    "${ROOT}/_shared/otel_init.py"
    "${ROOT}/_shared/cloud_run_json_exporter.py"
)

for src in "${HELPERS[@]}"; do
    if [[ ! -f "${src}" ]]; then
        echo "Source file missing: ${src}" >&2
        exit 1
    fi
done

CHECK_MODE=0
if [[ "${1:-}" == "--check" ]]; then
    CHECK_MODE=1
fi

# Every directory containing a server.py or entrypoint.py needs a copy
# — AND any directory that already ships an otel_init.py (catches
# slim-wrapper images like cloud/web-server/ whose actual app code lives
# elsewhere in the repo and is COPY'd in at build time). Exclude
# ``_shared`` itself: it is the canonical source dir, not a service.
SERVICES=()
for f in "${ROOT}"/*/server.py "${ROOT}"/*/entrypoint.py "${ROOT}"/*/otel_init.py; do
    [[ -f "$f" ]] || continue
    d="$(dirname "$f")"
    [[ "$(basename "$d")" == "_shared" ]] && continue
    SERVICES+=("$d")
done

# De-dup (entrypoint.py + server.py + otel_init.py in same dir).
SERVICES=($(printf '%s\n' "${SERVICES[@]}" | sort -u))

drift=0
for dir in "${SERVICES[@]}"; do
    for src in "${HELPERS[@]}"; do
        target="${dir}/$(basename "${src}")"
        if [[ ${CHECK_MODE} -eq 1 ]]; then
            if [[ ! -f "${target}" ]] || ! cmp -s "${src}" "${target}"; then
                echo "DRIFT: ${target}" >&2
                drift=$((drift + 1))
            fi
        else
            cp "${src}" "${target}"
            echo "synced: ${target#${ROOT}/}"
        fi
    done
done

if [[ ${CHECK_MODE} -eq 1 ]]; then
    if [[ ${drift} -gt 0 ]]; then
        echo "" >&2
        echo "${drift} file(s) have drifted from cloud/_shared/." >&2
        echo "Run: bash cloud/_shared/sync.sh" >&2
        exit 1
    fi
    echo "all $(( ${#SERVICES[@]} * ${#HELPERS[@]} )) files in sync"
fi
