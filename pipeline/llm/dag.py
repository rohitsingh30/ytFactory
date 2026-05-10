"""Stage DAG declaration for the ytFactory render pipeline.

This is the single declarative source of truth for:

- which stages exist
- what each stage depends on
- whether it fans out per-beat / per-tts-chunk or runs as a single unit
- what the pipeline runner expects each stage to produce
- which stages are LLM-driven (orchestrator runs them) vs subprocess /
  external-service (the runner just shells out)

The runner :mod:`pipeline.llm.pipeline_runner` walks this graph
topologically, parallelises any node with ``fanout != 1`` against the
configured cloud services, gates conditional edges on the upstream
output (e.g. ``upload`` only fires on ``critic.verdict == "SHIP"``),
and emits a per-attempt event per stage to the job doc so the UI's
poll loop can render a live timeline.

Adding a new stage = one entry in :data:`STAGE_DAG` + a
:class:`StageContract` (or a subprocess wrapper) registered under the
same name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Fanout = Literal[1, "beats", "tts_chunks"]
Kind = Literal["llm", "external", "compose", "upload"]


@dataclass(frozen=True)
class StageNode:
    """One node in the pipeline DAG.

    - ``deps`` are upstream stage names. A stage runs only after every
      dep has succeeded (or its conditional gate passed).
    - ``fanout`` determines how many parallel invocations the runner
      issues. ``1`` = one invocation; ``"beats"`` = one per beat in
      ``RenderJob.beats``; ``"tts_chunks"`` = one per text chunk.
    - ``kind`` selects the executor: ``"llm"`` runs through the
      orchestrator (StageContract + run_stage), ``"external"`` shells
      out / hits a Cloud Run service, ``"compose"`` runs ffmpeg,
      ``"upload"`` writes to GCS + YouTube.
    - ``conditional_on`` is set on edges that depend on a previous
      stage's output (``("critic", "SHIP")`` means "only run if
      critic's verdict is SHIP").
    - ``optional`` stages don't fail the render if they error — useful
      for AI judges that augment but don't gate.
    """
    name: str
    deps: tuple[str, ...]
    fanout: Fanout
    kind: Kind
    conditional_on: tuple[str, str] | None = None
    optional: bool = False


# The canonical Shorts pipeline. Long-form / sports-doc / kathaa each
# get their own dag.py-like module that overrides specific nodes (they
# share most stages) — Phase 6 work.
STAGE_DAG: dict[str, StageNode] = {
    "rewrite":    StageNode("rewrite",    deps=(),                   fanout=1,         kind="llm"),
    "cast":       StageNode("cast",       deps=("rewrite",),         fanout=1,         kind="llm"),
    "prompts":    StageNode("prompts",    deps=("cast",),            fanout="beats",   kind="llm"),
    "images":     StageNode("images",     deps=("prompts",),         fanout="beats",   kind="external"),
    "tts":        StageNode("tts",        deps=("rewrite",),         fanout=1,         kind="external"),
    "asr":        StageNode("asr",        deps=("tts",),             fanout=1,         kind="external"),
    "compose":    StageNode("compose",    deps=("images", "asr"),    fanout=1,         kind="compose"),
    "critic":     StageNode("critic",     deps=("compose",),         fanout=1,         kind="llm"),
    "upload":     StageNode("upload",
                            deps=("critic",),
                            fanout=1, kind="upload",
                            conditional_on=("critic", "SHIP")),
}


def topo_order(dag: dict[str, StageNode] | None = None) -> list[str]:
    """Topological sort — returns stage names in valid execution order.

    Pure stdlib; raises ``ValueError`` on cycles (which would be a
    bug in the DAG declaration). Stable across runs for deterministic
    debugging — uses dict insertion order as the secondary sort.
    """
    g = dag or STAGE_DAG
    indeg: dict[str, int] = {n: 0 for n in g}
    for node in g.values():
        for d in node.deps:
            if d not in g:
                raise ValueError(f"stage {node.name!r} depends on undefined stage {d!r}")
            indeg[node.name] += 1

    order: list[str] = []
    ready = [n for n in g if indeg[n] == 0]
    while ready:
        n = ready.pop(0)
        order.append(n)
        for child_name, child in g.items():
            if n in child.deps:
                indeg[child_name] -= 1
                if indeg[child_name] == 0:
                    ready.append(child_name)
    if len(order) != len(g):
        remaining = [n for n in g if n not in order]
        raise ValueError(f"cycle detected in STAGE_DAG involving {remaining!r}")
    return order


def stages_invalidated_by(stage: str, dag: dict[str, StageNode] | None = None) -> list[str]:
    """Return all downstream stages that depend on ``stage`` (transitively).

    Used by the cache-invalidation cascade: when the critic feeds back
    a Fix that re-runs ``prompts``, every stage downstream of prompts
    (images → compose → critic) needs to re-run too. The runner uses
    this to know which cache entries to evict.
    """
    g = dag or STAGE_DAG
    out: list[str] = []
    seen: set[str] = set()
    queue = [stage]
    while queue:
        s = queue.pop(0)
        for child_name, child in g.items():
            if s in child.deps and child_name not in seen:
                seen.add(child_name)
                out.append(child_name)
                queue.append(child_name)
    return out
