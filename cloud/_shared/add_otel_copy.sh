#!/usr/bin/env bash
# Insert ``COPY otel_init.py ./`` into every cloud/<service>/Dockerfile
# right after ``COPY server.py ./`` (or ``COPY entrypoint.py ./``).
#
# Idempotent — checks for the COPY line before adding.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COPY_LINE="COPY otel_init.py ./"

count=0
for df in "${ROOT}"/*/Dockerfile; do
    [[ -f "$df" ]] || continue
    rel="${df#${ROOT}/}"
    if grep -qF "${COPY_LINE}" "${df}"; then
        echo "skip (already has): ${rel}"
        continue
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
    echo "patched: ${rel}"
    count=$((count + 1))
done

echo ""
echo "Updated ${count} Dockerfiles."
