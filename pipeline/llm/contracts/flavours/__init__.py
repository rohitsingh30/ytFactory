"""Channel × format flavours.

Today only RewriteContract knows about closer_format / cliffhanger
toggles via channel_cfg. As we add cast / prompts / critic contracts,
the per-channel rule sets multiply: AITA channels need CTA + banned
acronyms + named-antagonist; cliffhanger channels swap the CTA pool;
ranked-sports channels need a Top-N counter; kathaa needs slow pacing.

Rather than letting that complexity creep into each contract as a
forest of ``if cfg.get("cliffhanger")`` branches, flavours encapsulate
ONE axis of variation:

- :class:`AITAFlavour` — AITA-class constraints (CTA, banned acronyms,
  spicy-must-haves).
- :class:`CliffhangerFlavour` — replaces CTA constraint with the
  cliffhanger CTA pool, removes the verdict-question requirement.
- :class:`RankedFlavour` — adds Top-N counter constraint to prompts.
- (more to come — kathaa, devotional, footage-only, …)

A contract composes constraints by asking each active flavour what to
add / replace. Channel YAML grows a ``flavours: [aita, cliffhanger]``
list — adding a new format is a YAML line, not a contract edit.

This module ships the protocol + the AITA + Cliffhanger flavours
(used by RewriteContract today). The other flavours land alongside
their respective contracts.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ... import script_check
from ...orchestrator import Constraint


@runtime_checkable
class Flavour(Protocol):
    """A composable rule set keyed on (channel, format)."""

    name: str

    def constraints(self, base: list[Constraint]) -> list[Constraint]:
        """Return the constraint list with this flavour's adjustments.

        ``base`` is what the contract has so far (often empty on the
        first flavour, populated as each flavour layers in). The
        flavour returns the new full list — append, replace by name,
        or filter as it sees fit.
        """
        ...


def compose_flavours(flavours: list[Flavour]) -> list[Constraint]:
    """Run ``flavours`` left-to-right; later ones override earlier ones."""
    out: list[Constraint] = []
    for fl in flavours:
        out = fl.constraints(out)
    return out


# ---------------------------------------------------------------------------
# Concrete flavours
# ---------------------------------------------------------------------------


class AITAFlavour:
    """The AITA-class rule set (vanilla — non-cliffhanger).

    Plugs into RewriteContract via channel YAML
    ``flavours: [aita]``. Adds / overrides:

    - ``missing_cta`` — paired with the AITA CTA examples from the
      script_check registry.
    - ``banned_acronyms`` — strict ban on AITA / WIBTA / YTA / NTA /
      NAH / ESH and "am I the asshole" in spoken narration.
    - Spicy must-haves (named antagonist, specific number/date,
      escalation curve, WAIT-WHAT pivot, named stakes) — currently as
      warnings, escalate to errors per channel preference.
    """
    name = "aita"

    def constraints(self, base: list[Constraint]) -> list[Constraint]:
        cta_examples = tuple(script_check.cta_examples(cliffhanger=False))
        return _merge_by_name(base, [
            Constraint(
                name="missing_cta",
                severity="error",
                description=(
                    "The last 2 sentences MUST contain a closing CTA "
                    "from the GOOD examples below."
                ),
                examples_good=cta_examples,
                examples_bad=(
                    "AITA?", "Am I the asshole?",
                    "Smash that like button and subscribe.",
                ),
            ),
            Constraint(
                name="banned_acronyms",
                severity="error",
                description=(
                    "NEVER author the verdict acronyms AITA / WIBTA / YTA / "
                    "NTA / NAH / ESH or the literal phrase \"am I the "
                    "asshole\" in spoken narration."
                ),
                examples_bad=(
                    "AITA for refusing the cake?",
                    "WIBTA if I told her no?",
                ),
            ),
        ])


class CliffhangerFlavour:
    """Part-1 cliffhanger flavour. Replaces AITA CTA with subscribe-CTA."""
    name = "cliffhanger"

    def constraints(self, base: list[Constraint]) -> list[Constraint]:
        cta_examples = tuple(script_check.cta_examples(cliffhanger=True))
        return _merge_by_name(base, [
            Constraint(
                name="missing_cta",
                severity="error",
                description=(
                    "Part-1 narrations MUST end on a Part-2 / subscribe CTA "
                    "(no verdict question — the verdict lives in Part 2)."
                ),
                examples_good=cta_examples,
                examples_bad=(
                    "Am I wrong here?",
                    "What would you have done?",
                ),
            ),
        ])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _merge_by_name(base: list[Constraint], additions: list[Constraint]) -> list[Constraint]:
    """Replace any base constraint sharing a name with an addition; append the rest.

    Lets later flavours override earlier ones cleanly: AITAFlavour
    adds ``missing_cta``; CliffhangerFlavour replaces it with the
    cliffhanger version when both are active.
    """
    by_name: dict[str, Constraint] = {c.name: c for c in base}
    for c in additions:
        by_name[c.name] = c
    return list(by_name.values())
