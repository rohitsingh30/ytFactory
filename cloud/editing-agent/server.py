"""ytFactory cinematic editor — Cloud Run SERVICE.

Single endpoint: ``POST /edit``. Accepts an EDL + a map of
``basename → gs://`` input refs. Downloads each input from GCS,
re-uses the SAME ``pipeline.editing.executor`` module the laptop
runs locally, uploads the produced mp4 to the requested GCS URI,
and returns ``{"output_uri": "gs://..."}``.

Symmetry with the laptop is the point — the EDL schema, the filter
whitelist, the LUT whitelist, and the ffmpeg compile path all live
in ``pipeline/editing/`` and are imported here unchanged. The cloud
service is "the laptop executor + GCS bookends", nothing more.

Endpoints:

* ``GET  /healthz`` — liveness (always 200 once uvicorn is up).
* ``GET  /readyz``  — readiness; verifies ffmpeg is on PATH and the
  shipped LUTs are present. Used by ``cloud/warm_*`` scripts and the
  admin tab.
* ``POST /edit``    — main endpoint.
* ``GET  /version`` — image build SHA + EDL version (handy for
  triaging a "wrong cloud build serving requests" bug).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from pipeline.editing.executor import execute_local
from pipeline.editing.schema import (
    EDL_VERSION,
    EdlValidationError,
    LUT_WHITELIST,
    build_edl_from_planner_json,
    lut_path_for,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("editing-agent")

# OTel SDK boot — exports spans + metrics + structured logs to GCP.
try:
    from otel_init import (
        init as _otel_init,
        instrument_fastapi as _otel_instrument_fastapi,
        instrument_outbound_http as _otel_instrument_outbound,
    )
    _otel_init("editing-agent")
    _otel_instrument_outbound()
    _OTEL_OK = True
except Exception:
    _OTEL_OK = False

app = FastAPI(title="ytFactory editing-agent", version=str(EDL_VERSION))
if _OTEL_OK:
    _otel_instrument_fastapi(app)


# --------------------------------------------------------------- GCS helpers


def _gcs_client():
    from google.cloud import storage  # noqa: PLC0415

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v3")
    return storage.Client(project=project)


def _parse_gcs(uri: str) -> tuple[str, str]:
    p = urlparse(uri)
    if p.scheme != "gs" or not p.netloc:
        raise HTTPException(400, f"invalid gcs uri: {uri!r}")
    return p.netloc, p.path.lstrip("/")


def _download(uri: str, dest: Path) -> Path:
    bucket, blob_path = _parse_gcs(uri)
    client = _gcs_client()
    blob = client.bucket(bucket).blob(blob_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(dest))
    return dest


def _upload(local: Path, uri: str, *, content_type: str | None = None) -> str:
    bucket, blob_path = _parse_gcs(uri)
    blob = _gcs_client().bucket(bucket).blob(blob_path)
    if content_type:
        blob.content_type = content_type
    blob.upload_from_filename(str(local))
    return uri


# ------------------------------------------------------------- request/response


class EditRequest(BaseModel):
    edl: dict[str, Any]
    inputs: dict[str, str]  # basename -> gs:// URI
    output_uri: str  # gs:// URI to write the final mp4
    job_uuid: str | None = None


class EditResponse(BaseModel):
    output_uri: str
    duration_s: float
    note: str = ""


# --------------------------------------------------------------- endpoints


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "ts": time.time()}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    """Readiness — used by cloud/warm scripts + admin tab.

    Verifies ffmpeg is on PATH and every whitelisted LUT exists.
    Returns 503 with an error block if anything's missing so warm
    scripts can fail loud rather than masquerade a broken service as
    healthy."""
    missing_luts = [n for n in LUT_WHITELIST if not lut_path_for(n).exists()]
    if not shutil.which("ffmpeg"):
        raise HTTPException(503, {"ready": False, "reason": "ffmpeg missing"})
    if missing_luts:
        raise HTTPException(503, {"ready": False, "missing_luts": missing_luts})
    return {"ready": True, "luts": sorted(LUT_WHITELIST)}


@app.get("/version")
def version() -> dict[str, Any]:
    return {
        "service": "ytfactory-editing-agent",
        "edl_version": EDL_VERSION,
        "image_sha": os.environ.get("IMAGE_SHA", "unknown"),
        "ffmpeg": _ffmpeg_version(),
    }


def _ffmpeg_version() -> str:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return (proc.stdout or "").splitlines()[:1] or ["unknown"]
    except Exception:  # noqa: BLE001
        return "unknown"


@app.post("/edit", response_model=EditResponse)
def edit(req: EditRequest) -> EditResponse:
    """Compile + run the EDL inside a fresh tempdir.

    Errors are surfaced as HTTP 4xx/5xx with the exception message in
    the body so the laptop client can decide whether to fall back to
    local ffmpeg (5xx → trip breaker) or surface to the user (4xx →
    user EDL is bad)."""
    job_uuid = req.job_uuid or str(uuid.uuid4())
    started = time.time()

    # 1. Validate EDL against the whitelist BEFORE staging anything.
    try:
        edl = build_edl_from_planner_json(req.edl)
    except EdlValidationError as e:
        logger.warning("[%s] EDL validation failed: %s", job_uuid, e)
        raise HTTPException(400, {"error": "edl_invalid", "detail": str(e)}) from e

    # 2. Stage inputs from GCS into a tmp dir.
    work_root = Path(tempfile.mkdtemp(prefix=f"editing-agent-{job_uuid}-"))
    inputs_dir = work_root / "inputs"
    output_dir = work_root / "output"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        for basename, gs_uri in req.inputs.items():
            # Reject path traversal in basename — the EDL schema
            # already forbids this in input_ref but defense in depth.
            if "/" in basename or basename.startswith("."):
                raise HTTPException(
                    400,
                    {"error": "bad_basename", "detail": basename},
                )
            _download(gs_uri, inputs_dir / basename)
            logger.info("[%s] staged %s ← %s", job_uuid, basename, gs_uri)

        # 3. Run the EDL. SAME executor the laptop runs.
        local_out = execute_local(
            edl,
            input_root=inputs_dir,
            output_dir=output_dir,
            output_name=Path(urlparse(req.output_uri).path).name or "edited.mp4",
        )
        logger.info(
            "[%s] ffmpeg done: %s (%.0f bytes)",
            job_uuid, local_out, local_out.stat().st_size,
        )

        # 4. Upload result.
        out_uri = _upload(local_out, req.output_uri, content_type="video/mp4")
        duration = time.time() - started
        logger.info(
            "[%s] /edit OK in %.1fs → %s", job_uuid, duration, out_uri,
        )
        return EditResponse(
            output_uri=out_uri,
            duration_s=round(duration, 2),
            note=(
                f"shots={len(edl.shots)} mode={edl.mode} "
                f"lut={edl.lut} aspect={edl.aspect}"
            ),
        )
    except subprocess.CalledProcessError as e:
        # ffmpeg returned non-zero — usually a malformed filter graph.
        # Surface the stderr tail so the laptop client (or human)
        # can act on it.
        tb = traceback.format_exc()
        logger.error("[%s] ffmpeg failed: %s\n%s", job_uuid, e, tb)
        raise HTTPException(
            500,
            {
                "error": "ffmpeg_failed",
                "rc": e.returncode,
                "stderr_tail": (e.stderr or "")[-2000:],
            },
        ) from e
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("[%s] unhandled: %s\n%s", job_uuid, e, tb)
        raise HTTPException(
            500, {"error": "internal", "detail": f"{e}\n{tb[:2000]}"},
        ) from e
    finally:
        # Always wipe the tempdir — Cloud Run's local FS is ephemeral
        # but explicit cleanup keeps long-running container instances
        # from filling up between requests.
        shutil.rmtree(work_root, ignore_errors=True)
