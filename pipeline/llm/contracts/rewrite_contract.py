"""RewriteContract — Stage 3 (rewrite) re-implemented as a contract.

Pulls EVERY rule from the same validators (`script_check` +
`script_lint`) the render later runs, derives the prompt's GOOD
example block from `script_check.cta_examples()` (drift-impossible),
validates the output inline, and asks the orchestrator for an
auto-retry on any error-level Fix.

Replaces the historical pattern of:

    1. hand-write _BASE_PROMPT alongside script_check
    2. call_claude_cli once
    3. WAY LATER, script_check.report(fail_on_error=True) raises
    4. entire 30-image render aborted

with:

    1. RewriteContract.gather_constraints(ctx) — sourced from validators
    2. RewriteContract.build_prompt(ctx, constraints) — examples derived
    3. orchestrator.run_stage(...) calls Azure
    4. RewriteContract.validate(ctx, output) — same gates, RIGHT NOW
    5. on error: RewriteContract.regen_prompt(...) → orchestrator retries
       with a focused diff prompt up to YTFACTORY_LLM_MAX_RETRIES times
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import script_check, script_lint
from ..fix import Fix
from ..orchestrator import Constraint, StageContext
from .base import render_constraints_block, render_fixes_block


@dataclass
class RewriteContract:
    """The orchestrated rewrite stage — see module docstring.

    Two render modes (selected by ``channel_cfg``):
      - vanilla AITA: a single-part narration ending with a verdict-CTA
      - cliffhanger: Part-1 ending with a Part-2 / subscribe CTA

    Both use the same orchestrator loop; only the constraints differ
    (closer flavour, no AITA verdict in cliffhanger Part 1).
    """

    name: str = "rewrite"
    fanout: int = 1
    artifact_paths: tuple[str, ...] = ("narration", "title_options", "hook")

    # ------------------------------------------------------------------
    # Constraints — the single source of truth shared with the validator
    # ------------------------------------------------------------------

    def gather_constraints(self, ctx: StageContext) -> list[Constraint]:
        """Resolve the rule set for this rewrite invocation.

        Errors gate the orchestrator's retry loop; warnings are
        surfaced on the result but don't trigger regen. The mapping
        mirrors the severity that ``script_check.check_script_text``
        emits for the same rules — same source of truth for both.
        """
        cfg = ctx.channel_cfg
        is_cliffhanger = bool(cfg.get("cliffhanger"))
        cta_examples = tuple(script_check.cta_examples(cliffhanger=is_cliffhanger))

        constraints: list[Constraint] = [
            Constraint(
                name="missing_cta",
                severity="error",
                description=(
                    "The last 2 sentences MUST contain a closing CTA "
                    "from the GOOD examples below. The visual closer "
                    "panel handles the AITA-acronym ask separately — "
                    "the spoken narration must use natural English."
                ),
                examples_good=cta_examples,
                examples_bad=(
                    "AITA?",
                    "Am I the asshole?",
                    "Smash that like button and subscribe.",
                    "Don't forget to subscribe and hit the bell.",
                    "LIKE if YTA, COMMENT if NTA. AITA?",
                ),
            ),
            Constraint(
                name="banned_acronyms",
                severity="error",
                description=(
                    "NEVER author the verdict acronyms AITA / WIBTA / YTA / "
                    "NTA / NAH / ESH or the literal phrase \"am I the "
                    "asshole\" anywhere in the spoken narration. The "
                    "audio path strips any sentence containing those "
                    "tokens — authoring them just loses the closer."
                ),
                examples_bad=(
                    "AITA for refusing the cake?",
                    "Am I the asshole here?",
                    "WIBTA if I told her no?",
                ),
            ),
            Constraint(
                name="length",
                severity="warning",  # script_check reports long_narration as warning
                description=(
                    "Aim for 110–160 words / 10–15 short sentences / "
                    "~22–32 seconds spoken. Past 165 words / ~35s "
                    "Shorts retention craters."
                ),
            ),
            Constraint(
                name="hook_first_8_words",
                severity="warning",
                description=(
                    "The opening ~8 words MUST be a hook: a curiosity "
                    "question, an \"Am I wrong for…\" frame, or a "
                    "strong-claim verb (refused, told, caught, threw…). "
                    "NEVER use the AITA acronym in the spoken hook. "
                    "NEVER \"Hi guys\" or \"today's story is\"."
                ),
                examples_good=(
                    "Am I wrong for refusing my sister's wedding cake?",
                    "I told my MIL she couldn't come to the birth.",
                    "I caught my husband texting his ex at our anniversary.",
                ),
                examples_bad=(
                    "Hi guys, today's story is about my sister.",
                    "So this happened last week and I'm still in shock.",
                ),
            ),
            Constraint(
                name="inciting_wedge",
                severity="warning",
                description=(
                    "The first ~30 words MUST contain a number, a name "
                    "with a relationship label (\"my MIL Carol\"), or a "
                    "specific prop. Spell numbers as words for TTS."
                ),
            ),
        ]
        return constraints

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def build_prompt(self, ctx: StageContext, constraints: list[Constraint]) -> str:
        """Compose the first-attempt rewrite prompt.

        Constraints render at the TOP so they're the model's first
        signal; the source story is at the bottom (LLMs anchor on
        what's nearest the answer slot).
        """
        title = (ctx.raw_input.get("title") or "").strip()
        body = (ctx.raw_input.get("body") or "").strip()
        story_text = f"{title}\n\n{body}" if title else body

        rules_block = render_constraints_block(constraints)
        cliffhanger_note = (
            "\nThis story is rendering as PART 1 of a two-part cliffhanger. "
            "End on a moment that demands the viewer come back for Part 2 — "
            "the verdict question lives in Part 2.\n"
            if ctx.channel_cfg.get("cliffhanger") else ""
        )

        return (
            f"You are writing a 22–32 second YouTube Shorts narration in the "
            f"style of top AITA / Reddit-story channels.\n\n"
            f"{rules_block}"
            f"{cliffhanger_note}"
            f"\nADDITIONAL CRAFT GUIDANCE (these aren't gated by validators "
            f"but they're what separates retentive AITA from algorithmic "
            f"mush):\n"
            f"  - NAME the antagonist with their relationship "
            f"(\"my MIL Carol\", \"my SIL Megan\").\n"
            f"  - SPECIFIC numbers/dates/props, spelled as words for TTS.\n"
            f"  - Escalation curve: hook → first wrong → antagonist doubles "
            f"down → moment it broke → kicker.\n"
            f"  - One unmistakable villain action (don't soften — \"screamed\" "
            f"not \"got upset\").\n"
            f"  - WAIT-WHAT pivot in the middle (a detail that flips the read).\n"
            f"  - Stakes the narrator names directly (what they will lose).\n"
            f"  - Conversational present tense. No \"crazy story!\" "
            f"editorializing.\n\n"
            f"Input story (mine the specific details, names, numbers, props):\n"
            f"\"\"\"\n{story_text[:6000]}\n\"\"\"\n\n"
            f"Return ONLY a JSON object (no prose, no markdown fences):\n"
            f"{{\n"
            f"  \"hook\": \"<first ~5–10 words of the narration>\",\n"
            f"  \"narration\": \"<full narration including hook AND the closing CTA>\",\n"
            f"  \"title_options\": [\"<click-bait title A>\", \"<title B>\", \"<title C>\"]\n"
            f"}}"
        )

    # ------------------------------------------------------------------
    # Regen prompt — focused diff with the previous output + the Fixes
    # ------------------------------------------------------------------

    def regen_prompt(
        self,
        ctx: StageContext,
        prev_output: dict,
        fixes: list[Fix],
    ) -> str:
        """Compose the regeneration prompt after a failed attempt.

        Strategy: don't re-issue the entire system prompt; just hand
        the model its own previous output + the diagnostic block + the
        rule it broke. Cheaper, faster, and the model is far better
        at "fix this one thing" than "rewrite from scratch following
        all rules".
        """
        prev_narration = (prev_output.get("narration") or "").strip()
        prev_hook = (prev_output.get("hook") or "").strip()
        diag = render_fixes_block(fixes)

        return (
            f"You wrote this narration on the previous attempt — it "
            f"violated one or more rules. Rewrite it to fix every "
            f"error-level rule below. Keep what worked: same story, "
            f"same characters, same voice — just patch the broken "
            f"parts.\n\n"
            f"Previous hook: {prev_hook!r}\n\n"
            f"Previous narration:\n\"\"\"\n{prev_narration}\n\"\"\"\n\n"
            f"{diag}\n"
            f"Return ONLY a JSON object (same shape as before):\n"
            f"{{\n"
            f"  \"hook\": \"...\",\n"
            f"  \"narration\": \"...\",\n"
            f"  \"title_options\": [\"...\", \"...\", \"...\"]\n"
            f"}}"
        )

    # ------------------------------------------------------------------
    # Validation — runs the EXACT same gates the renderer would
    # ------------------------------------------------------------------

    def validate(self, ctx: StageContext, output: dict) -> list[Fix]:
        """Run script_check + script_lint and return any failures as Fixes.

        Crucial invariant: this validator MUST be the same one the
        downstream renderer runs (``pipeline.llm.script_check``). Otherwise
        we're just moving the drift problem.
        """
        fixes: list[Fix] = []

        # Required-field shape check.
        for field_name in ("hook", "narration", "title_options"):
            if field_name not in output:
                fixes.append(Fix(
                    constraint="missing_field",
                    reason=f"output missing required field {field_name!r}",
                    target_stage=self.name,
                    target_path=field_name,
                    source_judge="rewrite_contract",
                ))
        titles = output.get("title_options")
        if titles is not None and (not isinstance(titles, list) or not titles):
            fixes.append(Fix(
                constraint="missing_field",
                reason="title_options must be a non-empty list",
                target_stage=self.name,
                target_path="title_options",
                source_judge="rewrite_contract",
            ))
        narration = output.get("narration")
        if not isinstance(narration, str) or not narration.strip():
            fixes.append(Fix(
                constraint="missing_field",
                reason="narration must be a non-empty string",
                target_stage=self.name,
                target_path="narration",
                source_judge="rewrite_contract",
            ))
            return fixes  # downstream checks need the string

        # script_lint runs first — auto-fixes some structural things and
        # never gates. We log lint info but don't surface as Fix.
        lint = script_lint.lint_and_fix(narration.strip())
        narration_for_check = lint.narration if lint.fixed else narration.strip()

        # script_check is the gate. Errors → Fix(severity=error). Warnings
        # → Fix(severity=warning) so they bubble through to the StageResult.
        issues = script_check.check_script_text(
            narration_for_check, channel_cfg=ctx.channel_cfg
        )
        for iss in issues:
            fixes.append(_issue_to_fix(iss, target_stage=self.name))
        return fixes


def _issue_to_fix(issue, *, target_stage: str) -> Fix:
    """Translate a ScriptIssue into a Fix tied to this stage's narration."""
    return Fix(
        constraint=issue.code,
        reason=issue.message,
        severity=issue.severity,
        target_stage=target_stage,
        target_path="narration",
        source_judge="script_check",
    )
