"""PromptsContract — Stage 4 (per-beat prompts) as an orchestrated stage.

Ports the existing ``pipeline.llm.prompts.author_beat_prompts`` to the
contract pattern. Pulls constraints from
:mod:`pipeline.llm.prompt_lint`, embeds them in the prompt, validates
inline, regenerates on error.

Closes the v5-smoke regression directly: the renderer logged
``[prompts] WARNING: failed to author prompts: expected JSON array,
got dict`` and silently fell back to heuristic prompts. Heuristics
strip cast-lock + kit-lock metadata, so the diffusion model paints
the wrong character on every beat. With this contract the
orchestrator catches the shape error on attempt 1 and regenerates —
the renderer keeps the LLM-authored prompts that respect the cast.

Per-beat fanout is a future Phase work; for now this contract
authors all N beats in a single LLM call (matching the legacy code's
shape) and leaves per-beat parallelism for the pipeline runner to
wire when it owns the prompts stage end-to-end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .. import prompt_lint
from ..fix import Fix
from ..orchestrator import Constraint, StageContext
from .base import render_constraints_block, render_fixes_block


@dataclass
class PromptsContract:
    """Orchestrated per-beat prompt author. See module docstring."""

    name: str = "prompts"
    fanout: int = 1   # legacy single-call shape; per-beat fanout TBD
    artifact_paths: tuple[str, ...] = ("beats[*].prompt", "beats[*].scene")

    def gather_constraints(self, ctx: StageContext) -> list[Constraint]:
        upstream = ctx.upstream or {}
        beats = upstream.get("beats") or []
        n = len(beats) if isinstance(beats, list) else 0

        return [
            Constraint(
                name="prompts_array",
                severity="error",
                description=(
                    "Output MUST be a JSON ARRAY (not an object) of "
                    f"exactly {n} items, one per beat in beat order. "
                    "The renderer aligns prompts to beats by INDEX. "
                    "If you wrap the array in an object the renderer "
                    "falls back to heuristic prompts and the LLM-"
                    "authored cast lock is lost on every beat."
                ),
                examples_good=(
                    f'[{{"key_visual": "...", "scene": "...", "narration_line": "..."}},'
                    f' ... {n} items total ...]',
                ),
                examples_bad=(
                    '{"prompts": [...]}                  // WRONG — wraps in object',
                    '{"beats": [...]}                    // WRONG — wraps in object',
                    '{"0": {...}, "1": {...}}            // WRONG — keyed object',
                ),
            ),
            Constraint(
                name="prompts_count_mismatch",
                severity="error",
                description=(
                    f"Array MUST have exactly {n} items. The renderer "
                    f"crashes if prompts.length != beats.length."
                ),
            ),
            Constraint(
                name="prompt_item_object",
                severity="error",
                description=(
                    "Every array item MUST be an object with at minimum "
                    "a 'scene' field (string, non-empty). Optional: "
                    "'key_visual', 'narration_line'."
                ),
            ),
            Constraint(
                name="prompt_text_bait",
                severity="warning",
                description=(
                    "Per-beat scene descriptions MUST NOT include "
                    "text-bait tokens (text, writing, letters, label, "
                    "logo, wording, caption, subtitle). SDXL/FLUX "
                    "render these as in-image gibberish text. Describe "
                    "shape, colour, and icon instead."
                ),
                examples_bad=(
                    "Birthday cake with text saying \"Happy Birthday\"",
                    "Sign with the word REFUSED in red letters",
                ),
                examples_good=(
                    "Birthday cake with three lit candles, pink frosting",
                    "Hand holding a red stop-sign-shaped icon",
                ),
            ),
            Constraint(
                name="prompt_meta_imperative",
                severity="warning",
                description=(
                    "Don't echo the meta-instruction back. Each item.scene "
                    "should DESCRIBE the scene, not start with phrases "
                    "like \"Generate an image of…\" or \"Replace hook "
                    "visual with…\"."
                ),
            ),
            Constraint(
                name="prompt_cast_drift",
                severity="warning",
                description=(
                    "Each beat's scene MUST only use character names "
                    "from the cast (narrator + supporting). Don't "
                    "invent names or misspell them — the renderer "
                    "uses cast names to look up the visual lock."
                ),
            ),
        ]

    def build_prompt(self, ctx: StageContext, constraints: list[Constraint]) -> str:
        """Build the first-attempt prompts prompt.

        Pulls Script + Cast from upstream so the model sees the
        narration text, the beat split, the cast-lock visual
        descriptions, and the channel style prefix in one place.
        """
        upstream = ctx.upstream or {}
        script = upstream.get("script") or {}
        cast = upstream.get("cast") or {}
        beats = upstream.get("beats") or []

        cfg = ctx.channel_cfg or {}
        style_prefix = (cfg.get("image_style_prefix") or "").strip()

        rules_block = render_constraints_block(constraints)
        cast_block = self._format_cast_block(cast)
        beats_block = self._format_beats_block(beats)

        return (
            "You are authoring per-beat image prompts for a YouTube Shorts "
            "render. The renderer turns each prompt into ONE image; there's "
            "one image per beat.\n\n"
            "CHANNEL AESTHETIC (locked — every beat must match):\n"
            f'"""\n{style_prefix[:1500]}\n"""\n\n'
            "CAST LOCK (paste these descriptions verbatim into beats where "
            "the character appears — diffusion drift across beats is the "
            "single biggest quality bug):\n"
            f"{cast_block}\n"
            f"{rules_block}\n"
            "BEATS (in order — produce ONE prompt per beat at the same index):\n"
            f"{beats_block}\n"
            "Return ONLY a JSON array (no prose, no markdown fences, no "
            f"object wrapper). The array MUST have exactly {len(beats)} "
            "items. Each item:\n"
            "  {\n"
            '    "key_visual": "<one-line summary of the dominant subject>",\n'
            '    "scene": "<full diffusion prompt: subject + composition + '
            'lighting + style — 30-80 words>",\n'
            '    "narration_line": "<the beat\'s text verbatim, for caption '
            'alignment>"\n'
            "  }"
        )

    def regen_prompt(
        self,
        ctx: StageContext,
        prev_output: dict,
        fixes: list[Fix],
    ) -> str:
        """Diff-style regen — the model is much better at "fix this one
        thing" than "rewrite all 19 prompts from scratch".
        """
        diag = render_fixes_block(fixes)
        # prev_output may be a list (correct shape) or dict (the bug we
        # are repairing). Show whichever it is.
        prev_repr = json.dumps(prev_output, indent=2)[:3000]
        upstream = ctx.upstream or {}
        beats = upstream.get("beats") or []

        return (
            "Your previous prompts output failed one or more rules. "
            "Rewrite it to fix every error-level rule below — keep the "
            "scene descriptions that worked; only patch the broken parts.\n\n"
            f"Previous output:\n{prev_repr}\n\n"
            f"{diag}\n"
            f"Return ONLY a JSON array of exactly {len(beats)} items "
            "(no object wrapper, no prose, no markdown fences)."
        )

    def validate(self, ctx: StageContext, output) -> list[Fix]:  # noqa: ANN001 — output may be list OR dict
        """Run prompt_lint and translate every issue into a Fix.

        Note ``output`` may be a list (the correct shape) or a dict
        (the legacy bug). The orchestrator's run_stage passes JSON-
        decoded output regardless of top-level type; we accept and
        report cleanly.
        """
        upstream = ctx.upstream or {}
        beats = upstream.get("beats") or []
        cast = upstream.get("cast")
        issues = prompt_lint.check_prompts(
            output, expected_beats=len(beats), cast=cast,
        )
        return [
            Fix(
                constraint=i.code,
                reason=i.message,
                severity=i.severity,
                target_stage=self.name,
                target_path=i.target_path,
                source_judge="prompt_lint",
            )
            for i in issues
        ]

    # ------------------------------------------------------------------
    # Block formatters
    # ------------------------------------------------------------------

    @staticmethod
    def _format_cast_block(cast: dict) -> str:
        if not cast:
            return "  (no cast.json — channel doesn't use cast lock)"
        narr = cast.get("narrator") or {}
        sup = cast.get("supporting") or []
        lines = [f"  Narrator: {narr.get('description', '<missing>')}"]
        for s in sup:
            if not isinstance(s, dict):
                continue
            name = s.get("name") or "?"
            aliases = ", ".join(s.get("aliases") or [])
            desc = s.get("description") or "<missing>"
            lines.append(f"  {name} (aka {aliases}): {desc}"
                         if aliases else f"  {name}: {desc}")
        return "\n".join(lines)

    @staticmethod
    def _format_beats_block(beats: list) -> str:
        if not beats:
            return "  (no beats — bug)"
        lines = []
        for b in beats:
            if isinstance(b, dict):
                idx = b.get("index", "?")
                text = (b.get("text") or "").strip()
            else:
                # BeatArtifact dataclass
                idx = getattr(b, "index", "?")
                text = (getattr(b, "text", None) or "").strip()
            lines.append(f"  beat {idx}: {text}")
        return "\n".join(lines)
