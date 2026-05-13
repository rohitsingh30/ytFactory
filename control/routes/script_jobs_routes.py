"""Cloud-native /api/jobs/from_script — the skill→cloud render entry.

Skills (the AI authoring layer) POST renderer commands here. The
endpoint validates the entry-point against an allow-list, packages
the channel YAML + script JSON (+ optional raw) into a spec.json,
uploads it to GCS, triggers the ytfactory-render-worker-v2 Cloud
Run JOB with JOB_SPEC_GCS_URI, and polls state.json until terminal.

Ported from web/server.py (lines ~2351-2700) for the 2026-05-09
website-first cloud cutover. The legacy laptop subprocess backend is
NOT carried over — this module is cloud-only. The control plane runs
on Cloud Run; bash-execing local Python is meaningless there.

Endpoints:
  POST /api/jobs/from_script        — submit a render
  GET  /api/jobs/from_script/{id}   — poll status
  GET  /api/jobs/from_script/{id}/mp4 — proxy mp4 download

Pairs with `pipeline.skill_dispatch` (the client-side helper).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# ----------------------------------------------------------------------------
# Config (Cloud Run env)
# ----------------------------------------------------------------------------

CLOUDRUN_JOB_NAME = os.environ.get("YTFACTORY_CLOUDRUN_JOB", "ytfactory-render-worker-v2")
CLOUDRUN_JOB_REGION = os.environ.get("YTFACTORY_CLOUDRUN_REGION", "asia-southeast1")
CLOUDRUN_JOB_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")
CLOUDRUN_ARTIFACTS_BUCKET = os.environ.get("YTFACTORY_BUCKET", "ytfactory-prod-v2-artifacts")
STATE_BUCKET = os.environ.get("YTFACTORY_STATE_BUCKET", "ytfactory-prod-v2-state")

# Renderer entry points the cloud render-worker image carries. Anything
# not in this set is rejected at the API boundary so a typo/poison
# payload can't trick the worker into running arbitrary code.
_ALLOWED_RENDER_CMDS: set[str] = {
    "scripts/make_shorts.py",
    "historyrecapped/scripts/render_long_form.py",
    "historyrecapped/scripts/render_footage_only.py",
    "sportsrecapped/scripts/render_long_form_doc.py",
    "sportsrecapped/scripts/render_tweet_reaction.py",
    "sportsrecapped/scripts/render_rivalry_compilation.py",
    "scrollpulse/scripts/render_split_screen.py",
}

# Audit Q2.44 — pre-fix this dict and ``web/server.py``'s
# ``ScriptJobsStore`` were two independent stores for the same
# logical concept. The web app's ``app.include_router`` order means
# ``POST /api/jobs/from_script`` in control IS SHADOWED by the
# matching @app.post in web/server.py — so this dict only ever
# accumulates rows in dev (when control/server_dev.py runs as the
# entrypoint). Documenting the dev-only role to make the contract
# explicit. Production prod_jobs persistence + cross-revision
# rehydration lives in web/server.py::SCRIPT_JOBS (a real
# Firestore-backed ScriptJobsStore).
SCRIPT_JOBS: dict[str, dict[str, Any]] = {}


# ----------------------------------------------------------------------------
# GCS helpers
# ----------------------------------------------------------------------------

def _gcs_client():
    from google.cloud import storage  # noqa: PLC0415
    return storage.Client(project=CLOUDRUN_JOB_PROJECT)


def _gcs_upload_text(text: str, uri: str, content_type: str = "application/json") -> None:
    p = urlparse(uri)
    blob = _gcs_client().bucket(p.netloc).blob(p.path.lstrip("/"))
    blob.upload_from_string(text, content_type=content_type)


def _gcs_read_json(uri: str) -> dict:
    p = urlparse(uri)
    blob = _gcs_client().bucket(p.netloc).blob(p.path.lstrip("/"))
    return json.loads(blob.download_as_bytes())


def _gcs_read_bytes_if_exists(bucket: str, name: str) -> bytes | None:
    blob = _gcs_client().bucket(bucket).blob(name)
    if not blob.exists():
        return None
    return blob.download_as_bytes()


def _b64_bytes(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64_file_or_gcs(rel_path: str) -> tuple[str, bytes] | None:
    """Resolve `rel_path` (e.g. mystoriesanimated/.../narrations/X.json) to
    a (relative-path, bytes) pair. Tries GCS state bucket first
    (YTFACTORY_STATE_BUCKET/<rel_path>), then falls back to /workspace
    (the bake-into-image path used for channel YAMLs)."""
    workspace = Path("/workspace")
    candidate = workspace / rel_path
    if candidate.is_file():
        return rel_path, candidate.read_bytes()
    blob = _gcs_read_bytes_if_exists(STATE_BUCKET, rel_path)
    if blob is not None:
        return rel_path, blob
    return None


# ----------------------------------------------------------------------------
# Cloud Run JOB trigger + state polling
# ----------------------------------------------------------------------------

async def _trigger_job_or_runtime_error(
    job_name: str,
    project: str,
    region: str,
    env_overrides: dict[str, str],
) -> str:
    """Q2.41 — wrap ``execute_job_async`` so failures surface as
    RuntimeError rather than leaking SDK exception types into the
    SCRIPT_JOBS error column. Mirrors web.server._trigger_cloudrun_job.
    """
    from control.core import cloud_run as _cr  # noqa: PLC0415
    try:
        return await asyncio.to_thread(
            _cr.execute_job_async,
            job_name,
            project=project,
            region=region,
            env_overrides=env_overrides,
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"cloud run jobs execute failed: {e}") from e


async def _run_cloudrun(job_id: str, cmd_in: list[str]) -> None:
    rec = SCRIPT_JOBS[job_id]
    try:
        # 1. Resolve --channel / --script paths from cmd args.
        channel_yaml_rel: str | None = None
        script_path_rel: str | None = None
        it = iter(cmd_in)
        next(it)  # skip the entry-point script
        for tok in it:
            if tok == "--channel":
                channel_yaml_rel = next(it)
            elif tok == "--script":
                script_path_rel = next(it)
        if not (channel_yaml_rel and script_path_rel):
            raise ValueError("cmd missing --channel or --script")

        # 2. Load channel YAML (always baked into image) + script JSON
        #    (always in GCS state bucket per the laptop→GCS sync).
        chan_resolved = _b64_file_or_gcs(channel_yaml_rel)
        if chan_resolved is None:
            raise FileNotFoundError(
                f"channel YAML not found in image or GCS state bucket: {channel_yaml_rel}"
            )
        script_resolved = _b64_file_or_gcs(script_path_rel)
        if script_resolved is None:
            raise FileNotFoundError(
                f"script JSON not found in GCS state bucket: {script_path_rel}. "
                f"Wait for the laptop state-sync watcher to push it, or "
                f"run scripts/sync_state_to_gcs.sh manually."
            )

        # Optional raw file (e.g. <channel>/raw/<slug>.json) — best-effort.
        raw_b64: str | None = None
        raw_rel: str | None = None
        sp = Path(script_path_rel)
        if sp.parent.name in ("scripts", "narrations"):
            raw_candidate = str(sp.parent.parent / "raw" / f"{sp.stem}.json")
            r = _b64_file_or_gcs(raw_candidate)
            if r is not None:
                raw_rel, raw_bytes = r
                raw_b64 = _b64_bytes(raw_bytes)

        # 3. Build spec.
        spec: dict[str, Any] = {
            "job_id": job_id,
            "cmd": list(cmd_in),
            "channel_yaml_path": channel_yaml_rel,
            "channel_yaml_b64": _b64_bytes(chan_resolved[1]),
            "script_path": script_path_rel,
            "script_json_b64": _b64_bytes(script_resolved[1]),
        }
        if raw_b64 and raw_rel:
            spec["raw_path"] = raw_rel
            spec["raw_b64"] = raw_b64

        # 4. Upload spec.json to GCS.
        spec_uri = f"gs://{CLOUDRUN_ARTIFACTS_BUCKET}/jobs/{job_id}/spec.json"
        await asyncio.to_thread(_gcs_upload_text, json.dumps(spec), spec_uri)
        rec["spec_uri"] = spec_uri

        # 5. Trigger Cloud Run JOB. Audit Q2.41 — pre-fix this shelled
        #    out to gcloud, which is NOT installed in the slim Cloud
        #    Run image. Now route through the SDK helper. Same SA
        #    grants apply (run.developer / run.invoker).
        execution_name = await _trigger_job_or_runtime_error(
            CLOUDRUN_JOB_NAME,
            CLOUDRUN_JOB_PROJECT,
            CLOUDRUN_JOB_REGION,
            {"JOB_SPEC_GCS_URI": spec_uri},
        )
        rec["cloudrun_execution"] = execution_name

        # 6. Poll state.json from GCS until terminal.
        state_uri = f"gs://{CLOUDRUN_ARTIFACTS_BUCKET}/jobs/{job_id}/state.json"
        terminal = ("done", "done_no_mp4_found", "failed")
        for _ in range(720):  # ~1 hr at 5s poll
            await asyncio.sleep(5)
            try:
                state = await asyncio.to_thread(_gcs_read_json, state_uri)
            except Exception:
                continue
            if state.get("state") in terminal:
                rec["state"] = state["state"]
                rec["exit_code"] = state.get("exit_code")
                rec["error"] = state.get("error")
                rec["mp4_path"] = state.get("mp4_uri")
                rec["completed_at"] = state.get("completed_at") or time.time()
                rec["log_uri"] = state.get("log_uri")
                return
        rec["state"] = "failed"
        rec["error"] = "polling timed out after 1 hour"
        rec["completed_at"] = time.time()
    except Exception as e:
        logger.exception("cloudrun render-job %s failed", job_id)
        rec["state"] = "failed"
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["completed_at"] = time.time()


# ----------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------

@router.post("/api/jobs/from_script")
async def create_script_job(payload: dict) -> dict:
    """Submit a renderer command for cloud execution.

    Body (preferred — generic):
        cmd:    list[str], e.g.
                ["scripts/make_shorts.py",
                 "--channel", "mystoriesanimated/variants/aita_animated.yaml",
                 "--script", "mystoriesanimated/.../scripts/<slug>.json"]
        First element MUST be in the `_ALLOWED_RENDER_CMDS` whitelist.

    Body (legacy convenience for shorts):
        channel_yaml + script_path  →  rewritten to the cmd above.
    """
    cmd_in: list[str] = list(payload.get("cmd") or [])

    if not cmd_in and (payload.get("channel_yaml") and payload.get("script_path")):
        cmd_in = [
            "scripts/make_shorts.py",
            "--channel", str(payload["channel_yaml"]),
            "--script", str(payload["script_path"]),
        ]
        cmd_in.extend(list(payload.get("extra_args") or []))

    if not cmd_in:
        raise HTTPException(
            400,
            "either `cmd: [...]` or `{channel_yaml, script_path}` is required",
        )
    entry = cmd_in[0]
    if entry not in _ALLOWED_RENDER_CMDS:
        raise HTTPException(
            400,
            f"entry-point {entry!r} not in allowed whitelist: "
            f"{sorted(_ALLOWED_RENDER_CMDS)}",
        )

    job_id = uuid.uuid4().hex[:10]
    label = payload.get("label") or " ".join(cmd_in[:6])
    SCRIPT_JOBS[job_id] = {
        "job_id": job_id,
        "label": label,
        "cmd": list(cmd_in),
        "state": "running",
        "started_at": time.time(),
        "completed_at": None,
        "exit_code": None,
        "mp4_path": None,
        "error": None,
        "backend": "cloudrun",
    }
    asyncio.create_task(_run_cloudrun(job_id, cmd_in))
    return {"job_id": job_id, "state": "running", "backend": "cloudrun"}


@router.get("/api/jobs/from_script/{job_id}")
async def get_script_job(job_id: str) -> dict:
    rec = SCRIPT_JOBS.get(job_id)
    if not rec:
        raise HTTPException(404, "script job not found")
    out = dict(rec)
    out["elapsed_s"] = round(
        (rec["completed_at"] or time.time()) - rec["started_at"], 1
    )
    # Lightweight log tail from GCS (best-effort).
    log_uri = rec.get("log_uri")
    if log_uri:
        try:
            p = urlparse(log_uri)
            blob = _gcs_client().bucket(p.netloc).blob(p.path.lstrip("/"))
            if blob.exists():
                size = blob.size or 0
                tail_bytes = min(8 * 1024, size)
                if tail_bytes:
                    data = blob.download_as_bytes(start=size - tail_bytes)
                    out["log_tail"] = data.decode("utf-8", errors="replace")
        except Exception:
            pass
    return out


@router.get("/api/jobs/from_script/{job_id}/mp4")
async def get_script_job_mp4(job_id: str):
    rec = SCRIPT_JOBS.get(job_id)
    if not rec:
        raise HTTPException(404, "script job not found")
    mp4 = rec.get("mp4_path")
    if not mp4 or not mp4.startswith("gs://"):
        raise HTTPException(404, "mp4 not yet available")
    # Sign + redirect — caller fetches directly from GCS.
    p = urlparse(mp4)
    blob = _gcs_client().bucket(p.netloc).blob(p.path.lstrip("/"))
    from datetime import timedelta
    url = blob.generate_signed_url(version="v4", expiration=timedelta(minutes=15))
    return RedirectResponse(url=url, status_code=302)


@router.get("/api/jobs/from_script")
async def list_script_jobs() -> dict:
    """Recent jobs newest first."""
    rows = sorted(SCRIPT_JOBS.values(), key=lambda r: -r["started_at"])
    return {"jobs": rows[:50]}
