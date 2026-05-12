#!/usr/bin/env bash
# Insert the right COPY <helper>.py lines into every Python
# cloud/<service>/Dockerfile.
#
# 2026-05-12 — multi-helper. The OTel boot now imports a sibling
# `cloud_run_json_exporter.py` for Cloud-Run-shaped structured log
# output. Both files must live next to server.py / entrypoint.py at
# build time, so this script patches a COPY for each. New helpers go
# into the OTEL_HELPERS array below — Dockerfile patching is fully
# data-driven from there.
#
# 2026-05-11 — context-aware. Two valid build-context conventions in
# this repo:
#
#   1. **per-service-dir** (12 services): `cd cloud/<svc>/ && gcloud
#      builds submit .`. Top-of-Dockerfile clue: `COPY server.py ./` /
#      `COPY requirements.txt .`. OTel COPY: `COPY <helper> ./`.
#
#   2. **repo-root** (editing-agent, render-worker-v2): `gcloud builds
#      submit . --config=cloud/<svc>/cloudbuild.yaml` (from repo root).
#      Top-of-Dockerfile clue: any `COPY pipeline/...` /
#      `COPY cloud/<svc>/...` / `COPY scripts/...` / `COPY <svc>/...`
#      line. OTel COPY:
#      `COPY cloud/<svc>/<helper> /workspace/<helper>`.
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

# Helpers that must be COPY'd into every Python Cloud Run image. Keep
# this in lockstep with cloud/_shared/sync.sh::HELPERS.
OTEL_HELPERS=(
    "otel_init.py"
    "cloud_run_json_exporter.py"
)

count=0
for df in "${ROOT}"/*/Dockerfile; do
    [[ -f "$df" ]] || continue
    rel="${df#${ROOT}/}"
    svc_dir="$(dirname "$df")"
    svc="$(basename "${svc_dir}")"

    # Scope filter: only Python services (server.py / entrypoint.py /
    # an existing otel_init.py) use OTel today. Skip everything else
    # (web-next is Node.js; weights-staging is a one-shot init
    # container; etc.). The ``otel_init.py`` clause catches slim-wrapper
    # images like cloud/web-server/ whose actual app code lives in
    # web/server.py and is COPY'd in via repo-root context. This MUST
    # mirror the filter in cloud/_shared/sync.sh — if they ever
    # disagree, deploys break with `COPY otel_init.py: file not
    # found`.
    if [[ ! -f "${svc_dir}/server.py" \
          && ! -f "${svc_dir}/entrypoint.py" \
          && ! -f "${svc_dir}/otel_init.py" ]]; then
        echo "skip (non-Python service): ${rel}"
        continue
    fi

    # Detect build context once per Dockerfile. List of root prefixes
    # intentionally includes BOTH well-known source roots
    # (pipeline/scripts/control/cloud) AND any COPY <svc>/... line —
    # services like web-next use the latter pattern when they ship
    # from the repo root via cloudbuild.yaml.
    if grep -qE "^COPY (pipeline|scripts|control|cloud|${svc})/" "$df"; then
        ctx="repo-root"
    else
        ctx="per-service-dir"
    fi

    patched_in_file=0
    for helper in "${OTEL_HELPERS[@]}"; do
        helper_re="${helper//./\\.}"
        # Skip if either form of the COPY is already present.
        if grep -qE "^COPY (cloud/[^/]+/)?${helper_re}" "$df"; then
            continue
        fi

        if [[ "${ctx}" == "repo-root" ]]; then
            COPY_LINE="COPY cloud/${svc}/${helper} /workspace/${helper}"
        else
            COPY_LINE="COPY ${helper} ./"
        fi

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
        patched_in_file=$((patched_in_file + 1))
        echo "patched (${ctx}, ${helper}): ${rel}"
    done

    if [[ ${patched_in_file} -eq 0 ]]; then
        echo "skip (already has all helpers): ${rel}"
    else
        count=$((count + 1))
    fi
done

echo ""
echo "Updated ${count} Dockerfiles."
