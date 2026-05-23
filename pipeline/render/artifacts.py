"""Live artifact emission — uploads each per-render intermediate to GCS
the moment it's produced AND updates the Firestore job doc so the
dashboard's poll loop can show inline previews as artifacts arrive.

Why this exists
---------------

Pre-2026-05-12 the dashboard had nothing to show until the FINAL mp4
landed. A 30-min long-form render meant 25-30 minutes of "compose:
real-mode" with no observable progress beyond a generic stage pill.

Now every render stage (rewrite, narrate, align, visualize, compose,
upload) calls :func:`emit_artifact` the moment it produces its output
file. The Firestore job doc grows an ``artifacts`` subdocument:

::

    artifacts: {
      script:    {status, uri, version, ...extras}
      narration: {status, uri, version, duration_s, ...}
      beats:     {status, uri, version, n_beats}
      images:    [{i, status, uri, ...}, ...]   # append-only
      thumb:     {status, uri, version}
      video:     {status, uri, version}
    }

Statuses follow {pending, ready, failed}. The dashboard's render-detail
page polls the same 800 ms loop it already uses and renders inline
previews for every ``ready`` artifact (script as collapsible text,
narration as ``<audio>``, images as a gallery, video as ``<video>``).

Failure semantics
-----------------

This module NEVER raises. If a GCS upload fails or Firestore is
unreachable, the call logs a warning and returns ``None``. The render
pipeline must not break because telemetry-style uploads failed —
the canonical mp4 still ships at the end via the worker's existing
``_upload_mp4_to_gcs`` path.

Versioning
----------

Each call increments ``version`` for that artifact kind. The
dashboard reads ``artifacts.<kind>.version`` and refuses to display
a cached preview from an earlier version, which prevents a stale
narration.wav from a previous render attempt sneaking in after a
re-render.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GCS + Firestore client cache
# ---------------------------------------------------------------------------


_storage_client = None
_firestore_client = None


def _get_storage_client():
    """Lazy import + cache the GCS client.

    Imports inside the function so this module can be imported on the
    laptop in environments without google-cloud-storage installed
    (where artifact emission is a no-op anyway)."""
    global _storage_client
    if _storage_client is None:
        try:
            from google.cloud import storage  # noqa: PLC0415
            _storage_client = storage.Client(
                project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("google-cloud-storage unavailable: %s", exc)
            return None
    return _storage_client


def _get_firestore_client():
    global _firestore_client
    if _firestore_client is None:
        try:
            from google.cloud import firestore  # noqa: PLC0415
            _firestore_client = firestore.Client(
                project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("google-cloud-firestore unavailable: %s", exc)
            return None
    return _firestore_client


def _bucket_name() -> str | None:
    """Artifacts bucket — same one the cloud worker uploads the final
    mp4 to. ``None`` when not configured (laptop without the env)."""
    return os.environ.get("YTFACTORY_BUCKET")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Canonical artifact kinds. Anything outside this set is accepted but
# logged (so a typo doesn't silently disappear).
KNOWN_KINDS: set[str] = {
    "script",
    "narration",
    "beats",
    "images",
    "thumb",
    "video",
    "envelope",
    "shotlist",
    "cast",
    "panels",
    "preview",
    # Telemetry-era additions (2026-05-24). One canonical artifact per
    # render stage so a post-mortem from gs://.../jobs/<id>/ alone can
    # reconstruct the full input/output tree without needing to grep
    # Cloud Logs. Each kind lands at jobs/<id>/<kind>/<filename>.
    "prompts_raw",       # what the LLM author returned, pre-refiner
    "prompts_refined",   # refiner output incl. per-beat status
    "refiner_io",        # exact prompt + raw response that refiner saw
    "image_meta",        # per-image: prompt, sha, seed, dims, latency
    "tts_chunks",        # per-chunk text + provider + wav_seconds + cache
    "asr_alignment",     # full whisper alignment + anchor matches
    "timeline",          # beat → time-range mapping
    "music",             # which track, why, ducking decisions
    "compose",           # ffmpeg args + output stats
    "render_plan",       # engine pick + resolved plugin slots
    "decision_log",      # ordered list of non-trivial fallback decisions
    "events_log",        # full chronological event stream (jsonl)
    "source",            # raw fetched source body (pre-rewrite)
}


def emit_artifact(
    job_id: str,
    kind: str,
    local_path: Path | str,
    *,
    index: int | None = None,
    extras: dict[str, Any] | None = None,
    content_type: str | None = None,
) -> str | None:
    """Upload ``local_path`` to GCS and update the Firestore job doc.

    Args:
        job_id: The Firestore job id this artifact belongs to.
        kind: One of :data:`KNOWN_KINDS`. Determines the GCS path
            and the Firestore field it updates.
        local_path: Local file to upload.
        index: For list-typed kinds (currently ``images``), the
            position in the list. Required when ``kind`` is list-typed,
            ignored otherwise.
        extras: Per-artifact metadata merged into the Firestore record
            (e.g. ``{duration_s: 1146.9}`` for narration,
            ``{n_beats: 24}`` for beats). Stays under the artifact's
            entry; doesn't pollute the top-level job doc.
        content_type: Override the auto-detected MIME type. Most kinds
            have a sensible default (json → application/json,
            wav → audio/wav, png → image/png, mp4 → video/mp4); pass
            this only when the local file extension is missing.

    Returns:
        The ``gs://…`` URI of the uploaded blob, or ``None`` if the
        upload was skipped (no bucket configured, GCS client missing,
        local file absent, etc).

    NEVER raises — all failures log + return None. Render correctness
    must not depend on artifact emission.
    """
    if kind not in KNOWN_KINDS:
        _logger.warning("emit_artifact: unknown kind %r — proceeding", kind)

    local = Path(local_path)
    if not local.exists():
        _logger.warning(
            "emit_artifact: local path %s missing for kind=%s job=%s; skipping",
            local, kind, job_id,
        )
        return None

    bucket_name = _bucket_name()
    if not bucket_name:
        _logger.debug(
            "emit_artifact: YTFACTORY_BUCKET unset — skipping (kind=%s)", kind,
        )
        return None

    storage_client = _get_storage_client()
    if storage_client is None:
        return None

    blob_path = _gcs_blob_path(job_id, kind, local.name, index=index)
    try:
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.content_type = content_type or _guess_content_type(local)
        blob.upload_from_filename(str(local))
    except Exception as exc:  # noqa: BLE001
        # Gap #7: GCS upload failure is non-blocking (render still
        # produced an mp4) but must surface as ERROR so IAM/bucket
        # regressions don't sit silently in WARNING streams.
        _logger.error(
            "emit_artifact: GCS upload failed for kind=%s job=%s "
            "path=%s: %s [%s]",
            kind, job_id, local, exc, type(exc).__name__,
        )
        return None

    uri = f"gs://{bucket_name}/{blob_path}"
    _logger.info(
        "emit_artifact: uploaded kind=%s job=%s → %s (%d bytes)",
        kind, job_id, uri, local.stat().st_size,
    )

    _update_firestore_artifact(
        job_id=job_id,
        kind=kind,
        uri=uri,
        index=index,
        extras=extras or {},
    )

    return uri


def emit_artifact_failed(
    job_id: str,
    kind: str,
    *,
    index: int | None = None,
    error: str = "",
) -> None:
    """Mark an artifact as failed in Firestore without an upload.

    Used by stages that produced (or attempted to produce) an artifact
    that we can't ship — e.g. compose succeeded but the thumbnail
    generation crashed. Surfaces "thumb: failed" on the dashboard so
    the user knows that specific artifact is broken without thinking
    the whole render is.
    """
    _update_firestore_artifact(
        job_id=job_id,
        kind=kind,
        uri=None,
        index=index,
        extras={"error": (error or "")[:1000]},
        status="failed",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _gcs_blob_path(
    job_id: str,
    kind: str,
    filename: str,
    *,
    index: int | None,
) -> str:
    """Layout under the bucket — ``jobs/<job_id>/<kind>[/<index>]/<filename>``.

    Examples:
        ``jobs/abc123/script/script.json``
        ``jobs/abc123/narration/narration.wav``
        ``jobs/abc123/images/00007/beat-7.png``  (index-keyed)
        ``jobs/abc123/video/short.mp4``
    """
    if index is not None:
        return f"jobs/{job_id}/{kind}/{index:05d}/{filename}"
    return f"jobs/{job_id}/{kind}/{filename}"


_CONTENT_TYPE_BY_SUFFIX: dict[str, str] = {
    ".json": "application/json",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".txt": "text/plain",
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
}


def _guess_content_type(path: Path) -> str:
    return _CONTENT_TYPE_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


# Kinds that store a list of entries on the job doc (e.g. images[i]).
_LIST_KINDS: set[str] = {"images", "panels"}


def _update_firestore_artifact(
    *,
    job_id: str,
    kind: str,
    uri: str | None,
    index: int | None,
    extras: dict[str, Any],
    status: str = "ready",
) -> None:
    """Merge the artifact entry into the job doc's ``artifacts`` map."""
    fs = _get_firestore_client()
    if fs is None:
        return

    entry: dict[str, Any] = {
        "status": status,
        "uri": uri,
    }
    if extras:
        entry.update(extras)

    try:
        doc_ref = fs.collection("jobs").document(job_id)
        if kind in _LIST_KINDS and index is not None:
            # Append-or-replace the index-th entry in artifacts.<kind>.
            # Firestore can't update a single index in a list atomically,
            # so we read-modify-write inside a transaction. Cheap (one
            # job doc + one round-trip per image) and avoids races
            # across the per-image worker callbacks.
            from google.cloud.firestore import transactional  # noqa: PLC0415

            @transactional
            def _txn(txn, doc_ref=doc_ref, kind=kind, index=index, entry=entry):
                snap = doc_ref.get(transaction=txn)
                doc = snap.to_dict() or {}
                artifacts = dict(doc.get("artifacts") or {})
                items = list(artifacts.get(kind) or [])
                # Pad with placeholders so list[index] is reachable.
                while len(items) <= index:
                    items.append({"status": "pending", "uri": None})
                # Bump per-item version AND assign the index.
                items[index] = {
                    **entry,
                    "i": index,
                    "version": int((items[index] or {}).get("version") or 0) + 1,
                }
                artifacts[kind] = items
                txn.set(
                    doc_ref,
                    {"artifacts": artifacts},
                    merge=True,
                )

            _txn(fs.transaction())
        else:
            # Scalar artifact — direct merge with version bump.
            snap = doc_ref.get()
            doc = snap.to_dict() or {} if snap.exists else {}
            current = (doc.get("artifacts") or {}).get(kind) or {}
            entry["version"] = int(current.get("version") or 0) + 1
            doc_ref.set(
                {"artifacts": {kind: entry}},
                merge=True,
            )
    except Exception as exc:  # noqa: BLE001
        # Gap #7: Firestore update failure is non-blocking but escalated
        # to ERROR so dashboard-write regressions surface.
        _logger.error(
            "emit_artifact: Firestore update failed for kind=%s "
            "job=%s: %s [%s]",
            kind, job_id, exc, type(exc).__name__,
        )


__all__ = [
    "emit_artifact",
    "emit_artifact_failed",
    "emit_artifact_json",
    "KNOWN_KINDS",
]


def emit_artifact_json(
    job_id: str,
    kind: str,
    data: Any,
    *,
    filename: str | None = None,
    index: int | None = None,
    extras: dict[str, Any] | None = None,
) -> str | None:
    """Convenience: dump ``data`` to JSON in a temp file and emit_artifact it.

    Saves callers from writing the same five-line "dump to tmp +
    emit_artifact + cleanup" pattern at every stage boundary. Used by
    the stage_envelope decorator and by every new telemetry artifact
    added in the 2026-05-24 observability pass.

    Filename defaults to ``<kind>.json``.

    Never raises — JSON serialisation failures fall through to the
    standard emit_artifact warning + return None path.
    """
    import json as _json  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    filename = filename or f"{kind}.json"
    try:
        # Custom default coerces dataclasses / pathlib / set / bytes
        # into something JSON can render without exploding the render
        # on a single non-serialisable field.
        def _fallback(o: Any) -> Any:
            try:
                if isinstance(o, (set, frozenset)):
                    return sorted(o)
                if isinstance(o, (bytes, bytearray)):
                    return o.decode("utf-8", errors="replace")
                if hasattr(o, "__dict__"):
                    return {k: v for k, v in vars(o).items() if not k.startswith("_")}
            except Exception:  # noqa: BLE001
                pass
            return repr(o)

        body = _json.dumps(data, indent=2, default=_fallback, sort_keys=False)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "emit_artifact_json: JSON encode failed kind=%s job=%s: %s",
            kind, job_id, exc,
        )
        return None

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8",
    ) as tmp:
        tmp.write(body)
        tmp_path = Path(tmp.name)
    try:
        # Rename to the requested filename so the GCS object has the
        # right name (emit_artifact uses local.name as the blob filename).
        target = tmp_path.with_name(filename)
        try:
            tmp_path.replace(target)
        except OSError:
            # Cross-device or permission issue — fall back to using the
            # tmp path as-is; the blob will have a tmp-style name.
            target = tmp_path
        return emit_artifact(
            job_id=job_id,
            kind=kind,
            local_path=target,
            index=index,
            extras=extras,
            content_type="application/json",
        )
    finally:
        # Best-effort cleanup; cloud workdirs are ephemeral so leaks
        # don't accumulate.
        for p in (tmp_path, target if 'target' in locals() else None):
            try:
                if p and p.exists():
                    p.unlink()
            except Exception:  # noqa: BLE001
                pass
