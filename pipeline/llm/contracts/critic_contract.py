"""CriticContract — automated post-render judge.

Wraps the manual ``/judge-video`` skill as an in-pipeline stage: takes
the rendered mp4 + the script + the cast lock, samples N frames, and
asks an LLM to return a structured verdict + list of Fixes.

Verdicts:
  - ``SHIP``  — render is good; pipeline runner allows the conditional
    upload edge to fire.
  - ``FIX``   — render needs targeted regen; runner cascades the Fix
    list through :func:`pipeline.llm.fix_router.cascade`, invalidates
    only the affected cache keys, re-runs the dependent stages.
  - ``BLOCK`` — fundamentally bad (e.g. the source story doesn't fit
    the channel); runner halts and writes the verdict back to
    Firestore for operator review.

Caps:
  - ``YTFACTORY_CRITIC_MAX_PASSES`` (default 2) — total critic passes
    per job. Even if the critic keeps returning FIX, the runner stops
    cascading after this many passes and either ships (if last verdict
    was warning-only) or marks failed. Bounds spend.
  - This stage is itself orchestrated, so the per-pass critic LLM call
    auto-retries up to ``YTFACTORY_LLM_MAX_RETRIES`` if its OWN output
    fails the contract's validate (bad shape, missing verdict field).

Today the critic runs on text-only context (script + cast lock + a
few frame URIs). When ``image_judge`` ships in Phase 5+, the critic
will also receive judge verdicts per beat.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..fix import Fix
from ..orchestrator import Constraint, StageContext
from .. import critic_axes
from .base import render_constraints_block, render_fixes_block


_VALID_VERDICTS = critic_axes.VERDICTS


@dataclass
class CriticContract:
    """Post-render automated judge — see module docstring."""

    name: str = "critic"
    fanout: int = 1
    artifact_paths: tuple[str, ...] = ("critic.verdict", "critic.fixes")

    # ------------------------------------------------------------------
    # Constraints — what the critic's OWN output has to look like
    # ------------------------------------------------------------------

    def gather_constraints(self, ctx: StageContext) -> list[Constraint]:
        return [
            Constraint(
                name="axes_field",
                severity="error",
                description=(
                    "Output MUST include an 'axes' object with all "
                    f"{len(critic_axes.REQUIRED_AXIS_NAMES)} required "
                    "axes scored 1-10: "
                    + ", ".join(critic_axes.REQUIRED_AXIS_NAMES) + ". "
                    "The pipeline DERIVES the SHIP/FIX/BLOCK verdict "
                    "from these axes — score honestly. Any axis ≤ 3 "
                    "auto-BLOCKs; any axis < 7 auto-FIXes; all ≥ 7 "
                    "ships. Sandbagging a 4 to a 7 will silently break "
                    "downstream regen. (Optional axes like vbench_score "
                    "are populated by the CPU adapter — do NOT score "
                    "them; the pipeline gates on them when present.)"
                ),
                examples_good=(
                    '{"axes": {"hook_strength": 8, "caption_legibility": 9, '
                    '"cast_continuity": 8, "mute_mode_score": 7, '
                    '"source_fidelity": 8, "closer_strength": 7}, '
                    '"verdict": "SHIP", "fixes": []}',
                    '{"axes": {"hook_strength": 4, "caption_legibility": 9, '
                    '"cast_continuity": 8, "mute_mode_score": 6, '
                    '"source_fidelity": 8, "closer_strength": 7}, '
                    '"verdict": "FIX", "fixes": [{"target_stage": "prompts", '
                    '"target_path": "beats[0].prompt", '
                    '"constraint": "static_t_pose_hook", "severity": "error", '
                    '"reason": "beat 0 is a static T-pose with no on-screen text"}]}',
                ),
                examples_bad=(
                    '{"verdict": "SHIP"}  # axes missing — pipeline will FIX',
                    '{"axes": {"hook_strength": "high"}}  # non-int axis',
                    '{"axes": {"hook_strength": 9}}  # only 1 of 6 axes scored',
                ),
            ),
            Constraint(
                name="verdict_field",
                severity="error",
                description=(
                    "Output MUST include a 'verdict' field set to exactly "
                    "one of: 'SHIP', 'FIX', 'BLOCK'. NOTE: the pipeline "
                    "OVERWRITES this from the axes-derived verdict — your "
                    "self-reported verdict is for your own internal "
                    "consistency only."
                ),
                examples_good=(
                    '{"axes": {...}, "verdict": "SHIP", "fixes": []}',
                ),
                examples_bad=(
                    '{"verdict": "MAYBE"}',
                ),
            ),
            Constraint(
                name="fixes_shape",
                severity="error",
                description=(
                    "When the DERIVED verdict is 'FIX' (any axis < 7), "
                    "the 'fixes' field MUST be a non-empty list of "
                    "objects with at minimum: target_stage, target_path, "
                    "constraint, severity, reason."
                ),
            ),
        ]

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def build_prompt(self, ctx: StageContext, constraints: list[Constraint]) -> str:
        rules = render_constraints_block(constraints)
        upstream = ctx.upstream
        script = upstream.get("script") or {}
        cast = upstream.get("cast") or {}
        mp4_uri = upstream.get("mp4_uri") or "unknown"
        frame_uris = upstream.get("frame_uris") or []

        frames_block = (
            "Frame samples (first frame = beat 0):\n"
            + "\n".join(f"  beat {i}: {u}" for i, u in enumerate(frame_uris))
            if frame_uris else "Frame samples: (not provided in this pass)"
        )

        return (
            f"You are the ytFactory post-render critic. You watch one short "
            f"as a YouTube viewer would AND as a pipeline engineer; you "
            f"emit a per-axis structured score. The pipeline derives the "
            f"SHIP/FIX/BLOCK verdict from your axes — see the gating rule "
            f"below.\n\n"
            f"{rules}"
            f"\nMP4: {mp4_uri}\n"
            f"\nNarration:\n\"\"\"\n{(script.get('narration') or '')[:2000]}\n\"\"\"\n"
            f"\nCast lock:\n{_format_cast_summary(cast)}\n"
            f"\n{frames_block}\n"
            f"\nSCORE EACH AXIS 1-10 (10 = best):\n"
            f"{critic_axes.render_axes_block(indent='  ')}\n"
            f"\nGATING RULE: any axis ≤ 3 → BLOCK; any axis < 7 → FIX; "
            f"all ≥ 7 → SHIP. The pipeline overwrites your verdict from "
            f"this rule. Score honestly — sandbagging a 4 to a 7 just "
            f"breaks downstream regen.\n\n"
            f"Return ONLY a JSON object matching the axes_field examples above. "
            f"For FIX verdicts, every Fix MUST name a target_stage from "
            f"{{rewrite, cast, prompts, images, tts, asr, compose}}, a "
            f"target_path that points at the broken artifact (e.g. "
            f"\"beats[14].image\"), the constraint that fired, severity "
            f"\"error\" or \"warning\", and a 1-2 sentence reason."
        )

    def regen_prompt(self, ctx: StageContext, prev_output: dict, fixes: list[Fix]) -> str:
        diag = render_fixes_block(fixes)
        return (
            f"Your previous critic output was malformed. Fix it.\n\n"
            f"Previous output:\n{prev_output!r}\n\n"
            f"{diag}\n"
            f"Return ONLY a JSON object with the correct shape:\n"
            f'  {{"axes": {{ {", ".join(f"{n!r}: <int 1-10>" for n in critic_axes.REQUIRED_AXIS_NAMES)} }}, '
            f'"verdict": "SHIP|FIX|BLOCK", "fixes": [...]}}\n'
            f"All {len(critic_axes.REQUIRED_AXIS_NAMES)} axes are required; "
            f"missing or non-int axes will trigger another regen. The "
            f"pipeline derives the verdict from axes."
        )

    # ------------------------------------------------------------------
    # Validation — verdict shape only; the FIXES themselves get routed
    # by fix_router.cascade() at the runner level.
    # ------------------------------------------------------------------

    def validate(self, ctx: StageContext, output: dict) -> list[Fix]:
        """Validate the critic's output AND derive the canonical verdict.

        Side effect: mutates ``output`` in place to set
        ``output["verdict"]`` from ``output["axes"]`` per
        :func:`critic_axes.derive_verdict` — the LLM's self-reported
        verdict is preserved as ``output["verdict_llm"]`` for audit
        but no longer the source of truth. This is the fix for the
        rubber-stamp bug surfaced by the 2026-05-13 27-render audit
        (every job verdict was SHIP because the LLM was free to choose).
        """
        fixes: list[Fix] = []

        axes = output.get("axes")
        # Axes presence is the precondition for a meaningful verdict.
        if not isinstance(axes, dict):
            fixes.append(Fix(
                constraint="axes_field",
                reason=(
                    "axes field missing or not a dict; pipeline cannot "
                    "derive verdict without per-axis scores"
                ),
                target_stage=self.name,
                target_path="critic.axes",
                source_judge="critic_contract",
            ))
            # Also stamp verdict for downstream readers who skip the
            # fixes path.
            output["verdict_llm"] = output.get("verdict")
            output["verdict"] = "FIX"
            return fixes

        # Check every required axis is an int. Optional axes (e.g.
        # vbench_score, populated by the CPU adapter post-LLM) are
        # validated only when present — see critic_axes.OPTIONAL_AXES.
        for name in critic_axes.REQUIRED_AXIS_NAMES:
            v = axes.get(name)
            if isinstance(v, bool) or not isinstance(v, int):
                fixes.append(Fix(
                    constraint="axes_field",
                    reason=(
                        f"axis {name!r} missing or not an int (got {v!r}); "
                        f"pipeline cannot derive verdict"
                    ),
                    target_stage=self.name,
                    target_path=f"critic.axes.{name}",
                    source_judge="critic_contract",
                ))
        # Present-but-malformed optional axes are also FIX-worthy
        # (someone wired the adapter wrong and emitted a string).
        for name in critic_axes.OPTIONAL_AXES:
            if name not in axes:
                continue
            v = axes[name]
            if isinstance(v, bool) or not isinstance(v, int):
                fixes.append(Fix(
                    constraint="axes_field",
                    reason=(
                        f"optional axis {name!r} present but not an int "
                        f"(got {v!r}); adapter emitted wrong type"
                    ),
                    target_stage=self.name,
                    target_path=f"critic.axes.{name}",
                    source_judge="critic_contract",
                ))

        # Derive the canonical verdict regardless of LLM's emission.
        derived = critic_axes.derive_verdict(axes)
        output["verdict_llm"] = output.get("verdict")
        output["verdict"] = derived

        # Validate verdict shape (now that we've forced it).
        if derived not in _VALID_VERDICTS:
            # Should be unreachable; defence-in-depth.
            fixes.append(Fix(
                constraint="verdict_field",
                reason=f"derived verdict {derived!r} not in {_VALID_VERDICTS!r}",
                target_stage=self.name,
                target_path="critic.verdict",
                source_judge="critic_contract",
            ))
            return fixes

        if derived == "FIX":
            raw_fixes = output.get("fixes")
            if not isinstance(raw_fixes, list) or not raw_fixes:
                fixes.append(Fix(
                    constraint="fixes_shape",
                    reason=(
                        "FIX verdict (derived from axes < 7) requires a "
                        "non-empty 'fixes' list — name the broken "
                        "stage(s)/beats so the runner can cascade"
                    ),
                    target_stage=self.name,
                    target_path="critic.fixes",
                    source_judge="critic_contract",
                ))
                return fixes
            for i, f in enumerate(raw_fixes):
                missing = [k for k in
                           ("target_stage", "target_path", "constraint", "severity", "reason")
                           if k not in f]
                if missing:
                    fixes.append(Fix(
                        constraint="fixes_shape",
                        reason=f"fix #{i} missing fields {missing!r}",
                        target_stage=self.name,
                        target_path=f"critic.fixes[{i}]",
                        source_judge="critic_contract",
                    ))
        return fixes


def _format_cast_summary(cast: dict) -> str:
    if not cast:
        return "(no cast.json — channel may not use cast lock)"
    proto = cast.get("protagonist") or {}
    sup = cast.get("supporting") or []
    lines = [f"  protagonist: {proto.get('description', '<missing>')}"]
    for s in sup[:5]:
        lines.append(f"  supporting: {s.get('name', '?')} — "
                     f"{s.get('description', '<missing>')}")
    return "\n".join(lines)
