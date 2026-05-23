"""GCS-backed artifact storage. Single source of truth for all media.

URI scheme: gs://<bucket>/jobs/<job_id>/<relpath>

Helpers:
- upload(local_path, uri)        — push a file to GCS
- download(uri, local_path)      — pull a file from GCS
- upload_bytes(data, uri)        — push raw bytes
- download_bytes(uri)            — pull raw bytes
- signed_url(uri, ttl_s, method) — pre-signed URL for browser upload/download
- delete_prefix(uri_prefix)      — recursive delete (used by post-upload GC)
- list_prefix(uri_prefix)        — iterate URIs under a prefix

Bucket comes from YTFACTORY_BUCKET env (default: ytfactory-prod-artifacts).
"""
from __future__ import annotations

import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from pipeline.observability.event_helpers import safe_track as _track

DEFAULT_BUCKET = "ytfactory-prod-artifacts"


def bucket_name() -> str:
    return os.environ.get("YTFACTORY_BUCKET", DEFAULT_BUCKET)


def job_uri(job_id: str, relpath: str = "") -> str:
    base = f"gs://{bucket_name()}/jobs/{job_id}"
    return f"{base}/{relpath.lstrip('/')}" if relpath else base


def parse_uri(uri: str) -> tuple[str, str]:
    """`gs://bucket/path/to/file` → (`bucket`, `path/to/file`)."""
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {uri!r}")
    rest = uri[len("gs://"):]
    if "/" not in rest:
        return rest, ""
    bucket, key = rest.split("/", 1)
    return bucket, key


# Lazily build the singleton GCS client — keeps in-memory tests free of GCP auth.
_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        from google.cloud import storage  # noqa: PLC0415 — lazy
        _CLIENT = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod"))
    return _CLIENT


def _blob(uri: str):
    bucket, key = parse_uri(uri)
    return _client().bucket(bucket).blob(key)


def _track_storage(op: str, *, collection: str, doc_id: str, duration_ms: int) -> None:
    _track(
        f"control.storage.{op}",
        category="control",
        metadata={
            "collection": collection,
            "doc_id": doc_id,
            "duration_ms": duration_ms,
        },
    )


# ---------------------------------------------------------------------------
# Upload / download
# ---------------------------------------------------------------------------


def upload(local_path: str | Path, uri: str, *, content_type: str | None = None) -> str:
    """Push a local file to GCS. Returns the gs:// URI."""
    t0 = time.perf_counter()
    blob = _blob(uri)
    if content_type:
        blob.content_type = content_type
    blob.upload_from_filename(str(local_path))
    _track_storage("upload", collection="gcs", doc_id=uri, duration_ms=int((time.perf_counter() - t0) * 1000))
    return uri


def download(uri: str, local_path: str | Path) -> Path:
    """Pull a GCS object to disk. Returns the local Path."""
    t0 = time.perf_counter()
    p = Path(local_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _blob(uri).download_to_filename(str(p))
    _track_storage("download", collection="gcs", doc_id=uri, duration_ms=int((time.perf_counter() - t0) * 1000))
    return p


def upload_bytes(data: bytes, uri: str, *, content_type: str | None = None) -> str:
    t0 = time.perf_counter()
    blob = _blob(uri)
    if content_type:
        blob.content_type = content_type
    blob.upload_from_file(io.BytesIO(data), size=len(data), rewind=True)
    _track_storage("upload_bytes", collection="gcs", doc_id=uri, duration_ms=int((time.perf_counter() - t0) * 1000))
    return uri


def download_bytes(uri: str) -> bytes:
    t0 = time.perf_counter()
    data = _blob(uri).download_as_bytes()
    _track_storage("download_bytes", collection="gcs", doc_id=uri, duration_ms=int((time.perf_counter() - t0) * 1000))
    return data


# ---------------------------------------------------------------------------
# Signed URLs (browser-side upload/download)
# ---------------------------------------------------------------------------


def signed_url(uri: str, *, ttl_s: int = 600, method: str = "GET") -> str:
    """Browser-usable URL. ttl_s capped at 7 days by GCS.

    Two signing paths:

    1. **Local-key signing** — when ADC carries a service-account JSON
       (laptop dev / CI), the SDK signs the URL on-process. Cheap,
       no extra round-trips, no extra IAM grants.

    2. **IAM-API delegated signing** — when running on Cloud Run /
       GKE / GCE, ADC resolves to ``compute_engine.Credentials`` —
       a token blob, *no private key*. Local signing fails with
       ``AttributeError: you need a private key to sign credentials``.
       In that case we delegate to the IAM ``signBlob`` API by
       passing ``service_account_email`` + ``access_token`` to
       ``generate_signed_url``; the SDK then transparently calls
       ``iam.serviceAccounts.signBlob`` on the runtime SA.

       Required IAM grant (one-shot, on the SA itself):

           gcloud iam service-accounts add-iam-policy-binding \\
             <sa>@<project>.iam.gserviceaccount.com \\
             --member="serviceAccount:<sa>@<project>.iam.gserviceaccount.com" \\
             --role="roles/iam.serviceAccountTokenCreator" \\
             --project=<project>

       Without this, every preview.mp4 request 502s with the
       "you need a private key" traceback (caught + logged in
       ``control/routes/render_routes.py``). Pre-2026-05-11 the
       cloud render-detail page rendered a black <video> tile for
       every successful Cloud Run render because of this gap.
    """
    t0 = time.perf_counter()
    blob = _blob(uri)
    expiration = timedelta(seconds=ttl_s)

    try:
        out = blob.generate_signed_url(
            version="v4",
            expiration=expiration,
            method=method,
        )
        _track_storage("signed_url", collection="gcs", doc_id=uri, duration_ms=int((time.perf_counter() - t0) * 1000))
        return out
    except AttributeError as e:
        # The SDK raises AttributeError("you need a private key …")
        # for token-only credential types — only on this branch do we
        # spend the extra IAM round-trip. Any other AttributeError is
        # a real bug, so re-raise.
        if "private key" not in str(e):
            raise
        out = _signed_url_via_iam(blob, expiration=expiration, method=method)
        _track_storage("signed_url", collection="gcs", doc_id=uri, duration_ms=int((time.perf_counter() - t0) * 1000))
        return out


def _signed_url_via_iam(blob, *, expiration: timedelta, method: str) -> str:
    """Fallback path: delegate URL signing to the IAM signBlob API.

    Used when the runtime credentials carry only an OAuth token
    (Cloud Run / GCE / GKE) rather than a private key. The runtime
    SA must hold ``roles/iam.serviceAccountTokenCreator`` on itself
    so it can ``iam.signBlob`` on its own behalf — see :func:`signed_url`.
    """
    import google.auth  # noqa: PLC0415
    import google.auth.transport.requests  # noqa: PLC0415

    creds, _project = google.auth.default()
    sa_email = getattr(creds, "service_account_email", None)
    if not sa_email or sa_email == "default":
        # Compute Engine returns "default" as a placeholder until you
        # query the metadata server explicitly. Resolve it.
        try:
            from google.auth import compute_engine  # noqa: PLC0415
            request = google.auth.transport.requests.Request()
            sa_email = compute_engine._metadata.get_service_account_info(  # type: ignore[attr-defined]
                request, service_account="default",
            )["email"]
        except Exception:  # noqa: BLE001
            sa_email = None
    if not sa_email:
        raise RuntimeError(
            "signed_url: cannot determine service-account email for IAM signing. "
            "On Cloud Run set the runtime service account explicitly."
        )

    # The SDK needs a fresh access_token to call iam.signBlob.
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)

    return blob.generate_signed_url(
        version="v4",
        expiration=expiration,
        method=method,
        service_account_email=sa_email,
        access_token=creds.token,
    )


# ---------------------------------------------------------------------------
# Listing + delete (post-upload GC)
# ---------------------------------------------------------------------------


def list_prefix(uri_prefix: str) -> Iterator[str]:
    """Yield all gs:// URIs under the prefix."""
    t0 = time.perf_counter()
    bucket, key_prefix = parse_uri(uri_prefix)
    count = 0
    try:
        for blob in _client().list_blobs(bucket, prefix=key_prefix):
            count += 1
            yield f"gs://{bucket}/{blob.name}"
    finally:
        _track_storage(
            "list_prefix",
            collection="gcs",
            doc_id=uri_prefix,
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )


def delete_prefix(uri_prefix: str, *, dry_run: bool = False) -> list[str]:
    """Delete every object under the prefix. Returns the list of deleted URIs.

    Used by the post-upload GC to wipe `jobs/<id>/beats/`, `jobs/<id>/voice.wav`,
    etc. once a Short is on YouTube.
    """
    t0 = time.perf_counter()
    bucket_name_, key_prefix = parse_uri(uri_prefix)
    bucket = _client().bucket(bucket_name_)
    deleted: list[str] = []
    for blob in _client().list_blobs(bucket_name_, prefix=key_prefix):
        uri = f"gs://{bucket_name_}/{blob.name}"
        deleted.append(uri)
        if not dry_run:
            bucket.blob(blob.name).delete()
    _track_storage("delete_prefix", collection="gcs", doc_id=uri_prefix, duration_ms=int((time.perf_counter() - t0) * 1000))
    return deleted


def delete_one(uri: str) -> bool:
    """Delete a single object. Returns True if it existed."""
    t0 = time.perf_counter()
    blob = _blob(uri)
    if not blob.exists():
        _track_storage("delete_one", collection="gcs", doc_id=uri, duration_ms=int((time.perf_counter() - t0) * 1000))
        return False
    blob.delete()
    _track_storage("delete_one", collection="gcs", doc_id=uri, duration_ms=int((time.perf_counter() - t0) * 1000))
    return True


# ---------------------------------------------------------------------------
# Convenience: per-job paths
# ---------------------------------------------------------------------------


# Heavy artifacts that get GC'd after a successful YouTube upload.
HEAVY_ARTIFACT_RELPATHS: tuple[str, ...] = (
    "beats/",
    "voice.wav",
    "captions.srt",
    "prompts.json",
    "script.json",
    "cast.json",
    "footage/",
)

# Light artifacts kept for 7–30 days via lifecycle rules.
LIGHT_ARTIFACT_RELPATHS: tuple[str, ...] = (
    "short.mp4",
    "thumb.png",
    "proposal.json",
)


def gc_heavy_artifacts(job_id: str, *, dry_run: bool = False) -> list[str]:
    """Wipe heavy intermediates for a job after YT upload succeeds.

    Lifecycle rules handle short.mp4 + thumb.png expiry on a longer horizon.
    """
    deleted: list[str] = []
    for rel in HEAVY_ARTIFACT_RELPATHS:
        prefix = job_uri(job_id, rel)
        deleted.extend(delete_prefix(prefix, dry_run=dry_run))
    return deleted


# ---------------------------------------------------------------------------
# Upload-record mirror — keeps the cloud dashboard fresh without redeploys
# ---------------------------------------------------------------------------
#
# The Cloud Run dashboard reads `<channel>/uploads/**/*.json` to know which
# videos to display. Those files only exist on the laptop where uploads
# actually run, so without a mirror the cloud dashboard stays frozen at the
# image's build-time snapshot.
#
# Layout: gs://<bucket>/upload-records/<channel>/[<niche>/]<slug>.json
# (mirrors the local <channel>/uploads/[<niche>/]<slug>.json layout, minus
# the literal `/uploads/` segment which is implicit in the prefix).


UPLOAD_RECORDS_PREFIX = "upload-records"


def upload_record_uri(rel_key: str) -> str:
    """Build the gs:// URI for an upload record under upload-records/.

    ``rel_key`` is the on-disk path with `/uploads/` collapsed out — e.g.
    ``"historyrecapped/foo.json"`` or
    ``"mystoriesanimated/reddit_amitheasshole/bar.json"``.
    """
    return f"gs://{bucket_name()}/{UPLOAD_RECORDS_PREFIX}/{rel_key.lstrip('/')}"


def upload_record_rel_key(local_path: Path, project_root: Path) -> str:
    """Translate a local upload-record path to its GCS rel_key.

    Strips the project root and the literal `/uploads/` middle segment, so:
        <root>/historyrecapped/uploads/foo.json
            → "historyrecapped/foo.json"
        <root>/mystoriesanimated/uploads/reddit_amitheasshole/bar.json
            → "mystoriesanimated/reddit_amitheasshole/bar.json"
    """
    rel = Path(local_path).resolve().relative_to(Path(project_root).resolve())
    parts = list(rel.parts)
    # find the FIRST "uploads" segment (channel-level) and drop it
    if "uploads" in parts:
        idx = parts.index("uploads")
        parts = parts[:idx] + parts[idx + 1 :]
    return "/".join(parts)


def list_upload_records():
    """Return list of (channel, slug, record_dict) for every record in the bucket.

    ``channel`` is the top-level segment (e.g. "mystoriesanimated"); for
    nested layouts the niche segment is preserved in the GCS path but the
    dashboard groups by top-level channel anyway.

    Performance:
      - 60 s in-process TTL cache keyed by ``(bucket_name(), prefix)``.
        The dashboard polls this every ~10–30 s; before the cache the
        same enumeration ran on every poll, costing a sequential
        ``download_as_bytes`` per record (~50 ms each). With ~80 records
        a cold poll took 3–8 s. Cached polls now cost ~0 ms.
      - On a true cache miss the per-record JSON downloads run in
        parallel via a 16-worker pool, so cold load is bounded by
        ``ceil(N / 16) × 50 ms`` (~250 ms for 80 records) instead of
        ``N × 50 ms``.
      - Writers (``_mirror_record_to_gcs``) call
        :func:`bust_upload_records_cache` so a fresh upload appears on
        the dashboard within one poll cycle, not 60 s later.
    """
    return _list_upload_records_cached()


def bust_upload_records_cache() -> None:
    """Drop the cached upload-records list. Call after writing a new record."""
    with _UPLOAD_RECORDS_LOCK:
        _UPLOAD_RECORDS_CACHE.clear()


# Module-level cache for list_upload_records. Keyed by
# (bucket_name, prefix) so cross-test bucket monkey-patching doesn't
# cross-pollinate. Value is (cached_at_epoch, records_list).
_UPLOAD_RECORDS_TTL_S = 60.0
_UPLOAD_RECORDS_CACHE: dict[tuple[str, str], tuple[float, list[tuple[str, str, dict]]]] = {}
_UPLOAD_RECORDS_LOCK = threading.Lock()


def _list_upload_records_cached() -> list[tuple[str, str, dict]]:
    import json as _json

    bkt = bucket_name()
    prefix_uri = f"gs://{bkt}/{UPLOAD_RECORDS_PREFIX}/"
    cache_key = (bkt, prefix_uri)

    now = time.time()
    with _UPLOAD_RECORDS_LOCK:
        cached = _UPLOAD_RECORDS_CACHE.get(cache_key)
    if cached is not None and (now - cached[0]) < _UPLOAD_RECORDS_TTL_S:
        return cached[1]

    # Cold path: list all keys, then fan out the body downloads in
    # parallel. The list_prefix() call is one round-trip; previously each
    # download_bytes() was a serial round-trip on top.
    targets: list[tuple[str, str, str]] = []  # (channel, slug, uri)
    for uri in list_prefix(prefix_uri):
        if not uri.endswith(".json"):
            continue
        _, key = parse_uri(uri)
        rel = key[len(UPLOAD_RECORDS_PREFIX) + 1 :]
        if "/" not in rel:
            continue
        channel = rel.split("/", 1)[0]
        slug = rel.rsplit("/", 1)[1][: -len(".json")]
        targets.append((channel, slug, uri))

    out: list[tuple[str, str, dict]] = [None] * len(targets)  # type: ignore[list-item]

    def _fetch(idx_target: tuple[int, tuple[str, str, str]]):
        idx, (channel, slug, uri) = idx_target
        try:
            rec = _json.loads(download_bytes(uri).decode("utf-8"))
        except Exception:
            return idx, None
        return idx, (channel, slug, rec)

    if targets:
        # GCS handles fan-out fine; 16 workers keeps us well under any
        # quota and saturates the I/O wait without spawning a thread per
        # record (which would be wasteful for the warm path).
        with ThreadPoolExecutor(max_workers=16, thread_name_prefix="gcs-upload-records") as pool:
            for idx, result in pool.map(_fetch, list(enumerate(targets))):
                if result is not None:
                    out[idx] = result

    final = [r for r in out if r is not None]
    with _UPLOAD_RECORDS_LOCK:
        _UPLOAD_RECORDS_CACHE[cache_key] = (now, final)
    return final
