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
from .base import render_constraints_block, render_fixes_block


_VALID_VERDICTS = ("SHIP", "FIX", "BLOCK")


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
                name="verdict_field",
                severity="error",
                description=(
                    "Output MUST include a 'verdict' field set to exactly "
                    "one of: 'SHIP', 'FIX', 'BLOCK'. SHIP = render is good "
                    "as-is; FIX = list of targeted Fixes for downstream "
                    "stages; BLOCK = the source can't be salvaged."
                ),
                examples_good=(
                    '{"verdict": "SHIP", "weakest_param": "pacing", "fixes": []}',
                    '{"verdict": "FIX", "fixes": [{"target_stage": "prompts", '
                    '"target_path": "beats[7].prompt", '
                    '"constraint": "cast_drift", "severity": "error", '
                    '"reason": "protagonist hair colour changed at beat 7"}]}',
                ),
                examples_bad=(
                    '{"score": 7}',
                    '{"verdict": "MAYBE"}',
                ),
            ),
            Constraint(
                name="fixes_shape",
                severity="error",
                description=(
                    "When verdict is 'FIX', the 'fixes' field MUST be a "
                    "non-empty list of objects with at minimum: "
                    "target_stage, target_path, constraint, severity, reason."
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
            f"emit a structured verdict.\n\n"
            f"{rules}"
            f"\nMP4: {mp4_uri}\n"
            f"\nNarration:\n\"\"\"\n{(script.get('narration') or '')[:2000]}\n\"\"\"\n"
            f"\nCast lock:\n{_format_cast_summary(cast)}\n"
            f"\n{frames_block}\n"
            f"\nReview these for:\n"
            f"  - Hook strength (first 1.5s — does it stop the scroll?).\n"
            f"  - Audio/visual sync (do the captions match the spoken word?).\n"
            f"  - Cast lock drift (does the protagonist look the same in every beat?).\n"
            f"  - Pacing (any beat that lingers? any cut that's too fast?).\n"
            f"  - Closing CTA (natural verdict question, no AITA acronyms).\n"
            f"  - Mute-mode legibility (a viewer with sound off — do they get it?).\n\n"
            f"Return ONLY a JSON object matching the verdict_field examples above. "
            f"For FIX verdicts, every Fix MUST name a target_stage from "
            f"{{rewrite, cast, prompts, images, tts, asr, compose}}, a "
            f"target_path that points at the broken artifact (e.g. "
            f"\"beats[14].image\"), the constraint that fired, severity "
            f"\"error\" or \"warning\", and a 1-2 sentence reason."
        )

    def regen_prompt(self, ctx: StageContext, prev_output: dict, fixes: list[Fix]) -> str:
        diag = render_fixes_block(fixes)
        return (
            f"Your previous critic verdict was malformed. Fix it.\n\n"
            f"Previous output:\n{prev_output!r}\n\n"
            f"{diag}\n"
            f"Return ONLY a JSON object with the correct shape: "
            f'{{"verdict": "SHIP|FIX|BLOCK", "weakest_param": "...", '
            f'"fixes": [...]}}.'
        )

    # ------------------------------------------------------------------
    # Validation — verdict shape only; the FIXES themselves get routed
    # by fix_router.cascade() at the runner level.
    # ------------------------------------------------------------------

    def validate(self, ctx: StageContext, output: dict) -> list[Fix]:
        fixes: list[Fix] = []
        verdict = output.get("verdict")
        if verdict not in _VALID_VERDICTS:
            fixes.append(Fix(
                constraint="verdict_field",
                reason=f"verdict must be one of {_VALID_VERDICTS!r}, got {verdict!r}",
                target_stage=self.name,
                target_path="critic.verdict",
                source_judge="critic_contract",
            ))
            return fixes

        if verdict == "FIX":
            raw_fixes = output.get("fixes")
            if not isinstance(raw_fixes, list) or not raw_fixes:
                fixes.append(Fix(
                    constraint="fixes_shape",
                    reason="FIX verdict requires a non-empty 'fixes' list",
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
