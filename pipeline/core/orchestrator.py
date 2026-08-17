"""Orchestrator — runs an ordered list of Stages against one Context.

This replaces the ~3,000-line monolithic worker: the worker just builds the Context,
picks the stage list for the spec, and calls ``run``. Each stage's telemetry envelope
(start/end/failed + artifact dump) is applied here, uniformly, once.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.core.context import Context
from pipeline.core.stage import Stage


@dataclass
class RenderResult:
    ok: bool
    stages_run: list[str] = field(default_factory=list)
    error: str | None = None


class Orchestrator:
    """Sequences stages. Fail-loud: the first stage that raises aborts the render —
    no placeholder artifacts, the exception propagates with the stage name."""

    def run(self, stages: list[Stage], ctx: Context) -> RenderResult:
        ran: list[str] = []
        for stage in stages:
            with ctx.telemetry.stage(stage.name):
                stage.run(ctx)
            ran.append(stage.name)
        return RenderResult(ok=True, stages_run=ran)
