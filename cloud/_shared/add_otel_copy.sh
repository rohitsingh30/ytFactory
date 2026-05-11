#!/usr/bin/env bash
# Insert the right COPY otel_init.py line into every cloud/<service>/Dockerfile.
#
# 2026-05-11 — context-aware. Two valid build-context conventions in
# this repo:
#
#   1. **per-service-dir** (12 services): `cd cloud/<svc>/ && gcloud
#      builds submit .`. Top-of-Dockerfile clue: `COPY server.py ./` /
#      `COPY requirements.txt .`. OTel COPY: `COPY otel_init.py ./`.
#
#   2. **repo-root** (editing-agent, render-worker-v2): `gcloud builds
#      submit . --config=cloud/<svc>/cloudbuild.yaml` (from repo root).
#      Top-of-Dockerfile clue: any `COPY pipeline/...` /
#      `COPY cloud/<svc>/...` / `COPY scripts/...` line.
#      OTel COPY: `COPY cloud/<svc>/otel_init.py /workspace/otel_init.py`.
#
# This script detects each Dockerfile's context and emits the matching
# COPY line. Idempotent — checks for the COPY (either form) before
# adding.
#
# See docs/cloud_service_dep_playbook.md §"Auto-patch scripts must
# detect each Dockerfile's build context".
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

count=0
for df in "${ROOT}"/*/Dockerfile; do
    [[ -f "$df" ]] || continue
    rel="${df#${ROOT}/}"
    svc="$(basename "$(dirname "$df")")"

    # Skip if either form of the COPY is already present.
    if grep -qE '^COPY (cloud/[^/]+/)?otel_init\.py' "$df"; then
        echo "skip (already has): ${rel}"
        continue
    fi

    # Detect build context by scanning for repo-root-relative COPYs.
    if grep -qE '^COPY (pipeline|scripts|control|cloud)/' "$df"; then
        # Repo-root context: emit absolute repo-root path.
        COPY_LINE="COPY cloud/${svc}/otel_init.py /workspace/otel_init.py"
        ctx="repo-root"
    else
        # Per-service-dir context: emit local relative path.
        COPY_LINE="COPY otel_init.py ./"
        ctx="per-service-dir"
    fi

    # Insert after first match of "COPY server.py" or "COPY entrypoint.py".
    # If neither, append at end.
    if grep -qE "^COPY (server|entrypoint)\.py" "${df}"; then
        awk -v line="${COPY_LINE}" '
            /^COPY (server|entrypoint)\.py/ && !done { print; print line; done=1; next }
            { print }
            END { if (!done) print line }
        ' "${df}" > "${df}.tmp"
    else
        cat "${df}" > "${df}.tmp"
        printf '\n%s\n' "${COPY_LINE}" >> "${df}.tmp"
    fi
    mv "${df}.tmp" "${df}"
    echo "patched (${ctx}): ${rel}"
    count=$((count + 1))
done

echo ""
echo "Updated ${count} Dockerfiles."
