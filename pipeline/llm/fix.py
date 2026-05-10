"""Cross-stage validation + repair contract: the ``Fix`` object.

A ``Fix`` is the unit of communication between any validator (deterministic
or AI-judge) and any stage that needs to repair its output. Every failure
in the pipeline gets expressed as one of these so the orchestrator can
route them back to the owning stage and ask for a focused regeneration.

Design
------
A Fix names FOUR things:

1. **target_stage** — which stage owns the bad artifact
   (``"rewrite"`` / ``"cast"`` / ``"prompts"`` / ``"images"`` / …).
   The :mod:`pipeline.llm.fix_router` looks this up to pick the
   contract that should regenerate.

2. **target_path** — the artifact path inside that stage's output. For
   per-beat stages this includes a sub-index (``"beats[14].image"``).
   The router parses it; the cache uses it to invalidate ONLY the
   affected key (the central reuse-cache trick — re-render one image,
   not 30).

3. **constraint** — a name that comes from a validator's registered
   rule (``"missing_cta"`` / ``"cast_drift"`` / ``"image_mismatch"``).
   Lets the regen prompt say "you violated rule X" instead of just
   echoing free-form prose.

4. **reason** — human-readable diagnostic. Goes into both the regen
   prompt (so the model knows what went wrong) and the per-attempt
   telemetry (so an operator can see why a stage retried).

Optional fields:

- **suggested_patch** — a concrete string the model can lift verbatim
  if it can't think of a better fix. The CTA examples
  (``script_check.cta_examples()``) are a perfect source for this.
- **source_judge** — which validator produced the Fix. Used for
  telemetry / debugging the validator itself.

Severity:

- ``"error"`` — orchestrator must regen. Counts toward retry budget.
- ``"warning"`` — passed through to the result; logged but not retried.
"""

from __future__ import annotations

from dataclasses import dataclass

# Severity constants — also used by ScriptIssue downstream so callers can
# compare without importing the dataclass.
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class Fix:
    """A typed report that an artifact violates a constraint.

    All fields except ``constraint`` and ``reason`` have safe defaults
    so existing validators that previously emitted free-form
    ``ScriptIssue`` objects can produce ``Fix`` instances incrementally.
    """
    constraint: str           # registered constraint name, eg. "missing_cta"
    reason: str               # human diagnostic for prompt + telemetry
    severity: str = SEVERITY_ERROR
    target_stage: str = ""    # owning stage; "" = unknown / let router infer
    target_path: str = ""     # artifact path; "" = whole stage output
    suggested_patch: str | None = None
    source_judge: str = ""    # validator that produced this Fix


# Back-compat shim for the orchestrator's earlier ValidationFailure name.
# Same semantics, narrower vocabulary — kept so older test code continues
# to work while new code uses Fix everywhere.
ValidationFailure = Fix


def errors(fixes: list[Fix]) -> list[Fix]:
    """Return only the error-level Fixes (the ones that gate a retry)."""
    return [f for f in fixes if f.severity == SEVERITY_ERROR]


def warnings(fixes: list[Fix]) -> list[Fix]:
    return [f for f in fixes if f.severity == SEVERITY_WARNING]


def by_stage(fixes: list[Fix]) -> dict[str, list[Fix]]:
    """Group fixes by their target stage. Empty target_stage → '_unrouted'."""
    out: dict[str, list[Fix]] = {}
    for f in fixes:
        out.setdefault(f.target_stage or "_unrouted", []).append(f)
    return out
