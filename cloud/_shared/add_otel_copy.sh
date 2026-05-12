#!/usr/bin/env bash
# Insert the right COPY otel_init.py line into every Python
# cloud/<service>/Dockerfile.
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
#      `COPY cloud/<svc>/...` / `COPY scripts/...` / `COPY <svc>/...`
#      line. OTel COPY:
#      `COPY cloud/<svc>/otel_init.py /workspace/otel_init.py`.
#
# This script detects each Dockerfile's context and emits the matching
# COPY line. Idempotent — checks for the COPY (either form) before
# adding.
#
# 2026-05-12 — SCOPE FIX. This script previously patched every
# cloud/<svc>/Dockerfile, including non-Python services like
# `web-next` (Node.js, runs `next start`). That broke the web-next
# Cloud Build because:
#   * `cloud/_shared/sync.sh` (correctly) only copies otel_init.py
#     into dirs with server.py/entrypoint.py — i.e. Python services.
#   * This script (then) patched web-next/Dockerfile too → COPY
#     resolved against a non-existent file at build time.
# Both scripts now use the SAME filter: a service is OTel-eligible
# iff it has `server.py` or `entrypoint.py` in its directory. See
# `docs/cloud_service_dep_playbook.md` §"Auto-patch scope: only
# Python OTel-using services".
#
# See docs/cloud_service_dep_playbook.md §"Auto-patch scripts must
# detect each Dockerfile's build context".
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

count=0
for df in "${ROOT}"/*/Dockerfile; do
    [[ -f "$df" ]] || continue
    rel="${df#${ROOT}/}"
    svc_dir="$(dirname "$df")"
    svc="$(basename "${svc_dir}")"

    # Scope filter: only Python services (server.py / entrypoint.py)
    # use OTel today. Skip everything else (web-next is Node.js;
    # weights-staging is a one-shot init container; etc.). This MUST
    # mirror the filter in cloud/_shared/sync.sh — if they ever
    # disagree, deploys break with `COPY otel_init.py: file not
    # found`.
    if [[ ! -f "${svc_dir}/server.py" && ! -f "${svc_dir}/entrypoint.py" ]]; then
        echo "skip (non-Python service): ${rel}"
        continue
    fi

    # Skip if either form of the COPY is already present.
    if grep -qE '^COPY (cloud/[^/]+/)?otel_init\.py' "$df"; then
        echo "skip (already has): ${rel}"
        continue
    fi

    # Detect build context by scanning for repo-root-relative COPYs.
    # The list of root prefixes intentionally includes BOTH well-known
    # source roots (pipeline/scripts/control/cloud) AND any
    # COPY <svc>/... line — services like web-next use the latter
    # pattern when they ship from the repo root via cloudbuild.yaml.
    if grep -qE "^COPY (pipeline|scripts|control|cloud|${svc})/" "$df"; then
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
