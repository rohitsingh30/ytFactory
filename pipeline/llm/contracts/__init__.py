"""Stage contracts — one per LLM stage in the orchestrator.

Each contract is a :class:`pipeline.llm.orchestrator.StageContract`
implementation that names its stage, gathers constraints from the
SAME validators the rest of the pipeline runs, builds prompts whose
GOOD examples are derived from those validators (eliminating drift),
and validates outputs inline so failures get caught BEFORE the render
goes 30 images deep.

See :mod:`pipeline.llm.orchestrator` for the runner that drives all
of these.
"""

from __future__ import annotations

from .rewrite_contract import RewriteContract  # noqa: F401
