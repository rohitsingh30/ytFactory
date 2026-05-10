"""Shared helpers for stage contracts.

Renders constraint blocks consistently across stages so prompts have a
predictable structure. Helps the LLM lock onto the rules — and helps
operators reading the prompt logs.
"""

from __future__ import annotations

from ..orchestrator import Constraint
from ..fix import Fix


def render_constraints_block(constraints: list[Constraint]) -> str:
    """Render constraints as a deterministic Markdown-ish rules block.

    Errors first (CRITICAL), then warnings (preferred), each with their
    examples_good / examples_bad blocks. The format is stable so prompt
    logs diff cleanly across stages and channels.
    """
    out: list[str] = []

    err = [c for c in constraints if c.severity == "error"]
    warn = [c for c in constraints if c.severity != "error"]

    if err:
        out.append("CRITICAL RULES (the output is REJECTED if any of these is violated):")
        for c in err:
            out.append(_render_one_constraint(c, prefix="  - "))
            out.append("")

    if warn:
        out.append("PREFERENCES (we'd like these too — they don't reject the output but a Shorts viewer notices):")
        for c in warn:
            out.append(_render_one_constraint(c, prefix="  - "))
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def _render_one_constraint(c: Constraint, prefix: str) -> str:
    lines = [f"{prefix}**{c.name}** — {c.description}"]
    if c.examples_good:
        lines.append("    GOOD examples (any of these phrasings satisfies the rule):")
        for ex in c.examples_good:
            lines.append(f"      • {ex!r}")
    if c.examples_bad:
        lines.append("    BAD examples (do NOT phrase the output like any of these):")
        for ex in c.examples_bad:
            lines.append(f"      • {ex!r}")
    return "\n".join(lines)


def render_fixes_block(fixes: list[Fix]) -> str:
    """Render a Fix list as the regen-prompt's diagnostic block.

    The model sees: which constraint fired, why, and (when supplied) a
    suggested patch. Errors are prioritised; warnings are listed in a
    smaller "also consider" block.
    """
    err = [f for f in fixes if f.severity == "error"]
    warn = [f for f in fixes if f.severity != "error"]

    out: list[str] = []
    if err:
        out.append("Your previous output FAILED these rules — you MUST fix every one of them:")
        for f in err:
            out.append(f"  - **{f.constraint}** — {f.reason}")
            if f.suggested_patch:
                out.append(f"    Suggested fix: {f.suggested_patch!r}")
        out.append("")
    if warn:
        out.append("Also consider improving (warnings — don't gate the output):")
        for f in warn:
            out.append(f"  - **{f.constraint}** — {f.reason}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
