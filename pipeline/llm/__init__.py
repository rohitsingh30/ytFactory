"""LLM-driven authoring + critique stack.

Sub-packaged 2026-05-05 (Phase 3 / Tier-S3 layout cleanup): the 14
flat modules under ``pipeline/`` that all routed Claude CLI calls
were grouped here for discoverability.

This ``__init__.py`` re-exports the public symbols of the original
``pipeline/llm.py`` (now ``pipeline/llm/cli.py``) so existing
callers like ``from pipeline.llm import call_claude_cli`` keep
working with no change.

Sibling modules (``pipeline.llm.cast``, ``pipeline.llm.critic``,
``pipeline.llm.prompts`` etc.) are imported via their fully-qualified
new paths — callers that used the old flat paths
(``from pipeline.cast import load_cast``) were updated in the same
commit.
"""
from __future__ import annotations

# Re-export the Claude CLI surface so `from pipeline.llm import X` still works.
from pipeline.llm.cli import (  # noqa: F401
    BACKEND_ANTHROPIC,
    BACKEND_AZURE,
    BACKEND_CLI,
    ClaudeCLIError,
    call_claude_cli,
    call_llm,
    model_for,
    _choose_backend,
    _parse_inner_json,
    _select_backend,
    _should_use_sdk,
)
