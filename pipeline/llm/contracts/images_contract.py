"""ImagesContract — per-beat image generation as an orchestrated stage.

Wraps the cloud image-gen call. Per-beat fanout — the runner invokes
this contract once per beat in :class:`pipeline.llm.render_job.RenderJob`,
so each beat gets its own cache key (beat-14 regen costs 1 image, not
30) and its own retry budget.

Two-layer validation:

1. :mod:`pipeline.llm.image_lint` — cheap deterministic gates
   (blank/black/white frames, file-too-small, aspect mismatch). Catches
   broken outputs before spending tokens on the vision judge.

2. :class:`pipeline.llm.judges.image_judge.ImageJudge` — vision LLM
   that compares the rendered PNG against the beat text + cast lock
   and emits :class:`pipeline.llm.fix.Fix` objects with
   ``constraint=cast_drift`` / ``constraint=beat_mismatch`` /
   ``constraint=text_in_image_gibberish``. The judge runs as its OWN
   stage today (it's an LLM) so this contract just calls image-gen +
   image_lint; the judge fires after compose in the post-render
   critic loop and routes its Fixes back here for per-beat regen.

NOTE: the actual HTTP call to the cloud image service still lives in
``pipeline.images.images_cloudrun`` — this contract calls into it.
The orchestrator wins (cache, retry, Fix routing) all happen at the
contract layer; the wire protocol stays untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import image_lint
from ..fix import Fix
from ..orchestrator import Constraint, StageContext
from .base import render_constraints_block, render_fixes_block


@dataclass
class ImagesContract:
    """Orchestrated per-beat image generation. See module docstring."""

    name: str = "images"
    fanout: str = "beats"
    artifact_paths: tuple[str, ...] = ("beats[*].image",)

    def gather_constraints(self, ctx: StageContext) -> list[Constraint]:
        """Constraints sourced from image_lint + cast lock + kit lock.

        Errors gate the orchestrator's retry loop. Warnings (aspect
        mismatch, soft style hits) pass through to the StageResult.
        """
        cfg = ctx.channel_cfg or {}
        upstream = ctx.upstream or {}
        cast = upstream.get("cast") or {}
        kit_lock = (cfg.get("kit_lock") or "").strip()
        style_prefix = (cfg.get("image_style_prefix") or "").strip()

        return [
            Constraint(
                name="image_blank_black",
                severity="error",
                description=(
                    "The generated image MUST not be all-black. A black "
                    "frame is the silent-failure sentinel from the GPU "
                    "service — re-render with a different seed."
                ),
            ),
            Constraint(
                name="image_blank_white",
                severity="error",
                description=(
                    "The generated image MUST not be all-white. White "
                    "frames are the safety filter blanking the output — "
                    "rephrase the scene to avoid the trigger."
                ),
            ),
            Constraint(
                name="image_too_small",
                severity="error",
                description=(
                    "Image file MUST be at least 5KB. Smaller files are "
                    "corrupt streams or generation failures."
                ),
            ),
            Constraint(
                name="cast_lock",
                severity="warning",
                description=(
                    "The protagonist MUST look the same as in cast.narrator: "
                    f"{(cast.get('narrator', {}) or {}).get('description', '<no cast>')[:200]}. "
                    "Same hair, same wardrobe, same age. Drift across "
                    "beats is the single biggest quality bug."
                ),
            ),
            Constraint(
                name="kit_lock",
                severity="warning",
                description=(
                    f"Channel kit-lock token MUST appear visually: "
                    f"{kit_lock[:200] if kit_lock else '<none configured>'}"
                ),
            ),
            Constraint(
                name="style_lock",
                severity="warning",
                description=(
                    f"Channel aesthetic MUST be present (locked style "
                    f"prefix, abridged): {style_prefix[:300]}"
                ),
            ),
        ]

    def build_prompt(
        self,
        ctx: StageContext,
        constraints: list[Constraint],
    ) -> str:
        """Return the diffusion prompt for this beat.

        Unlike text-generation contracts, this 'prompt' goes to the GPU
        image service, not an LLM. We assemble the prompt from upstream:
        the per-beat ``scene`` (from PromptsContract), the cast-lock
        narrator description verbatim, the channel style prefix, and
        the kit-lock token.

        For symmetry with the StageContract Protocol we still return a
        string. The pipeline runner's external-handler dispatcher knows
        the ``images`` stage is ``kind="external"`` and routes the
        string + cache key to the cloud image client instead of an LLM.
        """
        upstream = ctx.upstream or {}
        beats = upstream.get("beats") or []
        sub = ctx.sub_index
        if sub is None or sub >= len(beats):
            raise ValueError(
                f"images contract called with invalid sub_index={sub} "
                f"(beats has {len(beats)} entries)"
            )
        beat = beats[sub]
        scene = (beat.get("prompt") if isinstance(beat, dict)
                 else getattr(beat, "prompt", "")) or ""
        cast = upstream.get("cast") or {}
        narrator_desc = (cast.get("narrator", {}) or {}).get("description", "")

        cfg = ctx.channel_cfg or {}
        style_prefix = (cfg.get("image_style_prefix") or "").strip()
        kit_lock = (cfg.get("kit_lock") or "").strip()

        # Order matters for diffusion attention: subject first, then
        # cast lock (so the protagonist locks early in the embedding),
        # then style + kit lock as anchors.
        parts = [scene.strip()]
        if narrator_desc:
            parts.append(narrator_desc.strip())
        if style_prefix:
            parts.append(style_prefix)
        if kit_lock:
            parts.append(kit_lock)
        return ". ".join(p for p in parts if p)

    def regen_prompt(
        self,
        ctx: StageContext,
        prev_output,  # noqa: ANN001 — image bytes-or-uri, not text
        fixes: list[Fix],
    ) -> str:
        """Augment the diffusion prompt with avoid-clauses based on Fixes.

        The model has no way to "see" its previous output for diff-
        style regen (we'd have to send the rendered image back to a
        vision model — that's the ImageJudge's job). What we CAN do is
        change the prompt to avoid the failure mode the lint / judge
        flagged:

        - ``image_blank_white`` → likely safety-filter trigger; the
          regen prompt strips suspect tokens and re-tries with a
          different seed (set by the cache key — different attempt
          number → different key → fresh seed).
        - ``cast_drift`` → re-emphasise the cast description verbatim
          + add "MAINTAIN consistent character across all frames".
        - ``image_blank_black`` → usually transient infra; same prompt,
          new attempt, the cache key handles freshness.

        For now the regen prompt is the original prompt + a NEGATIVE-
        like postscript naming what to avoid; future work feeds the
        rendered image back to an inpainting model.
        """
        base = self.build_prompt(ctx, [])
        avoidances: list[str] = []
        for f in fixes:
            if f.severity != "error":
                continue
            if f.constraint in ("image_blank_white", "image_blank_black"):
                avoidances.append(
                    "ensure clear central subject, not a blank canvas"
                )
            elif f.constraint == "cast_drift":
                upstream = ctx.upstream or {}
                cast = upstream.get("cast") or {}
                narr = (cast.get("narrator", {}) or {}).get("description", "")
                if narr:
                    avoidances.append(
                        f"MAINTAIN consistent character: {narr.strip()}"
                    )
        if avoidances:
            return base + " " + " ".join(avoidances)
        return base

    def validate(
        self,
        ctx: StageContext,
        output: dict,
    ) -> list[Fix]:
        """Run image_lint on the freshly-rendered PNG.

        The pipeline runner persists the image at
        ``output['local_path']`` (set by the cloud image client). We
        open the file, run pixel gates, return Fixes per beat.

        ImageJudge runs separately (in the post-render critic loop)
        because it needs a vision LLM call — that's expensive and
        we only want it on a sampled subset of beats.
        """
        sub = ctx.sub_index
        local_path = output.get("local_path") if isinstance(output, dict) else None
        if not local_path:
            # No file to lint — the runner will surface upstream
            # generation errors (CloudRunUnavailable etc.) separately.
            return []

        issues = image_lint.check_image(
            Path(local_path),
            beat_index=sub if sub is not None else 0,
            expected_aspect=(ctx.channel_cfg or {}).get("aspect_ratio", "9:16"),
        )
        return [
            Fix(
                constraint=i.code,
                reason=i.message,
                severity=i.severity,
                target_stage=self.name,
                target_path=i.target_path,
                source_judge="image_lint",
            )
            for i in issues
        ]
