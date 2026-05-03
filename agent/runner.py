"""Dispatch a leased TaskEnvelope to the matching heavy worker.

Each task runs inside a per-task `TaskContext` with a fresh scratch directory
under `YTFACTORY_SCRATCH_ROOT` (default: $TMPDIR/ytfactory-scratch). The
runner deletes the scratch directory unconditionally on ack — success OR
failure OR worker exception — so the laptop disk stays bounded.

Real workers (images, tts, asr, compose, footage) get registered here as the
heavy-worker migration (task #8) ports them. For now, only `noop` is wired
so the lease protocol is testable end-to-end.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from shared.schema import TaskEnvelope, TaskKind

logger = logging.getLogger(__name__)


@dataclass
class TaskContext:
    """What every worker receives. Add cross-cutting state here, not args."""

    task: TaskEnvelope
    scratch: Path


WorkerFn = Callable[[TaskContext], Awaitable[str | None]]
"""Worker takes the context, returns the output URI (gs://... or None)."""

_REGISTRY: dict[TaskKind, WorkerFn] = {}


def register(kind: TaskKind, fn: WorkerFn) -> None:
    _REGISTRY[kind] = fn


def _scratch_root() -> Path:
    root = os.environ.get("YTFACTORY_SCRATCH_ROOT")
    if root:
        p = Path(root)
    else:
        p = Path(tempfile.gettempdir()) / "ytfactory-scratch"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _make_scratch(task: TaskEnvelope) -> Path:
    """Per-task subdir; embeds task id so cleanup logs are self-explanatory."""
    return Path(tempfile.mkdtemp(prefix=f"task-{task.task_id[:8]}-", dir=_scratch_root()))


async def run(task: TaskEnvelope) -> tuple[bool, str | None, str | None]:
    """Run the task. Returns (ok, output_uri, error). Always cleans scratch."""
    fn = _REGISTRY.get(task.kind)
    if fn is None:
        return False, None, f"no worker registered for kind={task.kind.value}"

    scratch = _make_scratch(task)
    ctx = TaskContext(task=task, scratch=scratch)
    try:
        try:
            out = await fn(ctx)
            return True, out, None
        except Exception as e:  # noqa: BLE001 — agent must never crash on a single task
            logger.exception("worker for %s raised", task.kind)
            return False, None, f"{type(e).__name__}: {e}"
    finally:
        # Cleanup is unconditional — ANY outcome triggers it.
        try:
            shutil.rmtree(scratch, ignore_errors=True)
            logger.debug("cleaned scratch %s", scratch)
        except Exception:  # noqa: BLE001 — never let cleanup mask the real outcome
            logger.warning("scratch cleanup failed for %s", scratch, exc_info=True)


# ---------------------------------------------------------------------------
# Built-in workers
# ---------------------------------------------------------------------------


async def _noop_worker(ctx: TaskContext) -> str | None:
    """Smoke worker — used by the end-to-end protocol test."""
    logger.info("noop task %s scratch=%s payload=%s", ctx.task.task_id, ctx.scratch, ctx.task.payload)
    # Touch a file in scratch so the cleanup test has something to verify.
    (ctx.scratch / "noop.touch").write_text("hi", encoding="utf-8")
    return f"noop://done/{ctx.task.task_id}"


register(TaskKind.NOOP, _noop_worker)
