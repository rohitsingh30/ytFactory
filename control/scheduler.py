"""Round-robin auto-scheduler for 24/7 Short production.

Every tick:
  1. Bail if ANY heavy task (RENDER_SHORT etc.) is queued or leased — only
     one short renders at a time so the M2 Max GPU never gets a second
     mflux/Z-Image-Turbo session and triggers a Metal OOM.
     (See historyrecapped/learnings/… and the broader
     feedback_gpu_one_render_at_a_time rule.)
  2. Round-robin across the 5 channels: pick the next channel after the
     last one we enqueued for. If that channel has no pending work, try
     the next, until we either find work or run out of channels.
  3. "Pending work" = a narration JSON with no matching upload record.
     Looks at both top-level (`<channel>/narrations/<slug>.json`) AND
     niche-nested (`<channel>/<niche>/narrations/<slug>.json`) layouts.
  4. On match, enqueue a RENDER_SHORT task via the same path the chat
     uses (control.chat_routes._enqueue_render_job). The agent on the
     laptop leases it, runs make_shorts.py, and ships to YouTube.

State (last_channel, last_slug, last_enqueued_at) persists in Firestore
collection ``scheduler_state`` (or in-process memory for the dev backend).

Cloud Scheduler triggers POST /api/scheduler/tick every 30 min.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from control.chat_routes import _enqueue_render_job
from control.queue import get_queue
from control.schema import HEAVY_KINDS, ShortProposal, TaskStatus

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEDULER_STATE_DOC = "scheduler_state/main"

# Stable order for round-robin. Channels not in this list never get
# auto-scheduled even if they have a backlog.
CHANNEL_ROTATION: list[str] = [
    "historyrecapped",
    "hindutavaanimated",
    "mystoriesanimated",
    "rhymetimejunction",
    "sportstoriesanimated",
]

logger = logging.getLogger(__name__)

_MEM_STATE: dict = {}


def _backend() -> str:
    return os.environ.get("YTFACTORY_QUEUE_BACKEND", "firestore")


# -------- in-flight check ----------------------------------------------------

def _has_in_flight_heavy() -> bool:
    """True if any HEAVY task (queued or leased) exists in the queue."""
    if _backend() == "memory":
        q = get_queue()
        # _tasks is private but the in-memory backend exposes it; this
        # only runs in dev/tests.
        for t in getattr(q, "_tasks", {}).values():
            if t.kind in HEAVY_KINDS and t.status in (TaskStatus.QUEUED, TaskStatus.LEASED):
                return True
        return False

    from google.cloud import firestore  # noqa: PLC0415
    client = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod"))
    col = client.collection("tasks")
    heavy_kind_values = [k.value for k in HEAVY_KINDS]
    # Two queries (one per status) — Firestore doesn't combine `in` filters
    # on different fields cheaply, and a single status `in` query with a
    # short list is fine.
    for status_val in (TaskStatus.QUEUED.value, TaskStatus.LEASED.value):
        q = (col.where("status", "==", status_val)
                .where("kind", "in", heavy_kind_values)
                .limit(1))
        if any(True for _ in q.stream()):
            return True
    return False


# -------- backlog walk -------------------------------------------------------

def _uploaded_slugs(channel_dir: Path) -> set[str]:
    out: set[str] = set()
    uploads = channel_dir / "uploads"
    if not uploads.exists():
        return out
    for f in uploads.rglob("*.json"):
        out.add(f.stem)
    return out


def _next_unrendered(channel: str) -> Optional[Tuple[str, Path]]:
    """Oldest unrendered narration for ``channel``, or None.

    Returns (slug, narration_path). "Unrendered" = the narration JSON
    exists but no upload record under <channel>/uploads/**/<slug>.json.
    Looks at <channel>/narrations/ AND <channel>/<niche>/narrations/.
    """
    chan_dir = PROJECT_ROOT / channel
    if not chan_dir.exists():
        return None

    uploaded = _uploaded_slugs(chan_dir)

    # Top-level + niche-nested narration buckets.
    candidates: list[tuple[str, Path]] = []
    top_narr = chan_dir / "narrations"
    if top_narr.exists():
        for f in top_narr.glob("*.json"):
            if f.stem not in uploaded:
                candidates.append((f.stem, f))
    for niche_narr in chan_dir.glob("*/narrations/*.json"):
        if niche_narr.stem not in uploaded:
            candidates.append((niche_narr.stem, niche_narr))

    if not candidates:
        return None
    # Oldest first (FIFO = curator submitted first goes first).
    candidates.sort(key=lambda x: x[1].stat().st_mtime)
    return candidates[0]


# -------- state I/O ----------------------------------------------------------

def _read_state() -> dict:
    if _backend() == "memory":
        return dict(_MEM_STATE)
    from google.cloud import firestore  # noqa: PLC0415
    client = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod"))
    snap = client.collection("scheduler_state").document("main").get()
    return (snap.to_dict() or {}) if snap.exists else {}


def _save_state(state: dict) -> None:
    if _backend() == "memory":
        _MEM_STATE.clear()
        _MEM_STATE.update(state)
        return
    from google.cloud import firestore  # noqa: PLC0415
    client = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "ytfactory-prod"))
    client.collection("scheduler_state").document("main").set(state, merge=True)


# -------- public API ---------------------------------------------------------

def tick() -> dict:
    """Run one scheduler tick. Returns a small dict describing what
    happened — surfaced in the /api/scheduler/tick HTTP response."""
    if _has_in_flight_heavy():
        return {
            "action": "skipped",
            "reason": "heavy_task_in_flight",
            "explain": "Wait for the current render to finish; only one heavy task at a time so the GPU doesn't OOM.",
        }

    state = _read_state()
    last_channel = state.get("last_channel", "")
    if last_channel in CHANNEL_ROTATION:
        start = (CHANNEL_ROTATION.index(last_channel) + 1) % len(CHANNEL_ROTATION)
    else:
        start = 0

    tried: list[str] = []
    for offset in range(len(CHANNEL_ROTATION)):
        ch = CHANNEL_ROTATION[(start + offset) % len(CHANNEL_ROTATION)]
        tried.append(ch)
        work = _next_unrendered(ch)
        if work is None:
            continue
        slug, narr_path = work
        logger.info("scheduler enqueueing channel=%s slug=%s", ch, slug)
        proposal = ShortProposal(
            channel=ch,
            format="auto",
            topic=slug,
            source_kind="manual_backlog",
            source_ref=str(narr_path.relative_to(PROJECT_ROOT)),
            length_s=55,
            notes="auto-scheduled from narration backlog",
        )
        resp = _enqueue_render_job(proposal)
        state["last_channel"] = ch
        state["last_slug"] = slug
        state["last_enqueued_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
        return {
            "action": "enqueued",
            "channel": ch,
            "slug": slug,
            "job_id": resp.job_id,
            "task_id": resp.task_id,
            "tried": tried,
        }

    return {
        "action": "skipped",
        "reason": "no_unrendered_scripts",
        "explain": "No channel has a narration without a matching upload record. Author more scripts.",
        "tried": tried,
    }
