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

import json
import logging
import os
import shutil
import subprocess
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    import _tel_track_io as _tel
except Exception:  # noqa: BLE001
    class _NoopTelemetry:
        def track(self, *_args, **_kwargs) -> None:
            return None
        def track_io(self, *_args, **_kwargs) -> None:
            return None
    _tel = _NoopTelemetry()
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
    prompt: str | None = None


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
    cmd = ["ffmpeg", "-version"]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=5, check=False,
        )
        duration_ms = int((time.perf_counter() - t0) * 1000)
        _tel.track(
            "ffmpeg.call",
            category="ffmpeg",
            success=proc.returncode == 0,
            duration_ms=duration_ms,
            metadata={
                "purpose": "editing_agent_version_probe",
                "args": cmd,
                "exit_code": proc.returncode,
                "stderr_tail": (proc.stderr or "")[-4000:],
                "output_bytes": 0,
            },
        )
        return ((proc.stdout or "").splitlines()[:1] or ["unknown"])[0]
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - t0) * 1000)
        _tel.track(
            "ffmpeg.call",
            category="ffmpeg",
            success=False,
            duration_ms=duration_ms,
            metadata={
                "purpose": "editing_agent_version_probe",
                "args": cmd,
                "exit_code": -1,
                "stderr_tail": str(exc)[-4000:],
                "output_bytes": 0,
            },
        )
        return "unknown"


def _work_root(job_uuid: str) -> Path:
    root = Path(os.environ.get("EDITING_AGENT_WORK_ROOT", ".editing-agent-work"))
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"editing-agent-{job_uuid}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _output_bytes(path: Path) -> int:
    try:
        return path.stat().st_size if path.exists() else 0
    except Exception:  # noqa: BLE001
        return 0


def _ffmpeg_observer(job_id: str, counter: dict[str, int]) -> Callable[[list[str], int, str, int, Path], None]:
    def _observe(cmd: list[str], rc: int, stderr: str, duration_ms: int, output_path: Path) -> None:
        counter["count"] = int(counter.get("count", 0)) + 1
        _tel.track(
            "ffmpeg.call",
            category="ffmpeg",
            success=rc == 0,
            duration_ms=duration_ms,
            job_id=job_id,
            metadata={
                "purpose": "editing_agent_execute_local",
                "args": [str(c) for c in cmd],
                "exit_code": rc,
                "stderr_tail": (stderr or "")[-4000:],
                "output_bytes": _output_bytes(output_path),
            },
        )

    return _observe


def _emit_editing_plan(
    req: EditRequest,
    *,
    job_id: str,
    success: bool,
    started: float,
    ffmpeg_calls: int,
    output_text: Any,
    error: str | None = None,
) -> None:
    prompt = req.prompt or json.dumps(req.edl, sort_keys=True, default=str)
    output_meta: dict[str, Any] = {"ffmpeg_calls": ffmpeg_calls}
    if error:
        output_meta["error"] = error[:500]
    _tel.track_io(
        "editing.plan",
        category="pipeline",
        success=success,
        duration_ms=int((time.time() - started) * 1000),
        job_id=job_id,
        input_text=prompt,
        output_text=output_text,
        output_meta=output_meta,
    )


@app.post("/edit", response_model=EditResponse)
def edit(req: EditRequest) -> EditResponse:
    """Compile + run the EDL inside a fresh per-request work dir.

    Errors are surfaced as HTTP 4xx/5xx with the exception message in
    the body so the laptop client can decide whether to fall back to
    local ffmpeg (5xx → trip breaker) or surface to the user (4xx →
    user EDL is bad)."""
    job_uuid = req.job_uuid or str(uuid.uuid4())
    started = time.time()
    plan_emitted = False
    ffmpeg_calls = 0

    # 1. Validate EDL against the whitelist BEFORE staging anything.
    try:
        edl = build_edl_from_planner_json(req.edl)
    except EdlValidationError as e:
        logger.warning("[%s] EDL validation failed: %s", job_uuid, e)
        _emit_editing_plan(
            req,
            job_id=job_uuid,
            success=False,
            started=started,
            ffmpeg_calls=0,
            output_text=str(e),
            error="edl_invalid",
        )
        raise HTTPException(400, {"error": "edl_invalid", "detail": str(e)}) from e

    # 2. Stage inputs from GCS into a per-request work dir.
    work_root = _work_root(job_uuid)
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
        ffmpeg_counter = {"count": 0}
        local_out = execute_local(
            edl,
            input_root=inputs_dir,
            output_dir=output_dir,
            output_name=Path(urlparse(req.output_uri).path).name or "edited.mp4",
            ffmpeg_observer=_ffmpeg_observer(job_uuid, ffmpeg_counter),
        )
        ffmpeg_calls = ffmpeg_counter["count"]
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
        _emit_editing_plan(
            req,
            job_id=job_uuid,
            success=True,
            started=started,
            ffmpeg_calls=ffmpeg_calls,
            output_text={
                "output_uri": out_uri,
                "shots": len(edl.shots),
                "mode": edl.mode,
                "lut": edl.lut,
                "aspect": edl.aspect,
            },
        )
        plan_emitted = True
        return EditResponse(
            output_uri=out_uri,
            duration_s=round(duration, 2),
            note=(
                f"shots={len(edl.shots)} mode={edl.mode} "
                f"lut={edl.lut} aspect={edl.aspect}"
            ),
        )
    except subprocess.CalledProcessError as e:
        ffmpeg_calls = max(ffmpeg_calls, int((locals().get("ffmpeg_counter") or {}).get("count", 0)))
        # ffmpeg returned non-zero — usually a malformed filter graph.
        # Surface the stderr tail so the laptop client (or human)
        # can act on it.
        tb = traceback.format_exc()
        logger.error("[%s] ffmpeg failed: %s\n%s", job_uuid, e, tb)
        if not plan_emitted:
            _emit_editing_plan(
                req,
                job_id=job_uuid,
                success=False,
                started=started,
                ffmpeg_calls=ffmpeg_calls,
                output_text=(e.stderr or "")[-2000:],
                error="ffmpeg_failed",
            )
            plan_emitted = True
        raise HTTPException(
            500,
            {
                "error": "ffmpeg_failed",
                "rc": e.returncode,
                "stderr_tail": (e.stderr or "")[-2000:],
            },
        ) from e
    except HTTPException as e:
        ffmpeg_calls = max(ffmpeg_calls, int((locals().get("ffmpeg_counter") or {}).get("count", 0)))
        if not plan_emitted:
            _emit_editing_plan(
                req,
                job_id=job_uuid,
                success=False,
                started=started,
                ffmpeg_calls=ffmpeg_calls,
                output_text=getattr(e, "detail", ""),
                error="http_exception",
            )
            plan_emitted = True
        raise
    except Exception as e:  # noqa: BLE001
        ffmpeg_calls = max(ffmpeg_calls, int((locals().get("ffmpeg_counter") or {}).get("count", 0)))
        tb = traceback.format_exc()
        logger.error("[%s] unhandled: %s\n%s", job_uuid, e, tb)
        if not plan_emitted:
            _emit_editing_plan(
                req,
                job_id=job_uuid,
                success=False,
                started=started,
                ffmpeg_calls=ffmpeg_calls,
                output_text=f"{e}\n{tb[:2000]}",
                error=type(e).__name__,
            )
            plan_emitted = True
        raise HTTPException(
            500, {"error": "internal", "detail": f"{e}\n{tb[:2000]}"},
        ) from e
    finally:
        # Always wipe the work dir — Cloud Run's local FS is ephemeral
        # but explicit cleanup keeps long-running container instances
        # from filling up between requests.
        shutil.rmtree(work_root, ignore_errors=True)
