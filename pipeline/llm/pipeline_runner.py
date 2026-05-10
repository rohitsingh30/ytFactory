"""Pipeline runner — the central orchestrator that walks the STAGE_DAG.

This is the brain. It:

1. Walks :data:`pipeline.llm.dag.STAGE_DAG` topologically.
2. For each stage, builds a :class:`StageContext` from the
   :class:`RenderJob`'s upstream artifacts.
3. Hits the per-artifact cache (Bazel-style content addressing) — on
   hit, skips execution and reuses the prior output.
4. On miss, dispatches to the right executor:
     - ``kind="llm"``      → :func:`pipeline.llm.orchestrator.run_stage`
       which auto-retries on validator failures up to
       ``YTFACTORY_LLM_MAX_RETRIES``.
     - ``kind="external"`` → calls a registered handler (e.g. images
       fans out per beat against the cloud image service).
     - ``kind="compose"``  → ffmpeg.
     - ``kind="upload"``   → GCS + YouTube Data API.
5. Writes the output back into the :class:`RenderJob`'s slot.
6. After the critic stage runs, if its verdict is ``FIX``, cascades
   the Fix list through :func:`pipeline.llm.fix_router.cascade`,
   invalidates ONLY the affected cache keys, and re-runs the
   downstream stages — bounded by ``YTFACTORY_CRITIC_MAX_PASSES``.

The runner is small on purpose. The complexity lives in
StageContracts (per-stage prompt + validation), validators (rule
definitions), and external-stage handlers (image / TTS clients).

Wiring into the cloud render-worker
-----------------------------------
``cloud/render-worker-v2/entrypoint.py``'s real-mode handler grows a
single call: ``run_pipeline(render_job)``. Everything else — the
linear stage loop, the per-stage timeline events, the GCS upload —
moves into this module + its registered handlers.

The first ship wires only the LLM stages (rewrite via RewriteContract;
cast / prompts / critic land as their contracts ship). External stages
fall through to the existing renderer subprocess until each is ported
in turn.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable

from . import cache as cache_mod
from . import fix_router
from .contracts import RewriteContract
from .contracts.critic_contract import CriticContract
from .dag import STAGE_DAG, StageNode, stages_invalidated_by, topo_order
from .fix import Fix, errors as fix_errors
from .orchestrator import (
    OrchestratorError,
    StageContext,
    StageResult,
    run_stage,
)
from .render_job import BeatArtifact, CriticVerdict, RenderJob

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage handler registries
# ---------------------------------------------------------------------------


# LLM stages → contract instance. Add a new contract here to make it
# pickup-able by the runner. The runner instantiates its StageContext
# from RenderJob.upstream_for(stage_name).
LLM_CONTRACTS = {
    "rewrite": RewriteContract(),
    "critic":  CriticContract(),
    # cast + prompts contracts land in Phase 3 — until then those stages
    # fall through to the legacy subprocess renderer.
}


# External stage handlers. Each takes ``(job, sub_index_or_None)`` and
# returns the artifact dict to merge into the RenderJob (the runner
# decides where to write it via ``_persist_stage_output``). Stages
# with fanout="beats" are called once per beat with the index.
ExternalHandler = Callable[[RenderJob, int | None], dict]
EXTERNAL_HANDLERS: dict[str, ExternalHandler] = {
    # populated as image / tts / asr / compose / upload handlers ship
}


def register_external(stage: str, handler: ExternalHandler) -> None:
    """Register an external-stage handler (idempotent)."""
    EXTERNAL_HANDLERS[stage] = handler


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """Aggregate result of one pipeline run."""
    job: RenderJob
    stage_results: dict[str, StageResult] = field(default_factory=dict)
    cache_hits: dict[str, int] = field(default_factory=dict)
    critic_passes: int = 0
    final_verdict: str = ""        # "SHIP" | "FIX" | "BLOCK" | "" (no critic ran)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


_DEFAULT_MAX_CRITIC_PASSES = int(os.environ.get("YTFACTORY_CRITIC_MAX_PASSES", "2"))


def run_pipeline(
    job: RenderJob,
    *,
    dag: dict[str, StageNode] | None = None,
    cache: cache_mod.CacheBackend | None = None,
    max_critic_passes: int | None = None,
    skip_external: bool = False,
) -> PipelineResult:
    """Walk the stage DAG end-to-end.

    Args:
        job: The :class:`RenderJob` (mutated in-place as artifacts land).
        dag: Override :data:`STAGE_DAG` for tests.
        cache: Override the default cache backend.
        max_critic_passes: Override
            ``YTFACTORY_CRITIC_MAX_PASSES`` (default 2). A pass is one
            full critic-verdict + (if FIX) cascade + re-run + re-critic.
        skip_external: When True, external stages (images, TTS, ASR,
            compose, upload) are skipped — the runner only drives LLM
            contracts. Lets tests exercise the LLM half without
            touching cloud services.

    Returns:
        :class:`PipelineResult` aggregating stage outputs, cache stats,
        and the final critic verdict.
    """
    g = dag or STAGE_DAG
    cb = cache or cache_mod.get_default_cache()
    crit_cap = (max_critic_passes if max_critic_passes is not None
                else _DEFAULT_MAX_CRITIC_PASSES)

    result = PipelineResult(job=job)

    # Topo order; the cascade after FIX may re-run a subset of these.
    order = topo_order(g)
    pending: set[str] = set(order)
    completed: set[str] = set()
    skipped: set[str] = set()

    while pending:
        # A stage is ready when every dep is in completed (NOT skipped)
        # AND its conditional gate (if any) opens. Skipped deps make
        # downstream stages also skip.
        ready: list[str] = []
        new_skipped: list[str] = []
        for s in list(pending):
            deps = g[s].deps
            if any(d in skipped for d in deps):
                new_skipped.append(s)
                continue
            if all(d in completed for d in deps) and _gate_open(g[s], job):
                ready.append(s)

        # Apply skips for stages whose deps are skipped.
        for s in new_skipped:
            logger.info("[pipeline] skipping %s — upstream skipped", s)
            skipped.add(s)
            pending.discard(s)

        if not ready:
            # Either every remaining stage's gate is closed, or DAG stalled.
            unsatisfied_but_deps_met = [
                s for s in pending
                if all(d in completed for d in g[s].deps)
                and not _gate_open(g[s], job)
            ]
            for s in unsatisfied_but_deps_met:
                logger.info("[pipeline] skipping %s — conditional gate closed", s)
                skipped.add(s)
                pending.discard(s)
            if pending:
                logger.warning("[pipeline] stalled; remaining=%s", sorted(pending))
            break

        stage_name = ready[0]
        node = g[stage_name]
        try:
            executed = _run_one_stage(
                node, job, cb, result, skip_external=skip_external,
            )
        except OrchestratorError as e:
            logger.error("[pipeline] stage %s exhausted retries: %s",
                         stage_name, [f.constraint for f in e.last_failures])
            raise

        if executed:
            completed.add(stage_name)
        else:
            skipped.add(stage_name)
            logger.info("[pipeline] stage %s skipped (no handler / contract / external-disabled)",
                        stage_name)
        pending.discard(stage_name)

        # Critic stage just landed → consider FIX cascade.
        if (executed and stage_name == "critic"
                and result.critic_passes < crit_cap):
            verdict = job.critic_verdict
            if verdict and verdict.verdict == "FIX" and verdict.fixes:
                _cascade_fixes(verdict.fixes, job, g, cb, result,
                               pending, completed, skipped)
                result.critic_passes += 1
                continue   # pending now contains downstream stages — re-walk

    result.final_verdict = (
        job.critic_verdict.verdict if job.critic_verdict else ""
    )
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gate_open(node: StageNode, job: RenderJob) -> bool:
    """True iff this stage's conditional_on (if any) is satisfied."""
    if node.conditional_on is None:
        return True
    upstream_stage, expected = node.conditional_on
    if upstream_stage == "critic":
        return bool(job.critic_verdict and job.critic_verdict.verdict == expected)
    # Extend as needed.
    return True


def _run_one_stage(
    node: StageNode,
    job: RenderJob,
    cache: cache_mod.CacheBackend,
    result: PipelineResult,
    *,
    skip_external: bool,
) -> bool:
    """Dispatch a single stage to the right executor.

    Returns True if the stage executed (or hit the cache); False if
    it was skipped (no handler / no contract / external-disabled). The
    caller uses this to decide whether to mark the stage completed
    (downstream stages can run) or skipped (downstream stages also
    skip).

    Cache check happens first (read-through); on miss we execute and
    write back. fanout != 1 stages loop over sub_indexes.
    """
    sub_indices: list[int | None]
    if node.fanout == "beats":
        sub_indices = list(range(len(job.beats))) if job.beats else []
        if not sub_indices:
            logger.info("[pipeline] stage %s skipped — no beats yet", node.name)
            return False
    elif node.fanout == "tts_chunks":
        # Future: split narration into chunks for parallel TTS. For now
        # treat as a single invocation.
        sub_indices = [None]
    else:
        sub_indices = [None]

    any_executed = False
    for sub in sub_indices:
        key = cache_mod.key_for(
            stage=node.name,
            channel=job.channel,
            channel_cfg=job.channel_cfg,
            upstream=job.upstream_for(node.name),
            sub_index=sub,
        )
        hit = cache.get(key)
        if hit is not None:
            logger.info("[pipeline] cache HIT stage=%s sub=%s key=%s",
                        node.name, sub, key)
            result.cache_hits[node.name] = result.cache_hits.get(node.name, 0) + 1
            _persist_stage_output(node.name, hit.output, job, sub)
            any_executed = True
            continue

        if node.kind == "llm":
            contract = LLM_CONTRACTS.get(node.name)
            if contract is None:
                logger.info("[pipeline] no LLM contract for %s — skipping (legacy path will handle)",
                            node.name)
                continue   # this sub_index unhandled
            ctx = StageContext(
                channel=job.channel,
                channel_cfg=job.channel_cfg,
                raw_input=job.raw_input,
                upstream=job.upstream_for(node.name),
                sub_index=sub,
            )
            stage_result = run_stage(contract, ctx)
            result.stage_results[node.name] = stage_result
            _persist_stage_output(node.name, stage_result.output, job, sub)
            cache.put(key, cache_mod.CacheRecord(output=stage_result.output))
            job.attempts_per_stage[node.name] = stage_result.attempts
            any_executed = True
        elif node.kind in ("external", "compose", "upload"):
            if skip_external:
                logger.info("[pipeline] skipping external stage %s (skip_external=True)",
                            node.name)
                continue
            handler = EXTERNAL_HANDLERS.get(node.name)
            if handler is None:
                logger.info("[pipeline] no handler for external stage %s — leaving to legacy path",
                            node.name)
                continue
            output = handler(job, sub)
            _persist_stage_output(node.name, output, job, sub)
            cache.put(key, cache_mod.CacheRecord(output=output))
            any_executed = True
        else:
            raise ValueError(f"unknown stage kind: {node.kind!r}")

    return any_executed


def _persist_stage_output(
    stage: str, output: dict, job: RenderJob, sub: int | None,
) -> None:
    """Merge a stage's output back into the RenderJob.

    The "where it goes" mapping mirrors the StageNode — keep them in
    sync. New stages add their own entry here.
    """
    if stage == "rewrite":
        job.script = output
        # Materialise beats so subsequent stages have something to fan out on.
        narration = (output.get("narration") or "").strip()
        sentences = [s.strip() for s in _split_sentences(narration) if s.strip()]
        job.beats = [
            BeatArtifact(index=i, text=s) for i, s in enumerate(sentences)
        ]
    elif stage == "cast":
        job.cast = output
    elif stage == "prompts":
        if sub is not None and 0 <= sub < len(job.beats):
            job.beats[sub].prompt = output.get("prompt") or output.get("text")
    elif stage == "images":
        if sub is not None and 0 <= sub < len(job.beats):
            job.beats[sub].image_uri = output.get("image_uri")
            job.beats[sub].image_local_path = output.get("local_path")
    elif stage == "tts":
        job.audio_uri = output.get("audio_uri")
        job.audio_local_path = output.get("local_path")
    elif stage == "asr":
        job.captions = output
    elif stage == "compose":
        job.mp4_uri = output.get("mp4_uri")
        job.thumb_uri = output.get("thumb_uri")
    elif stage == "critic":
        job.critic_verdict = CriticVerdict(
            verdict=output.get("verdict", ""),
            weakest_param=output.get("weakest_param", ""),
            fixes=[Fix(**f) for f in (output.get("fixes") or [])],
            raw=output,
        )
    elif stage == "upload":
        # Upload handlers can persist youtube_url in output["youtube_url"];
        # downstream stages can read it from the RenderJob if needed.
        pass


def _cascade_fixes(
    fixes: list,
    job: RenderJob,
    dag: dict[str, StageNode],
    cache: cache_mod.CacheBackend,
    result: PipelineResult,
    pending: set[str],
    completed: set[str],
    skipped: set[str],
) -> None:
    """Group fixes by owning stage, invalidate cache, requeue stages.

    Each Fix targets a stage (and optionally a sub_index). The cascade:
      1. Groups by (stage, sub_index).
      2. Deletes the cache entry at that key — forcing re-execution
         even when the upstream hash hasn't changed.
      3. Adds the stage back to ``pending`` and removes it from
         ``completed``.
      4. Same for every transitively-downstream stage so the change
         propagates (a re-rewrite invalidates cast/prompts/images/…).
    """
    grouped = fix_router.cascade(fixes)
    job.fixes_applied.extend(fixes)

    for stage, by_sub in grouped.items():
        if stage == "_unrouted":
            for sub_fixes in by_sub.values():
                for f in sub_fixes:
                    logger.warning("[pipeline] unrouted fix: constraint=%s reason=%s",
                                   f.constraint, f.reason)
            continue

        if stage not in dag:
            logger.warning("[pipeline] fix routed to unknown stage=%s — ignored", stage)
            continue

        for sub, sub_fixes in by_sub.items():
            key = cache_mod.key_for(
                stage=stage,
                channel=job.channel,
                channel_cfg=job.channel_cfg,
                upstream=job.upstream_for(stage),
                sub_index=sub,
            )
            cache.delete(key)
            logger.info("[pipeline] FIX cascade — invalidated cache + re-queueing "
                        "stage=%s sub=%s (reasons: %s)",
                        stage, sub, [f.constraint for f in sub_fixes])

        pending.add(stage)
        completed.discard(stage)
        skipped.discard(stage)

        # Every stage downstream of `stage` also re-runs. Invalidate
        # their cache keys too so they don't silently re-use stale
        # outputs.
        for d in stages_invalidated_by(stage, dag):
            d_key = cache_mod.key_for(
                stage=d,
                channel=job.channel,
                channel_cfg=job.channel_cfg,
                upstream=job.upstream_for(d),
                sub_index=None,
            )
            cache.delete(d_key)
            pending.add(d)
            completed.discard(d)
            skipped.discard(d)


def _split_sentences(text: str) -> list[str]:
    """Cheap sentence split for materialising beats. Mirrors the
    renderer's pipeline.beats logic — same number of beats per
    narration, by construction.
    """
    import re
    return re.split(r"(?<=[.!?])\s+", text.strip())
