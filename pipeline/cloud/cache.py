"""Per-call artifact persistence to GCS — B2 in 2026-05-23 docket.

Backstory: pre-2026-05-23 the render-worker wrote panel PNGs, TTS chunk
WAVs, alignment JSONs etc. into ``/tmp/render/<job_id>/`` (Cloud Run
container scratch). When a worker SIGKILLed mid-render (job
``a0aac53e`` hit the 60-min ``--task-timeout`` and was killed at
compose seg 17/60), the container died with the scratch volume and the
₹15-20 of already-paid image+TTS cost evaporated with it. The retry
had to re-pay the whole bill.

Fix: copy every per-call artifact to GCS as it's written. On worker
startup, hydrate the scratch dir from GCS first. A re-render then
short-circuits every per-call cache hit — cost-of-retry collapses
from ₹15-20 to ~₹0.

Storage layout::

    gs://<YTFACTORY_BUCKET>/jobs/<job_id>/cache/
        panels/panel_000.png      (1-1 mirror of /tmp/render/<job_id>/panels/)
        tts_chunks/chunk_0000.wav
        alignments/<name>.json

Design notes:

* Uploads are **fire-and-forget** (daemon thread) — the render must
  NEVER fail because the cache upload failed. Errors are logged and
  swallowed.
* ``persist_artifact`` is **env-driven** (``YTFACTORY_JOB_ID`` +
  ``YTFACTORY_BUCKET``) so library code doesn't need to thread a
  ``job_id`` parameter through every call site. If either env var is
  unset (e.g. local laptop renders) the call is a silent no-op.
* ``hydrate_cache`` lists ``gs://.../jobs/<job_id>/cache/**`` once at
  worker startup and downloads any pre-existing artifacts back into
  ``work_dir`` under the matching ``cache/`` subdir. The existing
  cache-skip logic in ``_generate_panel_stills`` / ``synth_long_narration``
  then short-circuits those panels/chunks transparently.
* The bucket lifecycle should be configured to delete
  ``jobs/*/cache/`` older than 7 days — cache is just for retries,
  not long-term storage. (Set externally — this module doesn't
  touch lifecycle.)

Idempotency: re-uploading the same object overwrites at the GCS layer;
no harm done. ``persist_artifact`` is therefore safe to call multiple
times for the same path (e.g. the same panel after a regen).
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Per-process pool for fire-and-forget uploads. Bounded so a render
# with 60 panels + 20 TTS chunks doesn't spawn 80 threads at once;
# they queue on a ThreadPoolExecutor sized at 4 (conservative — GCS
# uploads are I/O-bound and we don't want to starve the render's own
# parallel work).
_UPLOAD_POOL: Any = None
_UPLOAD_POOL_LOCK = threading.Lock()
_PENDING_FUTURES: list[Future] = []
_PENDING_FUTURES_LOCK = threading.Lock()
_PERSIST_DISABLED_REASON: str | None = None


def _get_upload_pool():
    """Lazy ThreadPoolExecutor for the persist pool. Idempotent."""
    global _UPLOAD_POOL
    if _UPLOAD_POOL is not None:
        return _UPLOAD_POOL
    with _UPLOAD_POOL_LOCK:
        if _UPLOAD_POOL is None:
            from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
            _UPLOAD_POOL = ThreadPoolExecutor(
                max_workers=int(os.environ.get("YTFACTORY_CACHE_UPLOAD_WORKERS", "4")),
                thread_name_prefix="cache-persist",
            )
    return _UPLOAD_POOL


def _persist_disabled() -> str | None:
    """Reason persist is disabled, or None if it should run.

    Disabled when:
      * YTFACTORY_JOB_ID env unset (local laptop renders, not cloud).
      * YTFACTORY_CACHE_PERSIST=0 (test/debug override).
      * Cached "no bucket env / SDK missing" failure from a prior call.
    """
    global _PERSIST_DISABLED_REASON
    if _PERSIST_DISABLED_REASON is not None:
        return _PERSIST_DISABLED_REASON
    if os.environ.get("YTFACTORY_CACHE_PERSIST", "1") == "0":
        return "YTFACTORY_CACHE_PERSIST=0"
    if not os.environ.get("YTFACTORY_JOB_ID"):
        return "YTFACTORY_JOB_ID unset (local render — no GCS persistence)"
    if not os.environ.get("YTFACTORY_BUCKET"):
        return "YTFACTORY_BUCKET unset"
    return None


def _gcs_blob_path(*, job_id: str, kind: str, name: str) -> str:
    """Build the canonical GCS object path for an artifact."""
    return f"jobs/{job_id}/cache/{kind}/{name}"


def persist_artifact(local_path: Path | str, *, kind: str, name: str | None = None) -> None:
    """Fire-and-forget upload of ``local_path`` to the job's GCS cache prefix.

    Args:
      local_path: file to upload.
      kind: subdirectory under ``cache/`` (e.g. ``"panels"``,
        ``"tts_chunks"``, ``"alignments"``).
      name: object name (default: ``local_path.name``).

    Returns immediately. The upload happens on a daemon thread; errors
    are logged but never re-raised — the render must never fail because
    of a cache upload failure.

    No-op when ``YTFACTORY_JOB_ID`` or ``YTFACTORY_BUCKET`` env vars
    are unset (e.g. local laptop renders).
    """
    reason = _persist_disabled()
    if reason:
        # Log the FIRST disable reason at INFO so cloud renders show
        # explicitly why persistence is off; subsequent calls go to
        # DEBUG to avoid flooding the log.
        global _PERSIST_DISABLED_REASON
        if _PERSIST_DISABLED_REASON is None:
            logger.info("cache.persist disabled: %s", reason)
            _PERSIST_DISABLED_REASON = reason
        return

    p = Path(local_path)
    if not p.exists():
        logger.warning("cache.persist: local path missing: %s", p)
        return

    job_id = os.environ["YTFACTORY_JOB_ID"]
    bucket = os.environ["YTFACTORY_BUCKET"]
    blob_path = _gcs_blob_path(job_id=job_id, kind=kind, name=name or p.name)

    pool = _get_upload_pool()
    fut = pool.submit(_do_upload, str(p), bucket, blob_path)
    with _PENDING_FUTURES_LOCK:
        _PENDING_FUTURES.append(fut)
        # Opportunistic GC: drop already-done futures so the list
        # doesn't grow unbounded across a long render.
        if len(_PENDING_FUTURES) > 64:
            _PENDING_FUTURES[:] = [f for f in _PENDING_FUTURES if not f.done()]


def _do_upload(local: str, bucket: str, blob_path: str) -> None:
    """Worker-thread body of an upload. Never raises."""
    try:
        from google.cloud import storage  # noqa: PLC0415
        client = storage.Client()
        b = client.bucket(bucket)
        blob = b.blob(blob_path)
        blob.upload_from_filename(local)
        logger.debug("cache.persist OK: gs://%s/%s ← %s", bucket, blob_path, local)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cache.persist failed for gs://%s/%s ← %s: %s",
            bucket, blob_path, local, exc,
        )


def hydrate_cache(work_dir: Path | str, *, job_id: str | None = None,
                  bucket: str | None = None) -> dict[str, int]:
    """Download any pre-existing per-call cache for ``job_id`` into ``work_dir``.

    Used by the worker entrypoint on startup so a retry of a previously-
    SIGKILLed render short-circuits every panel/chunk that was already
    paid for.

    Mirrors the GCS layout 1-1 into ``work_dir/cache/<kind>/<name>``.
    The existing cache-skip checks in the renderer
    (``_generate_panel_stills``, ``synth_long_narration``) then no-op
    for those panels/chunks.

    Args:
      work_dir: local target. ``cache/<kind>/`` subdirs are created
        as needed.
      job_id: defaults to ``YTFACTORY_JOB_ID`` env.
      bucket: defaults to ``YTFACTORY_BUCKET`` env.

    Returns: dict ``{kind: count}`` of files hydrated per subdir.
      Empty dict if there was nothing to hydrate (first attempt, GCS
      unavailable, or env unset). Never raises.
    """
    job_id = job_id or os.environ.get("YTFACTORY_JOB_ID")
    bucket = bucket or os.environ.get("YTFACTORY_BUCKET")
    if not job_id or not bucket:
        logger.debug(
            "cache.hydrate skipped: job_id=%r bucket=%r — local render",
            job_id, bucket,
        )
        return {}

    work = Path(work_dir)
    counts: dict[str, int] = {}

    try:
        from google.cloud import storage  # noqa: PLC0415
        client = storage.Client()
        b = client.bucket(bucket)
        prefix = f"jobs/{job_id}/cache/"
        for blob in b.list_blobs(prefix=prefix):
            # blob.name looks like jobs/<job_id>/cache/panels/panel_000.png
            rel = blob.name[len(prefix):]
            if not rel or rel.endswith("/"):
                continue
            kind, _, fname = rel.partition("/")
            if not kind or not fname:
                continue
            # Local target: work_dir/cache/<kind>/<fname>
            # (mirrors how cache_dir is constructed in long_engine.py:202)
            target = work / "cache" / kind / fname
            target.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(target))
            counts[kind] = counts.get(kind, 0) + 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache.hydrate failed for job=%s: %s", job_id, exc)
        return counts

    if counts:
        total = sum(counts.values())
        logger.info(
            "cache.hydrate: restored %d artifacts from gs://%s/jobs/%s/cache/ — %s",
            total, bucket, job_id, counts,
        )
    return counts


def shutdown_upload_pool(wait: bool = True, timeout: float = 30.0) -> None:
    """Cleanly shut down the upload pool — for end-of-render flush.

    Pending uploads finish (up to ``timeout`` seconds) before return.
    Called from the worker's signal handler so a SIGTERM that races
    in just after the last panel is generated still gets the chunk
    persisted before SIGKILL arrives.

    ``ThreadPoolExecutor.shutdown`` doesn't accept a timeout, so we
    explicitly ``wait`` on the tracked futures with one before tearing
    the pool down. Any future not finished by then is abandoned (the
    SIGKILL is about to land anyway).
    """
    global _UPLOAD_POOL
    if _UPLOAD_POOL is None:
        return
    pool = _UPLOAD_POOL
    _UPLOAD_POOL = None
    try:
        if wait:
            from concurrent.futures import wait as _wait  # noqa: PLC0415
            with _PENDING_FUTURES_LOCK:
                pending = [f for f in _PENDING_FUTURES if not f.done()]
                _PENDING_FUTURES.clear()
            if pending:
                done, not_done = _wait(pending, timeout=timeout)
                if not_done:
                    logger.warning(
                        "cache.shutdown: %d uploads abandoned at timeout=%.1fs",
                        len(not_done), timeout,
                    )
        pool.shutdown(wait=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache.shutdown_upload_pool: %s", exc)


def reset_state_for_tests() -> None:
    """Reset the module-level disabled-reason cache. Tests only.

    The disabled-reason flag is process-global to keep the steady-
    state log noise to one line per job; tests that flip env vars
    between cases need to clear it.
    """
    global _PERSIST_DISABLED_REASON, _UPLOAD_POOL
    _PERSIST_DISABLED_REASON = None
    with _PENDING_FUTURES_LOCK:
        _PENDING_FUTURES.clear()
    if _UPLOAD_POOL is not None:
        try:
            _UPLOAD_POOL.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        _UPLOAD_POOL = None


__all__ = [
    "persist_artifact",
    "hydrate_cache",
    "shutdown_upload_pool",
    "reset_state_for_tests",
]
