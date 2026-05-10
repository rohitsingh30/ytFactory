"""Burner-channel endpoints — drive the /app/burner-channels page.

GET  /api/burner_channels                  list of burners + counts
GET  /api/burner_channels/catalog          live catalog the burner will engage with
POST /api/burner_channels/{slug}/engage    kick off worker (subprocess)
GET  /api/burner_channels/{slug}/engage    poll status JSON
POST /api/burner_channels/{slug}/engage/stop  request graceful stop

The actual worker lives in `pipeline.burner_engage` and runs as a
spawned subprocess so the API stays snappy and a worker crash never
kills the FastAPI process.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

from pipeline.cross_engage import burner_engage
from pipeline.utils import catalog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/burner_channels")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WORKER_LOG_DIR = PROJECT_ROOT / "data" / "burner_engage" / "logs"


@router.get("")
async def list_burners() -> dict:
    burners = burner_engage.list_burner_channels()
    # Hydrate live status (running / stopped / never-run)
    catalog_size = catalog.catalog_count()
    out = []
    for b in burners:
        st = burner_engage.read_state(b["slug"])
        out.append({
            **b,
            "running": burner_engage.is_running(b["slug"]),
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
async def start_engage(slug: str) -> dict:
    """Spawn the engage worker as a subprocess.

    Idempotent: if a worker is already alive for this slug, return its
    state without spawning a duplicate.
    """
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

    WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = WORKER_LOG_DIR / f"{slug}.log"
    log_fp = log_path.open("a", buffering=1)
    log_fp.write(f"\n\n=== engage worker spawn @ {os.getpid()} ===\n")
    env = os.environ.copy()
    # Ensure the worker can import pipeline.* (this server runs with
    # PYTHONPATH=. but env may not propagate identically to subprocess
    # under uvicorn's reloader; force it).
    env["PYTHONPATH"] = str(PROJECT_ROOT) + ":" + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "pipeline.cross_engage.burner_engage", "run", slug],
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
