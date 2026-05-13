"""Cloud Run Job render dispatcher (Layer 2).

When ``YTFACTORY_RENDER_BACKEND=cloudrun`` is set, the control plane
triggers the ``ytfactory-render-worker-v2`` Cloud Run Job (one
execution per render) instead of letting the sim worker pick up the
queued task.

The Job entry point is ``cloud/render-worker-v2/entrypoint.py``. It
reads ``YTFACTORY_JOB_ID`` from env, fetches the job doc from
Firestore, and runs the pipeline. Every stage transition writes back
to ``jobs/<job_id>`` so the UI's poll loop (``/api/jobs/{id}``) stays
live.

This module is a thin wrapper around ``google.cloud.run_v2``. Two
back-ends:

- "google-cloud-run" SDK (default in production)
- "gcloud" CLI fallback for environments without the SDK installed

Callable surface — only one function the control plane uses:

    trigger_render_job(job_id) -> ExecutionRef

Designed to be cheap to call: ``RunJobRequest`` with no override args
hits the Job's already-deployed env, only adding ``YTFACTORY_JOB_ID``
as a per-execution override. ``project_id`` / ``region`` / ``job_name``
all come from env so the same code runs in dev and prod.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BACKEND_ENV = "YTFACTORY_RENDER_BACKEND"  # sim | cloudrun
PROJECT_ENV = "GOOGLE_CLOUD_PROJECT"
REGION_ENV = "YTFACTORY_CLOUDRUN_REGION"
JOB_NAME_ENV = "YTFACTORY_CLOUDRUN_JOB"

DEFAULT_PROJECT = "ytfactory-prod-v2"
DEFAULT_REGION = "asia-southeast1"
DEFAULT_JOB_NAME = "ytfactory-render-worker-v2"


def render_backend() -> str:
    """One of: ``sim`` (default), ``cloudrun``, ``laptop`` (deprecated)."""
    return os.environ.get(BACKEND_ENV, "sim").lower().strip()


def is_cloudrun() -> bool:
    return render_backend() == "cloudrun"


def project_id() -> str:
    return os.environ.get(PROJECT_ENV, DEFAULT_PROJECT)


def region() -> str:
    return os.environ.get(REGION_ENV, DEFAULT_REGION)


def job_name() -> str:
    return os.environ.get(JOB_NAME_ENV, DEFAULT_JOB_NAME)


@dataclass
class ExecutionRef:
    """Lightweight handle to a triggered Cloud Run Job execution."""

    job_id: str
    execution_name: str           # full resource name: projects/.../executions/<id>
    triggered_via: str            # "sdk" | "cli"


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


def trigger_render_job(job_id: str) -> ExecutionRef:
    """Launch one Cloud Run Job execution for ``job_id``.

    Tries the google-cloud-run SDK first; falls back to ``gcloud run
    jobs execute`` if the SDK isn't installed (handy for dev boxes).
    """
    if not job_id:
        raise ValueError("job_id is required")

    try:
        return _trigger_via_sdk(job_id)
    except ImportError:
        logger.info(
            "google-cloud-run not installed; falling back to gcloud CLI"
        )
        return _trigger_via_cli(job_id)


def _trigger_via_sdk(job_id: str) -> ExecutionRef:
    """Preferred path — uses the typed Python SDK."""
    from google.cloud import run_v2  # noqa: PLC0415

    client = run_v2.JobsClient()
    job_resource = (
        f"projects/{project_id()}/locations/{region()}/jobs/{job_name()}"
    )

    # Per-execution env override: YTFACTORY_JOB_ID + (when this caller
    # is inside an active OTel span) YTFACTORY_TRACEPARENT so the
    # JOB's root span links back to the chat-request span that
    # triggered it. Everything else (CLOUDRUN_*_URL,
    # GOOGLE_CLOUD_PROJECT, etc.) comes from the Job's deploy-time
    # env so the override stays minimal.
    env_overrides = [
        run_v2.EnvVar(name="YTFACTORY_JOB_ID", value=job_id),
    ]
    for k, v in _trace_env_overrides().items():
        env_overrides.append(run_v2.EnvVar(name=k, value=v))

    overrides = run_v2.RunJobRequest.Overrides(
        container_overrides=[
            run_v2.RunJobRequest.Overrides.ContainerOverride(
                env=env_overrides,
            )
        ],
    )
    request = run_v2.RunJobRequest(name=job_resource, overrides=overrides)
    operation = client.run_job(request=request)

    # Don't .result() — that blocks until the job finishes. Return the
    # execution ref so the orchestrator stays async.
    execution_name = operation.metadata.name if operation.metadata else "(pending)"
    logger.info(
        "triggered Cloud Run Job execution: job_id=%s execution=%s",
        job_id, execution_name,
    )
    return ExecutionRef(
        job_id=job_id,
        execution_name=execution_name,
        triggered_via="sdk",
    )


def _trigger_via_cli(job_id: str) -> ExecutionRef:
    """Fallback — shells out to ``gcloud run jobs execute --async``."""
    if shutil.which("gcloud") is None:
        raise RuntimeError(
            "Neither google-cloud-run SDK nor gcloud CLI is available. "
            "Install one of:\n"
            "  pip install google-cloud-run\n"
            "  brew install --cask google-cloud-sdk"
        )

    # Use ``^|^`` delimiter so a tracestate value containing a comma
    # can't break the gcloud parser. ``YTFACTORY_TRACEPARENT`` itself
    # is a fixed shape (``00-<trace>-<span>-<flags>``) with no commas;
    # ``YTFACTORY_TRACESTATE`` may contain ``,`` in multi-vendor cases.
    pairs = [f"YTFACTORY_JOB_ID={job_id}"]
    for k, v in _trace_env_overrides().items():
        pairs.append(f"{k}={v}")
    env_arg = "^|^" + "|".join(pairs) if len(pairs) > 1 else pairs[0]

    cmd = [
        "gcloud", "run", "jobs", "execute", job_name(),
        "--project", project_id(),
        "--region", region(),
        "--update-env-vars", env_arg,
        "--async",
        "--format", "value(metadata.name)",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"gcloud run jobs execute failed (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip()}"
        )
    execution_name = (proc.stdout or "").strip() or "(unknown)"
    logger.info(
        "triggered Cloud Run Job via CLI: job_id=%s execution=%s",
        job_id, execution_name,
    )
    return ExecutionRef(
        job_id=job_id,
        execution_name=execution_name,
        triggered_via="cli",
    )


# ---------------------------------------------------------------------------
# Audit Q2.41 — generic JOB-execute helper for jobs OTHER than the
# render-worker. The slim Cloud Run image does NOT ship the gcloud CLI
# (per memory/feedback_cloudrun_dispatch_sdk_required.md). Three sites
# pre-fix called ``asyncio.create_subprocess_exec("gcloud", ...)`` →
# the binary was missing → 500 with "FileNotFoundError: gcloud".
#
# This helper uses google-cloud-run SDK with the same fallback to CLI
# (handy on dev boxes / tests) but with no implicit binding to the
# render-worker job_name. Pass job_name + env_overrides explicitly.
# ---------------------------------------------------------------------------


def execute_job_async(
    job_name: str,
    *,
    project: str | None = None,
    region: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> str:
    """Trigger a Cloud Run JOB asynchronously. Returns the execution name.

    Tries the SDK first; falls back to gcloud CLI for dev-only paths
    where SDK isn't installed. Raises RuntimeError if NEITHER is
    available. Audit Q2.41 — pre-fix three call sites in web/server.py
    + control/routes/script_jobs_routes.py shelled to gcloud directly,
    which the slim Cloud Run image doesn't ship.
    """
    proj = project or project_id()
    reg = region or globals().get("region", lambda: "asia-southeast1")()
    try:
        # Use the dotted import form so tests can stub via
        # ``sys.modules["google.cloud.run_v2"] = None`` (matches
        # the pattern used by _sdk_available).
        import google.cloud.run_v2 as run_v2  # noqa: PLC0415
    except ImportError:
        # Fallback to CLI (dev only — slim Cloud Run image lacks gcloud).
        if shutil.which("gcloud") is None:
            raise RuntimeError(
                "Neither google-cloud-run SDK nor gcloud CLI available. "
                "Install: pip install google-cloud-run"
            )
        return _execute_job_via_cli(job_name, proj, reg, env_overrides or {})

    client = run_v2.JobsClient()
    job_resource = f"projects/{proj}/locations/{reg}/jobs/{job_name}"
    overrides = None
    if env_overrides:
        overrides = run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[run_v2.EnvVar(name=k, value=v)
                         for k, v in env_overrides.items()],
                )
            ],
        )
    request = run_v2.RunJobRequest(name=job_resource, overrides=overrides)
    operation = client.run_job(request=request)
    execution_name = operation.metadata.name if operation.metadata else "(pending)"
    logger.info(
        "triggered Cloud Run Job via SDK: name=%s execution=%s",
        job_name, execution_name,
    )
    return execution_name


def _execute_job_via_cli(
    job_name_value: str, proj: str, reg: str, env_overrides: dict[str, str]
) -> str:
    cmd = [
        "gcloud", "run", "jobs", "execute", job_name_value,
        "--project", proj,
        "--region", reg,
        "--async",
        "--format", "value(metadata.name)",
    ]
    if env_overrides:
        pairs = [f"{k}={v}" for k, v in env_overrides.items()]
        env_arg = "^|^" + "|".join(pairs) if len(pairs) > 1 else pairs[0]
        cmd[-2:-2] = ["--update-env-vars", env_arg]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"gcloud run jobs execute failed (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip()}"
        )
    return (proc.stdout or "").strip() or "(unknown)"


def _trace_env_overrides() -> dict[str, str]:
    """Return ``{YTFACTORY_TRACEPARENT, YTFACTORY_TRACESTATE}`` from the
    active OTel span, or an empty dict when no span is active / OTel
    isn't installed.

    Keeping the import inside the function so callers without OTel
    installed (e.g. legacy laptop CI lanes) don't pay an import cost.
    """
    try:
        from pipeline.observability import propagation  # noqa: PLC0415
        env: dict[str, str] = {}
        propagation.inject_into_env(env)
        return env
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Health / introspection
# ---------------------------------------------------------------------------


def status() -> dict:
    return {
        "backend": render_backend(),
        "project": project_id(),
        "region": region(),
        "job_name": job_name(),
        "sdk_available": _sdk_available(),
        "cli_available": shutil.which("gcloud") is not None,
    }


def _sdk_available() -> bool:
    try:
        import google.cloud.run_v2  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False
