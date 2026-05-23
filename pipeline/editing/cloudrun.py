"""Cloud Run client for the cinematic editor.

Mirrors :mod:`pipeline.images.images_cloudrun` exactly — same auth,
same retry pattern, same per-render circuit breaker. Falls back to the
local executor on :class:`CloudRunUnavailable` unless
``CLOUDRUN_EDITING_AGENT_DISABLE_FALLBACK=1`` is set.

Env vars:

* ``CLOUDRUN_EDITING_AGENT_URL`` — service URL
  (e.g. ``https://ytfactory-editing-agent-...run.app``).
* ``CLOUDRUN_EDITING_AGENT_TIMEOUT`` — per-call HTTP timeout (default 600 s).
* ``CLOUDRUN_EDITING_AGENT_DISABLE_FALLBACK`` — set to ``1`` to
  hard-error instead of falling back. Use in canary.
* ``YTFACTORY_BUCKET`` — GCS bucket for input/output staging
  (defaults to ``ytfactory-prod-v3-artifacts``).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

from pipeline import observability as obs
from pipeline.cloud.cloudrun_auth import get_id_token
from pipeline.observability.event_helpers import inject_trace_headers

from .circuit_breaker import (
    CloudRunUnavailable,
    breaker_open,
    fallback_disabled,
    reset_circuit_breaker,  # noqa: F401  (re-exported for caller convenience)
    trip_breaker,
)
from .executor import execute_local
from .schema import Edl

logger = logging.getLogger(__name__)


def _service_url() -> str:
    url = os.environ.get("CLOUDRUN_EDITING_AGENT_URL", "").strip().rstrip("/")
    if not url:
        raise CloudRunUnavailable(
            "CLOUDRUN_EDITING_AGENT_URL not set — cloud editing unavailable; "
            "the caller's fallback path will activate."
        )
    return url


def _timeout_s() -> int:
    try:
        return int(os.environ.get("CLOUDRUN_EDITING_AGENT_TIMEOUT", "600"))
    except ValueError:
        return 600


def _bucket() -> str:
    return os.environ.get("YTFACTORY_BUCKET", "ytfactory-prod-v3-artifacts")


def _track_http_call(
    *,
    method: str,
    url: str,
    status_code: int | None,
    duration_ms: int,
    request_body: object | None = None,
    response_body: object | None = None,
    success: bool,
) -> None:
    try:
        obs.track(
            "http.call",
            category="http",
            success=success,
            duration_ms=duration_ms,
            job_id=os.environ.get("YTFACTORY_JOB_ID") or None,
            metadata={
                "service": "editing-agent",
                "method": method,
                "url": url,
                "status_code": status_code,
                "request_sha256": obs.hash_full(request_body),
                "response_sha256": obs.hash_full(response_body),
            },
        )
    except Exception:  # noqa: BLE001
        pass


# --- GCS helpers (lazy-import google-cloud-storage so laptop fallback
#     doesn't require the lib) ---


def _gcs_client():
    from google.cloud import storage  # noqa: PLC0415

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v3")
    return storage.Client(project=project)


def _stage_inputs_to_gcs(input_root: Path, edl: Edl, job_uuid: str) -> dict[str, str]:
    """Upload every distinct ``input_ref`` referenced by the EDL to
    ``gs://<bucket>/editing-agent/<uuid>/inputs/`` and return a map of
    ``basename → gs://`` URI. The cloud service downloads from these
    URIs, runs the EDL locally inside the container, and uploads the
    result.

    Each input is uploaded ONCE even if multiple shots reference it —
    de-duped by basename.
    """
    client = _gcs_client()
    bucket = client.bucket(_bucket())
    refs: dict[str, str] = {}
    for shot in edl.shots:
        ref = shot.input_ref.split("#", 1)[0]  # strip scene marker
        if ref in refs:
            continue
        local = (input_root / ref).resolve()
        if not local.exists():
            raise CloudRunUnavailable(
                f"input {ref!r} missing at {local}; cannot stage to GCS"
            )
        blob_path = f"editing-agent/{job_uuid}/inputs/{ref}"
        blob = bucket.blob(blob_path)
        blob.upload_from_filename(str(local))
        refs[ref] = f"gs://{_bucket()}/{blob_path}"
    return refs


def _download_result(gcs_uri: str, output_path: Path) -> Path:
    """Pull the produced mp4 from GCS to ``output_path``."""
    from urllib.parse import urlparse  # noqa: PLC0415

    p = urlparse(gcs_uri)
    blob = _gcs_client().bucket(p.netloc).blob(p.path.lstrip("/"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(output_path))
    return output_path


# --- HTTP /edit ---


def _post_edit(url: str, payload: dict) -> dict:
    """POST EDL + input refs to ``/edit``. Returns parsed JSON
    ``{"output_uri": "gs://..."}``. Raises :class:`CloudRunUnavailable`
    on connection error / timeout / 5xx."""
    token = get_id_token(url)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    inject_trace_headers(headers)
    timeout = _timeout_s()
    body = json.dumps(payload)
    endpoint = f"{url}/edit"
    backoff_s = [1, 2, 4, 8]
    last_err: Exception | None = None
    for attempt in (1, 2, 3, 4, 5):
        sess = requests.Session()
        t0 = time.perf_counter()
        try:
            resp = sess.post(
                endpoint,
                data=body,
                headers=headers,
                timeout=timeout,
            )
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _track_http_call(
                method="POST",
                url=endpoint,
                status_code=resp.status_code,
                duration_ms=duration_ms,
                request_body=body,
                response_body=resp.text,
                success=resp.status_code == 200,
            )
            if resp.status_code in (429, 503) and attempt <= 4:
                wait = backoff_s[attempt - 1]
                logger.warning(
                    "editing-agent /edit %d (rate exceeded) attempt %d/5; "
                    "sleeping %ds",
                    resp.status_code, attempt, wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                raise CloudRunUnavailable(
                    f"editing-agent /edit returned {resp.status_code}: "
                    f"{resp.text[:400]}"
                )
            return resp.json()
        except requests.exceptions.RequestException as e:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _track_http_call(
                method="POST",
                url=endpoint,
                status_code=None,
                duration_ms=duration_ms,
                request_body=body,
                response_body=str(e),
                success=False,
            )
            last_err = e
            if attempt < 5:
                wait = backoff_s[min(attempt - 1, len(backoff_s) - 1)]
                logger.warning(
                    "editing-agent /edit conn error attempt %d/5: %s; "
                    "sleeping %ds",
                    attempt, e, wait,
                )
                time.sleep(wait)
                continue
            raise CloudRunUnavailable(f"editing-agent unreachable: {e}") from e
        finally:
            sess.close()
    raise CloudRunUnavailable(f"editing-agent retries exhausted: {last_err}")


# --- public entry point ---


def execute_edit(
    edl: Edl,
    *,
    input_root: Path,
    output_dir: Path,
    output_name: str = "edited.mp4",
) -> Path:
    """Cloud-first edit dispatcher.

    Order:

    1. If circuit breaker is OPEN → straight to laptop fallback.
    2. If ``CLOUDRUN_EDITING_AGENT_URL`` not set → laptop fallback
       (but the breaker is NOT tripped — this is a config-not-cloud
       failure mode).
    3. Stage inputs to GCS, POST EDL, poll, download result.
    4. On any cloud failure: trip breaker + run laptop fallback,
       UNLESS ``CLOUDRUN_EDITING_AGENT_DISABLE_FALLBACK=1``.
    """
    if breaker_open():
        logger.info(
            "editing-agent circuit breaker OPEN — routing to laptop ffmpeg",
        )
        return execute_local(
            edl, input_root=input_root, output_dir=output_dir, output_name=output_name,
        )

    try:
        url = _service_url()
    except CloudRunUnavailable as e:
        logger.info("editing-agent: %s", e)
        if fallback_disabled():
            raise
        return execute_local(
            edl, input_root=input_root, output_dir=output_dir, output_name=output_name,
        )

    job_uuid = str(uuid.uuid4())
    output_path = output_dir / output_name

    try:
        gcs_inputs = _stage_inputs_to_gcs(input_root, edl, job_uuid)
        payload = {
            "edl": edl.to_dict(),
            "inputs": gcs_inputs,
            "output_uri": f"gs://{_bucket()}/editing-agent/{job_uuid}/output/{output_name}",
            "job_uuid": job_uuid,
        }
        logger.info(
            "editing-agent: dispatching job=%s shots=%d inputs=%d",
            job_uuid, len(edl.shots), len(gcs_inputs),
        )
        resp = _post_edit(url, payload)
        out_uri = resp.get("output_uri")
        if not out_uri:
            raise CloudRunUnavailable(
                f"editing-agent /edit returned no output_uri: {resp}"
            )
        return _download_result(out_uri, output_path)
    except CloudRunUnavailable as e:
        if fallback_disabled():
            raise
        trip_breaker(str(e))
        logger.warning(
            "editing-agent cloud path failed (%s); falling back to laptop ffmpeg",
            e,
        )
        return execute_local(
            edl, input_root=input_root, output_dir=output_dir, output_name=output_name,
        )


def warmup() -> bool:
    """Probe ``/readyz`` so the cold-load happens behind the renderer's
    boot. Returns True if the service responded 200, False otherwise.
    Wired into :mod:`pipeline.cloud.warm` via ``include_editing=True``.
    """
    try:
        url = _service_url()
    except CloudRunUnavailable:
        return False
    endpoint = f"{url}/readyz"
    try:
        token = get_id_token(url)
        headers = {"Authorization": f"Bearer {token}"}
        inject_trace_headers(headers)
        t0 = time.perf_counter()
        resp = requests.get(
            endpoint,
            headers=headers,
            timeout=60,
        )
        duration_ms = int((time.perf_counter() - t0) * 1000)
        _track_http_call(
            method="GET",
            url=endpoint,
            status_code=resp.status_code,
            duration_ms=duration_ms,
            request_body=None,
            response_body=resp.text,
            success=resp.status_code == 200,
        )
        return resp.status_code == 200
    except Exception as e:  # noqa: BLE001
        try:
            _track_http_call(
                method="GET",
                url=endpoint,
                status_code=None,
                duration_ms=0,
                request_body=None,
                response_body=str(e),
                success=False,
            )
        except Exception:  # noqa: BLE001
            pass
        logger.info("editing-agent warmup probe failed: %s", e)
        return False
