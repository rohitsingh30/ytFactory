"""Channel state CRUD against gs://ytfactory-prod-v2-state.

Skills (Claude Code on laptop) used to read/write narrations / cast /
shotlist / uploads JSON via the local filesystem, mirrored to GCS by
the ``com.ytfactory.state-sync`` launchd plist. Post-2026-05-09 cloud
cutover, the laptop carries zero data: skills hit these endpoints
instead, the website is the only writer to GCS, and state-sync is
retired.

URL shape:
    GET    /api/state/{channel}/{kind}/{slug}    → fetch JSON
    PUT    /api/state/{channel}/{kind}/{slug}    → write JSON (body)
    DELETE /api/state/{channel}/{kind}/{slug}    → remove
    GET    /api/state/{channel}/{kind}           → list slugs

`kind` is one of the per-channel subdir names (narrations, cast,
shotlist, uploads, raw, footage_plan, chapters, critiques). All are
JSON files in v1. `_holds.json` (no slug) is exposed as
``kind=_holds, slug=__channel`` shim so it fits the same shape.

Auth:
    On Cloud Run (K_SERVICE set) — IAM is trusted; Google Frontend
    already validated the OIDC token against run.invoker before the
    request even hit us.
    Off Cloud Run (laptop dev) — Bearer YTFACTORY_AGENT_TOKEN required.
"""
from __future__ import annotations

import json
import logging
import hmac
import os
import re
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

logger = logging.getLogger(__name__)

router = APIRouter()


# Whitelisted kinds — anything else is rejected. Keeps a typo'd PUT
# from creating a stray gs://.../<channel>/random_path/.
_ALLOWED_KINDS: frozenset[str] = frozenset({
    "narrations",
    "cast",
    "shotlist",
    "uploads",
    "raw",
    "footage_plan",
    "chapters",
    "critiques",
    "scripts",
    "prompts",   # per-beat scene prompts (animated-path skills)
    "pronounce", # TTS pronunciation sidecar (cosmosdecoded shorts)
    "_holds",
})

# Channel + slug: alnum, dash, underscore, dot. Reject anything that
# could escape the prefix (../, leading /, etc).
_SAFE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")

_BUCKET_ENV = "YTFACTORY_STATE_BUCKET"
_DEFAULT_BUCKET = "ytfactory-prod-v2-state"


# ---------------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------------


def _require_auth(authorization: str | None) -> None:
    if os.environ.get("K_SERVICE"):
        return
    expected = os.environ.get("YTFACTORY_AGENT_TOKEN")
    if not expected:
        raise HTTPException(503, "state API unauthenticated: YTFACTORY_AGENT_TOKEN not set")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    # Audit S1.16 — constant-time compare so an attacker can't byte-by-byte
    # discover the token via response-time side channel.
    if not hmac.compare_digest(token, expected):
        raise HTTPException(403, "invalid token")


def _validate(channel: str, kind: str, slug: str | None = None) -> None:
    if not _SAFE_RE.match(channel):
        raise HTTPException(400, f"invalid channel: {channel!r}")
    if kind not in _ALLOWED_KINDS:
        raise HTTPException(400, f"invalid kind: {kind!r} (allowed: {sorted(_ALLOWED_KINDS)})")
    if slug is not None and not _SAFE_RE.match(slug):
        raise HTTPException(400, f"invalid slug: {slug!r}")


def _bucket_name() -> str:
    return os.environ.get(_BUCKET_ENV, _DEFAULT_BUCKET)


def _gcs_client():
    from google.cloud import storage  # noqa: PLC0415
    return storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2"))


def _blob_path(channel: str, kind: str, slug: str) -> str:
    return f"{channel}/{kind}/{slug}.json"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/state/{channel}/{kind}/{slug}")
def state_get(
    channel: str,
    kind: str,
    slug: str,
    authorization: str | None = Header(None),
) -> JSONResponse:
    _require_auth(authorization)
    _validate(channel, kind, slug)

    cli = _gcs_client()
    blob = cli.bucket(_bucket_name()).blob(_blob_path(channel, kind, slug))
    if not blob.exists():
        raise HTTPException(404, f"state not found: {channel}/{kind}/{slug}")

    body = blob.download_as_bytes()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        # Should not happen — we only PUT JSON. If it does, something
        # external wrote garbage; surface it cleanly so the caller can
        # decide to re-author.
        raise HTTPException(
            502, f"state at {channel}/{kind}/{slug} is not valid JSON: {e}"
        )
    return JSONResponse(data)


@router.put("/api/state/{channel}/{kind}/{slug}")
def state_put(
    channel: str,
    kind: str,
    slug: str,
    body: Any = Body(...),
    authorization: str | None = Header(None),
) -> JSONResponse:
    _require_auth(authorization)
    _validate(channel, kind, slug)

    # Schema-first validation: narration JSONs must conform to the
    # universal NicheVideo envelope. Every producer (skill, future
    # cloud agent, web form, batch importer) writes the same shape;
    # niches differ in values, not structure.
    #
    # Other kinds (cast, shotlist, raw, prompts, ...) are still
    # free-form for v1 — they move under the contract as their own
    # sub-schemas land. The narration kind is the spine, so it's
    # gated first.
    if kind == "narrations":
        from pipeline.schemas.niche_schema import validate_payload  # noqa: PLC0415
        from pydantic import ValidationError  # noqa: PLC0415
        try:
            parsed = validate_payload(body if isinstance(body, dict) else {})
        except ValidationError as ve:
            raise HTTPException(
                422,
                {"error": "narration payload does not conform to NicheVideo schema",
                 "errors": ve.errors()},
            )
        # Cross-check: payload's channel + slug must match the URL path —
        # otherwise a caller could PUT a payload claiming channel=X to
        # the URL of channel=Y.
        if parsed.channel != channel:
            raise HTTPException(
                422,
                f"payload channel={parsed.channel!r} mismatches URL channel={channel!r}",
            )
        if parsed.slug != slug:
            raise HTTPException(
                422,
                f"payload slug={parsed.slug!r} mismatches URL slug={slug!r}",
            )

    payload = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    cli = _gcs_client()
    blob = cli.bucket(_bucket_name()).blob(_blob_path(channel, kind, slug))
    blob.upload_from_string(payload, content_type="application/json")
    logger.info(
        "state PUT channel=%s kind=%s slug=%s bytes=%d",
        channel, kind, slug, len(payload),
    )
    return JSONResponse({"ok": True, "uri": f"gs://{_bucket_name()}/{_blob_path(channel, kind, slug)}"})


@router.delete("/api/state/{channel}/{kind}/{slug}")
def state_delete(
    channel: str,
    kind: str,
    slug: str,
    authorization: str | None = Header(None),
) -> JSONResponse:
    _require_auth(authorization)
    _validate(channel, kind, slug)

    cli = _gcs_client()
    blob = cli.bucket(_bucket_name()).blob(_blob_path(channel, kind, slug))
    if not blob.exists():
        raise HTTPException(404, f"state not found: {channel}/{kind}/{slug}")
    blob.delete()
    logger.info("state DELETE channel=%s kind=%s slug=%s", channel, kind, slug)
    return JSONResponse({"ok": True})


@router.get("/api/state/{channel}/{kind}")
def state_list(
    channel: str,
    kind: str,
    authorization: str | None = Header(None),
) -> JSONResponse:
    _require_auth(authorization)
    _validate(channel, kind)

    cli = _gcs_client()
    prefix = f"{channel}/{kind}/"
    slugs: list[str] = []
    for blob in cli.list_blobs(_bucket_name(), prefix=prefix):
        # Skip nested subdirs (e.g. mystoriesanimated/<niche>/narrations/) —
        # the v1 list endpoint returns only flat-layout slugs. Nested
        # niche listing is a follow-up.
        rel = blob.name[len(prefix):]
        if "/" in rel or not rel.endswith(".json"):
            continue
        slugs.append(rel[: -len(".json")])
    return JSONResponse({"channel": channel, "kind": kind, "slugs": slugs})


@router.get("/api/state/health")
def state_health() -> dict:
    """No-auth health check — confirms the routes are mounted and the
    bucket env var is set. Doesn't actually touch GCS."""
    return {
        "ok": True,
        "bucket": _bucket_name(),
        "allowed_kinds": sorted(_ALLOWED_KINDS),
    }
