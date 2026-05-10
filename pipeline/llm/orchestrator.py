"""Constraint-aware LLM stage runner with auto-retry on validation failure.

Why this exists
---------------
Before this module landed (2026-05-10), every prompt in
``pipeline/llm/`` was hand-written next to — but not derived from —
the validators that gated its output downstream. The two would
inevitably drift: the rewrite prompt taught the model "Tell me what
you would have done" as a GOOD CTA example, while
``pipeline.llm.script_check._CTA_PATTERNS`` only accepted the inverse
word order ("what would you have done"). The model copied the GOOD
example faithfully, the validator rejected it, and the entire 30-image
render aborted minutes later with no retry.

The fix has two halves:

1. **Validators expose their constraints as data**, not just regex
   modules. ``script_check._CTA_RULES`` now pairs each pattern with a
   guaranteed-matching example. The contract pulls those examples
   straight into the prompt — drift becomes impossible.

2. **A central orchestrator runs every LLM stage** with the same loop:
   build prompt with constraints baked in → call Azure → validate
   inline → if failures, regenerate with a focused diff prompt → cap
   retries → raise a structured error if exhausted.

The result: a stage that previously had ONE shot to satisfy
loosely-coupled validators now has up to ``max_retries+1`` chances and
each retry knows exactly what failed.

Public surface
--------------
- :class:`StageContext` — input passed to a stage (channel cfg, raw
  input, optional learned corrections).
- :class:`Constraint` — one named rule a stage's output must satisfy.
- :class:`ValidationFailure` — what came back when validation didn't
  pass; severity ``"error"`` triggers regen, ``"warning"`` is logged.
- :class:`StageContract` (Protocol) — what each stage must implement
  (gather_constraints, build_prompt, regen_prompt, validate).
- :func:`run_stage` — drive one stage end-to-end.
- :class:`OrchestratorError` — raised when retries are exhausted.

Each stage's contract lives under ``pipeline/llm/contracts/``. The
first one is :mod:`pipeline.llm.contracts.rewrite_contract`; cast and
prompts contracts will follow as the same pattern.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from .. import telemetry as _tlm
from .cli import call_claude_cli, model_for
from .fix import Fix, ValidationFailure, errors as fix_errors, warnings as fix_warnings  # noqa: F401

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class StageContext:
    """Input bundle for one LLM stage invocation.

    ``channel_cfg`` is the parsed channel YAML (closer_format, length
    targets, banned phrases, kit_lock, etc.). ``raw_input`` is the
    stage-specific payload (e.g. for rewrite: the raw_story dict;
    for cast: the produced Script). ``learned_corrections`` is reserved
    for the future feedback loop (post-render critic verdicts piped
    back into the prompt as negative examples) — the contract may
    ignore it today.

    ``upstream`` lets later stages see what earlier ones produced
    (cast sees script, prompts sees script + cast). Read-only from
    the contract's POV — written by the pipeline runner as the DAG
    advances.
    """
    channel: str
    channel_cfg: dict
    raw_input: dict
    learned_corrections: list[dict] = field(default_factory=list)
    upstream: dict = field(default_factory=dict)
    sub_index: int | None = None    # for fanout=beats / fanout=tts_chunks


@dataclass(frozen=True)
class Constraint:
    """One named rule the LLM output must satisfy.

    Constraints are the SHARED VOCABULARY between the prompt and the
    validator: the contract uses them to write the prompt's "rules"
    block AND the validator translates the same constraints into pass /
    fail checks. By construction they cannot drift.

    - ``name`` matches the validator's issue ``code`` so a failure can
      be traced back to the constraint that was supposed to prevent it
      (``"missing_cta"``, ``"long_narration"`` etc.).
    - ``severity`` mirrors :class:`pipeline.llm.script_check.ScriptIssue.severity`.
    - ``description`` is rendered into the prompt verbatim — keep it
      imperative and concrete ("the narration MUST end with a verdict
      question chosen from the GOOD examples below").
    - ``examples_good`` / ``examples_bad`` are written into the prompt
      so the model has a concrete target. The good examples MUST be
      sourced from validator-paired data (e.g. ``_CTA_RULES``), never
      hand-typed alongside the validator regex.
    """
    name: str
    severity: str  # "error" | "warning"
    description: str
    examples_good: tuple[str, ...] = ()
    examples_bad: tuple[str, ...] = ()


@runtime_checkable
class StageContract(Protocol):
    """What each LLM stage must implement to plug into :func:`run_stage`.

    Optional attributes (set as class vars on concrete contracts):

    - ``fanout`` — ``1`` for single-output stages, ``"beats"`` /
      ``"tts_chunks"`` for fan-out stages where the pipeline runner
      invokes the contract once per sub-index (beat / chunk).
    - ``artifact_paths`` — declared paths (for the Fix router). E.g.
      RewriteContract owns ``"narration"`` + ``"title_options"``;
      ImagesContract owns ``"beats[*].image"``.

    These default to single-output / unrouted if the concrete contract
    doesn't set them; the router will fall back to ``target_stage``
    matching alone.
    """

    name: str  # "rewrite" | "cast" | "prompts" | "critic" | …

    def gather_constraints(self, ctx: StageContext) -> list[Constraint]:
        """Resolve the full set of rules for this stage + context."""
        ...

    def build_prompt(self, ctx: StageContext, constraints: list[Constraint]) -> str:
        """Compose the first-attempt prompt.

        MUST embed every error-level constraint explicitly with its
        ``examples_good`` / ``examples_bad`` blocks so the model can
        satisfy them in one shot.
        """
        ...

    def regen_prompt(
        self,
        ctx: StageContext,
        prev_output: dict,
        fixes: list[Fix],
    ) -> str:
        """Compose a regeneration prompt after a failed attempt.

        The contract decides whether to ask for a full rewrite or a
        targeted patch — both are valid. The orchestrator passes ALL
        fixes (errors + warnings); the contract chooses which to act
        on (typically errors only, but warnings are useful context).
        """
        ...

    def validate(self, ctx: StageContext, output: dict) -> list[Fix]:
        """Run every gate that would otherwise fire downstream.

        MUST use the same validator the rest of the pipeline runs
        (``script_check`` / ``script_lint`` / etc.) — never duplicate
        rule logic. Return all fixes; the orchestrator separates
        errors from warnings.
        """
        ...


@dataclass
class StageResult:
    """What :func:`run_stage` returns on success."""
    output: dict
    attempts: int
    final_warnings: list[Fix]
    backend: str
    elapsed_s: float


class OrchestratorError(RuntimeError):
    """Raised when a stage fails validation after every retry."""

    def __init__(
        self,
        stage: str,
        attempts: int,
        last_failures: list[Fix],
        last_output: dict | None,
    ) -> None:
        msg = (
            f"stage {stage!r} failed validation after {attempts} attempts: "
            + ", ".join(f"{f.constraint}({f.severity})" for f in last_failures)
        )
        super().__init__(msg)
        self.stage = stage
        self.attempts = attempts
        self.last_failures = last_failures
        self.last_output = last_output


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


_DEFAULT_MAX_RETRIES = int(os.environ.get("YTFACTORY_LLM_MAX_RETRIES", "2"))


def run_stage(
    contract: StageContract,
    ctx: StageContext,
    *,
    max_retries: int | None = None,
    timeout_s: int = 600,
    llm_call: Callable[..., Any] | None = None,
) -> StageResult:
    """Run one LLM stage with constraint-aware prompting + auto-retry.

    Loop:
      1. Gather constraints for this context.
      2. Build prompt (first attempt) or regen prompt (subsequent).
      3. Call the configured LLM backend via ``call_claude_cli``.
      4. Validate output against the contract's gates.
      5. If error-level fixes: keep retrying until ``max_retries``
         is exhausted.
      6. If only warnings: succeed, surface them on the result.

    Args:
        contract: The stage's :class:`StageContract` implementation.
        ctx: The :class:`StageContext` bundle.
        max_retries: Override the default (``YTFACTORY_LLM_MAX_RETRIES``
            env, default 2 → up to 3 attempts total).
        timeout_s: Per-LLM-call timeout.
        llm_call: Test seam — defaults to
            :func:`pipeline.llm.cli.call_claude_cli`.

    Raises:
        OrchestratorError: when every attempt produced error-level
            failures. Carries the last output + failures for inspection.
    """
    retries = _DEFAULT_MAX_RETRIES if max_retries is None else max_retries
    if llm_call is None:
        llm_call = call_claude_cli

    constraints = contract.gather_constraints(ctx)
    job_id = os.environ.get("YTFACTORY_JOB_ID") or None
    backend_tag = os.environ.get("YTFACTORY_LLM_BACKEND", "auto")

    t0 = time.time()
    prev_output: dict | None = None
    prev_fixes: list[Fix] = []

    for attempt in range(retries + 1):
        if attempt == 0:
            prompt = contract.build_prompt(ctx, constraints)
        else:
            assert prev_output is not None
            logger.warning(
                "[orchestrator] stage=%s retry %d/%d after errors: %s",
                contract.name, attempt, retries,
                [f.constraint for f in fix_errors(prev_fixes)],
            )
            prompt = contract.regen_prompt(ctx, prev_output, prev_fixes)

        try:
            output = llm_call(
                prompt,
                output_json=True,
                model=model_for(contract.name),
                stage=contract.name,
                timeout_s=timeout_s,
            )
        except Exception as e:
            _tlm.track(
                "llm_stage", category="llm", success=False,
                duration_ms=int((time.time() - t0) * 1000),
                job_id=job_id,
                metadata={"stage": contract.name, "attempt": attempt + 1,
                          "backend": backend_tag, "error": str(e)[:200]},
            )
            raise

        if not isinstance(output, dict):
            fixes = [Fix(
                constraint="bad_shape",
                severity="error",
                reason=f"expected JSON object, got {type(output).__name__}",
                target_stage=contract.name,
                source_judge="orchestrator",
            )]
        else:
            fixes = contract.validate(ctx, output)

        errors_now = fix_errors(fixes)
        warnings_now = fix_warnings(fixes)

        if not errors_now:
            elapsed = time.time() - t0
            _tlm.track(
                "llm_stage", category="llm", success=True,
                duration_ms=int(elapsed * 1000),
                job_id=job_id,
                metadata={"stage": contract.name, "attempts": attempt + 1,
                          "backend": backend_tag, "warnings": len(warnings_now)},
            )
            if warnings_now:
                logger.info(
                    "[orchestrator] stage=%s ok with %d warnings on attempt %d: %s",
                    contract.name, len(warnings_now), attempt + 1,
                    [w.constraint for w in warnings_now],
                )
            return StageResult(
                output=output,
                attempts=attempt + 1,
                final_warnings=warnings_now,
                backend=backend_tag,
                elapsed_s=elapsed,
            )

        prev_output = output if isinstance(output, dict) else None
        prev_fixes = fixes

    elapsed = time.time() - t0
    _tlm.track(
        "llm_stage", category="llm", success=False,
        duration_ms=int(elapsed * 1000),
        job_id=job_id,
        metadata={"stage": contract.name, "attempts": retries + 1,
                  "backend": backend_tag,
                  "last_failures": [f.constraint for f in prev_fixes]},
    )
    raise OrchestratorError(
        stage=contract.name,
        attempts=retries + 1,
        last_failures=prev_fixes,
        last_output=prev_output,
    )
