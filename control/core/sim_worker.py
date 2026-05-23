"""In-process simulated render worker — Layer 1 of the cloud-native plan.

Why this exists
---------------
The real render path is moving to a Cloud Run Job (Layer 2). Until that
ships, AND for fast UX iteration after, we need a worker that:

- picks RENDER_SHORT tasks off the queue
- advances the matching job through the canonical 7 stages
  (rewrite → cast → images → tts → asr → compose → upload)
- writes timeline events the UI can stream
- drops a real-but-placeholder mp4 the UI can <video src=...>

Activated by ``YTFACTORY_SIM_WORKER=1`` (default ON for local dev, OFF
in production). When OFF, the queue waits for a real render backend.

Implementation notes
--------------------
- Stage timing is intentionally short (~2-5s) so demos feel alive but
  don't waste minutes per click.
- The placeholder mp4 is generated once per process via ffmpeg into a
  cache dir, then reused.
- This module is import-safe: nothing happens at import time. Call
  ``start_sim_worker(app)`` from the FastAPI startup hook.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from control.core import jobs as jobs_mod
from control.core.queue import InMemoryQueue, get_queue
from control.core.schema import TaskKind, TaskStatus
from pipeline.observability.event_helpers import safe_track as _track

logger = logging.getLogger(__name__)


SIM_ENV = "YTFACTORY_SIM_WORKER"
SIM_SPEED_ENV = "YTFACTORY_SIM_SPEED"  # multiplier: lower = faster

CACHE_DIR = Path(os.environ.get("YTFACTORY_SIM_CACHE", tempfile.gettempdir())) / "ytfactory-sim"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
PLACEHOLDER_MP4 = CACHE_DIR / "placeholder_short.mp4"
PLACEHOLDER_THUMB = CACHE_DIR / "placeholder_thumb.jpg"


# Canonical pipeline stages. The UI stage timeline mirrors this exactly.
STAGES: list[tuple[str, str]] = [
    ("rewrite", "Rewriting script"),
    ("cast", "Casting voice & visuals"),
    ("images", "Generating images"),
    ("tts", "Synthesizing narration"),
    ("asr", "Aligning captions"),
    ("compose", "Composing video"),
    ("upload", "Uploading to GCS"),
]


def is_enabled() -> bool:
    return os.environ.get(SIM_ENV, "1").lower() in {"1", "true", "yes", "on"}


def speed_multiplier() -> float:
    """Per-stage seconds get multiplied by this. 1.0 = normal demo speed."""
    try:
        return float(os.environ.get(SIM_SPEED_ENV, "1.0"))
    except ValueError:
        return 1.0


# ---------------------------------------------------------------------------
# Placeholder asset generation
# ---------------------------------------------------------------------------


def ensure_placeholder_assets() -> None:
    """Generate (once) a 9:16 placeholder mp4 + thumbnail for sim renders."""
    if PLACEHOLDER_MP4.exists() and PLACEHOLDER_THUMB.exists():
        return
    if shutil.which("ffmpeg") is None:
        logger.warning("ffmpeg not on PATH — sim worker will skip placeholder mp4")
        return
    try:
        # 8s 9:16 dark loop with a centered "ytFactory · sim render" caption.
        # Uses the lavfi color source + drawtext overlay; no fonts dir needed
        # if the system font path is discoverable. Keep it simple — if drawtext
        # fails (no font), fall back to plain color.
        font = _find_font()
        common = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=#0a0a0a:s=1080x1920:d=8:r=30",
        ]
        if font:
            vf = (
                f"drawtext=fontfile='{font}':text='ytFactory · sim render':"
                "fontcolor=#fafafa:fontsize=58:x=(w-text_w)/2:y=(h-text_h)/2,"
                f"drawtext=fontfile='{font}':text='Layer 1 placeholder mp4':"
                "fontcolor=#777777:fontsize=34:x=(w-text_w)/2:y=(h-text_h)/2+90"
            )
            cmd = common + ["-vf", vf, "-pix_fmt", "yuv420p", str(PLACEHOLDER_MP4)]
        else:
            cmd = common + ["-pix_fmt", "yuv420p", str(PLACEHOLDER_MP4)]
        subprocess.run(cmd, check=True, capture_output=True)
        # Thumbnail: a single frame from the mp4.
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(PLACEHOLDER_MP4),
                "-frames:v", "1", "-q:v", "3",
                str(PLACEHOLDER_THUMB),
            ],
            check=True, capture_output=True,
        )
        logger.info("sim placeholder mp4 cached at %s (%d bytes)",
                    PLACEHOLDER_MP4, PLACEHOLDER_MP4.stat().st_size)
    except subprocess.CalledProcessError as e:
        logger.warning("ffmpeg failed: %s", (e.stderr or b"").decode("utf8", "ignore"))
    except Exception:  # noqa: BLE001
        logger.warning("placeholder generation failed", exc_info=True)


def _find_font() -> Optional[str]:
    candidates = [
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


# ---------------------------------------------------------------------------
# Stage advancement
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_timeline() -> list[dict]:
    return [{"stage": k, "label": label, "status": "pending"} for k, label in STAGES]


def _set_stage(timeline: list[dict], key: str, status: str, msg: str | None = None) -> list[dict]:
    out = [dict(s) for s in timeline]
    for s in out:
        if s["stage"] == key:
            s["status"] = status
            s["ts"] = _utcnow_iso()
            if msg:
                s["msg"] = msg
    return out


def _track_sim_stage(
    event: str,
    *,
    job_id: str,
    stage: str,
    duration_ms: int | None = None,
    success: bool = True,
    metadata: dict | None = None,
) -> None:
    payload = {
        "stage": stage,
        "mode": "sim",
        **(metadata or {}),
    }
    _track(
        event,
        category="render",
        success=success,
        duration_ms=duration_ms,
        job_id=job_id,
        metadata=payload,
    )


def _advance_one_job(task) -> None:  # task: TaskEnvelope
    """Walk a single job through all stages. Synchronous; called inside the
    worker loop's executor so the event loop stays responsive."""
    job_id = task.job_id
    speed = speed_multiplier()
    timeline = _empty_timeline()

    jobs_mod.mark_stage(job_id, status="rendering", stage="rewrite", timeline=timeline)

    for key, _ in STAGES:
        stage_t0 = time.perf_counter()
        _track_sim_stage("stage.start", job_id=job_id, stage=key)
        timeline = _set_stage(timeline, key, "running", "sim · running")
        jobs_mod.mark_stage(
            job_id,
            status="rendering" if key != "upload" else "uploading",
            stage=key,
            timeline=timeline,
        )
        # Variable per-stage timing so the UI feels alive.
        per_stage_s = random.uniform(1.4, 3.0) * speed
        time.sleep(per_stage_s)
        duration_ms = int((time.perf_counter() - stage_t0) * 1000)
        timeline = _set_stage(timeline, key, "done")
        jobs_mod.mark_stage(
            job_id,
            status="rendering" if key != "upload" else "uploading",
            stage=key,
            timeline=timeline,
        )
        _track_sim_stage("stage.end", job_id=job_id, stage=key, duration_ms=duration_ms)

    # Done: surface the placeholder mp4 via the preview proxy.
    fields = {
        "timeline": timeline,
        "critique": {
            "verdict": "SHIP",
            "weakest_param": "(sim render — no real critic ran)",
            "notes": "Simulated render. Real critic runs on the cloud worker.",
        },
        "preview_local_path": str(PLACEHOLDER_MP4) if PLACEHOLDER_MP4.exists() else None,
    }
    jobs_mod.mark_done(
        job_id,
        short_uri=f"sim://{job_id}/short.mp4",
        thumb_uri=str(PLACEHOLDER_THUMB) if PLACEHOLDER_THUMB.exists() else None,
    )
    jobs_mod.get_jobs().update(job_id, **fields)
    logger.info("sim render complete job=%s", job_id)


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


_STOP_EVENT: threading.Event | None = None
_THREAD: threading.Thread | None = None


def _claim_one_render_task() -> Optional[object]:
    """Atomically claim a queued RENDER_SHORT task by flipping it to LEASED.

    Works against the InMemoryQueue (the dev backend). For the Firestore
    queue we rely on the existing lease() API instead.
    """
    q = get_queue()
    if isinstance(q, InMemoryQueue):
        with q._lock:  # type: ignore[attr-defined]
            for t in sorted(
                q._tasks.values(), key=lambda t: t.created_at  # type: ignore[attr-defined]
            ):
                if t.status == TaskStatus.QUEUED and t.kind == TaskKind.RENDER_SHORT:
                    t.status = TaskStatus.LEASED
                    t.lease_owner = "sim-worker"
                    return t
        return None
    # Firestore queue: defer to its lease() API.
    return q.lease("sim-worker", [TaskKind.RENDER_SHORT], ttl_s=600)


def _ack_one(task, *, ok: bool, error: str | None = None) -> None:
    q = get_queue()
    if isinstance(q, InMemoryQueue):
        with q._lock:  # type: ignore[attr-defined]
            t = q._tasks.get(task.task_id)  # type: ignore[attr-defined]
            if t is not None:
                t.status = TaskStatus.DONE if ok else TaskStatus.FAILED
                t.error = error
        return
    q.ack(task.task_id, "sim-worker", ok=ok, output_uri=None, error=error)


def _worker_loop(stop: threading.Event, poll_interval_s: float = 0.6) -> None:
    logger.info("sim worker loop started (speed=%.2fx)", speed_multiplier())
    while not stop.is_set():
        try:
            task = _claim_one_render_task()
        except Exception:  # noqa: BLE001
            logger.warning("sim worker claim failed", exc_info=True)
            task = None
        if task is None:
            if stop.wait(timeout=poll_interval_s):
                break
            continue
        try:
            _advance_one_job(task)
            _ack_one(task, ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("sim render failed job=%s err=%s", task.job_id, e, exc_info=True)
            jobs_mod.mark_failed(task.job_id, stage="sim", error=str(e))
            _ack_one(task, ok=False, error=str(e))
    logger.info("sim worker loop stopped")


def start_sim_worker(force: bool = False) -> bool:
    """Start the background thread. Idempotent. Returns True if started."""
    global _STOP_EVENT, _THREAD
    if not (force or is_enabled()):
        logger.info("sim worker disabled (%s != 1)", SIM_ENV)
        return False
    if _THREAD is not None and _THREAD.is_alive():
        return True
    ensure_placeholder_assets()
    _STOP_EVENT = threading.Event()
    _THREAD = threading.Thread(target=_worker_loop, args=(_STOP_EVENT,), daemon=True, name="sim-worker")
    _THREAD.start()
    logger.info("sim worker started — every QUEUED RENDER_SHORT task will auto-render")
    return True


def stop_sim_worker(timeout_s: float = 2.0) -> None:
    global _STOP_EVENT, _THREAD
    if _STOP_EVENT is not None:
        _STOP_EVENT.set()
    if _THREAD is not None:
        _THREAD.join(timeout=timeout_s)
    _THREAD = None
    _STOP_EVENT = None


def status() -> dict:
    return {
        "enabled": is_enabled(),
        "running": _THREAD is not None and _THREAD.is_alive(),
        "speed_multiplier": speed_multiplier(),
        "placeholder_mp4": str(PLACEHOLDER_MP4),
        "placeholder_exists": PLACEHOLDER_MP4.exists(),
        "stages": [k for k, _ in STAGES],
    }
