"""Dispatch a leased TaskEnvelope to the matching heavy worker.

Real workers (images, tts, asr, compose, footage) get registered here as the
heavy-worker migration (task #8) ports them. For now, only `noop` is wired
so the lease protocol is testable end-to-end.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from shared.schema import TaskEnvelope, TaskKind

logger = logging.getLogger(__name__)

WorkerFn = Callable[[TaskEnvelope], Awaitable[str | None]]
"""Worker takes the envelope, returns the output URI (gs://... or None)."""

_REGISTRY: dict[TaskKind, WorkerFn] = {}


def register(kind: TaskKind, fn: WorkerFn) -> None:
    _REGISTRY[kind] = fn


async def run(task: TaskEnvelope) -> tuple[bool, str | None, str | None]:
    """Run the task. Returns (ok, output_uri, error)."""
    fn = _REGISTRY.get(task.kind)
    if fn is None:
        return False, None, f"no worker registered for kind={task.kind.value}"
    try:
        out = await fn(task)
        return True, out, None
    except Exception as e:  # noqa: BLE001 — agent must never crash on a single task
        logger.exception("worker for %s raised", task.kind)
        return False, None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Built-in workers
# ---------------------------------------------------------------------------


async def _noop_worker(task: TaskEnvelope) -> str | None:
    """Smoke worker — used by the end-to-end protocol test."""
    logger.info("noop task %s payload=%s", task.task_id, task.payload)
    return f"noop://done/{task.task_id}"


register(TaskKind.NOOP, _noop_worker)
