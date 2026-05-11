#!/usr/bin/env bash
# Append OTel deps to every cloud/<service>/requirements.txt.
#
# Idempotent — checks for an OTEL marker line before appending. Run
# this whenever cloud/_shared/otel_requirements.txt changes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPS_FILE="${ROOT}/_shared/otel_requirements.txt"

if [[ ! -f "${DEPS_FILE}" ]]; then
    echo "Source deps file missing: ${DEPS_FILE}" >&2
    exit 1
fi

MARKER="# ---- OTEL_DEPS (managed by cloud/_shared/append_otel_deps.sh)"
DEPS_BODY=$(cat "${DEPS_FILE}")

count=0
for req in "${ROOT}"/*/requirements.txt; do
    [[ -f "$req" ]] || continue
    rel="${req#${ROOT}/}"
    if grep -q "^${MARKER}" "${req}"; then
        # Re-sync block: drop everything after marker, re-append.
        # Use awk to print only lines BEFORE the marker.
        awk -v m="${MARKER}" '$0 == m { exit } { print }' "${req}" > "${req}.tmp"
        mv "${req}.tmp" "${req}"
    fi
    {
        printf '\n%s\n' "${MARKER}"
        echo "${DEPS_BODY}"
    } >> "${req}"
    echo "appended OTel deps to: ${rel}"
    count=$((count + 1))
done

echo ""
echo "Updated ${count} requirements.txt files."
