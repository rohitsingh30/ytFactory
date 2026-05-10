"""Burner-channel endpoints — drive the /app/burner-channels page.

GET  /api/burner_channels                  list of burners + counts
GET  /api/burner_channels/catalog          live catalog the burner will engage with
POST /api/burner_channels/{slug}/engage    kick off worker
GET  /api/burner_channels/{slug}/engage    poll status JSON
POST /api/burner_channels/{slug}/engage/stop  request graceful stop

Worker model (post 2026-05-09 cloud cutover):
- On Cloud Run (``K_SERVICE`` env set), POST enqueues a ``BURNER_ENGAGE``
  task in the Firestore-backed queue. The laptop agent
  (``pipeline.laptop_agent``) leases it and runs the worker locally
  (Chrome can't run on Cloud Run). State flows back through GCS so
  this endpoint's GET poll sees live progress — see
  ``pipeline.cross_engage.burner_engage._save_state`` /
  ``read_state`` for the GCS path.
- On laptop dev (no ``K_SERVICE``), POST keeps the legacy
  ``subprocess.Popen`` path so a single-machine workflow still works
  end-to-end without a queue.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from pipeline.cross_engage import burner_engage
from pipeline.utils import catalog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/burner_channels")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WORKER_LOG_DIR = PROJECT_ROOT / "data" / "burner_engage" / "logs"


@router.get("")
async def list_burners() -> dict:
    burners = burner_engage.list_burner_channels()
    # Hydrate live status (running / stopped / never-run).
    # Prewarm the GCS state cache with ONE list_blobs + parallel
    # downloads — without this, each `read_state(slug)` below would
    # fan out to its own GCS round trip (49 burners × ~100ms = ~5s
    # per dashboard poll, with the page polling every 5s).
    catalog_size = catalog.catalog_count()
    burner_engage.prewarm_states([b["slug"] for b in burners])
    out = []
    for b in burners:
        st = burner_engage.read_state(b["slug"])
        out.append({
            **b,
            # Pass the already-fetched state so is_running doesn't
            # re-resolve it (the 2s TTL cache hides this on warm runs
            # but a cold poll would otherwise pay double).
            "running": burner_engage.is_running(b["slug"], state=st),
            "phase": (st or {}).get("phase"),
            "last_action_at": (st or {}).get("last_action_at"),
            "last_action_msg": (st or {}).get("last_action_msg"),
        })
    return {"burners": out, "catalog_size": catalog_size}


@router.get("/catalog")
async def get_catalog() -> dict:
    """The catalog any burner will engage with."""
    rows = catalog.list_catalog_dicts()
    return {"videos": rows, "total": len(rows)}


@router.post("/{slug}/engage")
async def start_engage(slug: str, body: dict = Body(default_factory=dict)) -> dict:
    """Spawn the engage worker.

    Body (optional):

      ``mode`` — engagement intensity, one of
      ``subscribe_only`` / ``like_subscribe`` / ``like_subscribe_view``
      (default) / ``complete``. See
      ``pipeline.cross_engage.burner_engage`` for what each mode does.
      Unknown values 400.

    On Cloud Run (``K_SERVICE`` set): enqueue a ``BURNER_ENGAGE`` task
    in the queue. The laptop agent leases it within seconds and runs
    the worker on the laptop (Chrome + macOS Keychain are unavailable
    on Cloud Run, so this MUST happen on a real desktop). Returns
    immediately with the task_id.

    On laptop dev (no ``K_SERVICE``): keep the legacy ``subprocess.Popen``
    path so a single-machine workflow still works end-to-end without a
    queue / agent.

    Idempotent: if a worker is already alive for this slug (per the
    GCS-backed state file's recent ``last_action_at``), return its
    state without spawning / enqueueing a duplicate.
    """
    mode = (body or {}).get("mode") or burner_engage.DEFAULT_MODE
    if mode not in burner_engage.ALL_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown mode {mode!r}; valid: "
                f"{list(burner_engage.ALL_MODES)}"
            ),
        )

    burners = {b["slug"]: b for b in burner_engage.list_burner_channels()}
    if slug not in burners:
        raise HTTPException(status_code=404, detail=f"unknown burner '{slug}'")
    if not burners[slug].get("profile_known"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"no Chrome profile mapping for '{slug}'. Add one to "
                f"~/.config/ytfactory/profile_map.json: "
                f'{{"{slug}": {{"email": "<email>"}}}}'
            ),
        )
    if burner_engage.is_running(slug):
        return {"started": False, "reason": "already_running",
                "state": burner_engage.read_state(slug)}

    # Cloud path: enqueue for the laptop agent.
    if os.environ.get("K_SERVICE"):
        # Imported lazily so laptop dev (which doesn't necessarily have
        # the Firestore queue backend wired) keeps booting fine.
        from control.core.queue import get_queue, new_task_id  # noqa: PLC0415
        from control.core.schema import TaskEnvelope, TaskKind  # noqa: PLC0415

        # Clear any stale GCS stop sentinel from a previous run so the
        # newly-spawned worker doesn't immediately self-terminate.
        try:
            burner_engage.clear_stop_sentinel(slug)
        except Exception:  # noqa: BLE001
            logger.warning("burner_engage: couldn't clear stop sentinel for %s", slug,
                           exc_info=True)

        task_id = new_task_id()
        task = TaskEnvelope(
            task_id=task_id,
            job_id=f"burner-{slug}-{int(time.time())}",
            kind=TaskKind.BURNER_ENGAGE,
            payload={"slug": slug, "mode": mode},
            # Long timeout — the worker is fire-and-forget on the laptop,
            # but we set a generous lease in case the agent code-path
            # ever switches to "block until exit".
            max_attempts=1,
        )
        get_queue().enqueue(task)
        return {
            "started": True,
            "task_id": task_id,
            "mode": mode,
            "agent_required": True,
            "hint": "queued for laptop agent — drawer will populate when worker writes its first state file",
        }

    # Laptop dev path: legacy subprocess spawn.
    WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = WORKER_LOG_DIR / f"{slug}.log"
    log_fp = log_path.open("a", buffering=1)
    log_fp.write(f"\n\n=== engage worker spawn @ {os.getpid()} (mode={mode}) ===\n")
    env = os.environ.copy()
    # Ensure the worker can import pipeline.* (this server runs with
    # PYTHONPATH=. but env may not propagate identically to subprocess
    # under uvicorn's reloader; force it).
    env["PYTHONPATH"] = str(PROJECT_ROOT) + ":" + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "pipeline.cross_engage.burner_engage", "run", slug, "--mode", mode],
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        env=env,
        # Detach: worker is independent of the API process. Closing the
        # FastAPI server should NOT kill an in-flight engage loop.
        # NOTE: stdin=DEVNULL is required on macOS Python 3.14 — without
        # it `start_new_session=True` leaves stdin pointing at the
        # FastAPI parent's TTY which is closed under uvicorn, leading
        # to "init_sys_streams: can't initialize sys standard streams"
        # on import.
        start_new_session=True,
    )
    return {
        "started": True,
        "pid": proc.pid,
        "log_path": str(log_path),
    }


@router.get("/{slug}/engage")
async def poll_engage(slug: str) -> dict:
    """Live status JSON. Returns 404 if there's no state yet (never run)."""
    state = burner_engage.read_state(slug)
    if state is None:
        raise HTTPException(status_code=404, detail="no engage state for this burner")
    state["running"] = burner_engage.is_running(slug)
    return state


@router.post("/{slug}/engage/stop")
async def stop_engage(slug: str) -> dict:
    """Request the worker to stop on its next tick (within ~2s)."""
    burner_engage.request_stop(slug)
    return {"stop_requested": True, "slug": slug}
