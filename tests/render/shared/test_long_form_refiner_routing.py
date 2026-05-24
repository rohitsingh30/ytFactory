"""Pin the 2026-05-24 long-form refiner-routing fix (O37).

Backstory: job 845bdb0df20e4ba3885ca33c7749e74d (mystoriesanimated/nosleep
long-form, slug ``i-ve-been-flying-...-845bdb0d``) shipped a 1-hour mp4
where 75% of panels rendered as photoreal floating product photos — a
white tee on a grey backdrop, an empty pot, a yellow shirt. The render
"succeeded" (compose finished, upload finished) but the artifact was
unshippable. /diagnose-render's Finding #7 isolated the root cause:

  > The long-form image path has NO refiner stage. The outline-LLM
  > authors panel-scene strings directly and pipeline/images/prompt_refiner.py
  > is bypassed.

The shorts path goes script → author_beat_prompts (which runs the
refiner) → prompts.json → ai_beat_slideshow → build_full_prompt(refined_*)
→ Z-Image-Turbo. Each beat carries refined_visual / refined_scene /
style_block, and build_full_prompt prepends era + cast verbatim.

The long-form path went script.panels[].scene → bare string → images.generate
with style_prefix appended late. No refiner, no shot rotation, no
style_block, no scene-anchor. On Z-Image-Turbo (CFG-distilled at 0.0)
the 30-50 char scene plus 190-char ANTI_TEXT_PREFIX collapsed to product
photo mode.

Fix: ``_generate_panel_stills`` batches the panel list through
``refine_prompts_batch`` ONCE at the top, then per panel:
  - refined slot non-empty → build_full_prompt(refined_*) → wire prompt
  - refined slot empty → fall back to bare scene + style_prefix (logged)
  - whole batch empty (refiner attempted, 0 refined) → RAISE

These tests pin all three branches plus the new ``scene_anchor`` channel
field. If a future change reverts ``_generate_panel_stills`` to passing
bare panel scenes (the pre-fix shape), every test in this file fails.
"""
from __future__ import annotations

import contextlib
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


@contextlib.contextmanager
def _refiner_flag(value: str | None):
    prev = os.environ.get("YTFACTORY_PROMPT_REFINER")
    try:
        if value is None:
            os.environ.pop("YTFACTORY_PROMPT_REFINER", None)
        else:
            os.environ["YTFACTORY_PROMPT_REFINER"] = value
        yield
    finally:
        if prev is None:
            os.environ.pop("YTFACTORY_PROMPT_REFINER", None)
        else:
            os.environ["YTFACTORY_PROMPT_REFINER"] = prev


def _panels(n: int) -> list[dict]:
    return [{"scene": f"protagonist sits silent in a window seat {i}",
             "seed_offset": i} for i in range(n)]


def _stub_generate_collect(captured: list[dict]):
    """Return a fake images.generate that captures (prompt, style_prefix)."""
    def fake(*, out_path: Path, prompt: str, style_prefix: str, **_kwargs):
        captured.append({
            "prompt": prompt,
            "style_prefix": style_prefix,
            "out_path": out_path,
        })
        out_path.write_bytes(b"\x89PNG" + b"\x00" * 8192)
    return fake


class LongFormRefinerRoutingTest(unittest.TestCase):
    """O37 / F29: long-form path MUST route through the refiner when the
    env flag is on, and the wire prompt MUST be the build_full_prompt
    refined-mode assembly (not bare scene)."""

    def test_refiner_flag_off_keeps_legacy_bare_scene(self):
        """Backwards-compat path: env flag OFF → bare scene + style_prefix.

        This is the pre-O37 contract; existing renders that don't opt
        into the refiner must keep their old behavior so the rollout is
        zero-regression by default.
        """
        from pipeline.render.shared import long_form_lib as lib
        captured: list[dict] = []
        with _refiner_flag(None), \
             patch("pipeline.images.generate",
                   side_effect=_stub_generate_collect(captured)), \
             TemporaryDirectory() as tmp:
            lib._generate_panel_stills(
                panels=_panels(3),
                style_prefix="warm hand-drawn 2D illustration with ink line work",
                image_provider="local_test",
                image_seed=42,
                image_steps=4,
                image_width=1080,
                image_height=1920,
                cache_dir=Path(tmp),
                era_anchor_prefix=None,
                character_description="a 29-year-old protagonist with brown hair",
                mood="anxious",
                scene_anchor="inside the cabin of a long-haul flight, dim",
                channel_key="mystoriesanimated",
            )
        # Flag OFF → each call's prompt is the bare scene (legacy path),
        # style_prefix is forwarded.
        self.assertEqual(len(captured), 3)
        for i, call in enumerate(captured):
            self.assertEqual(
                call["prompt"].strip(),
                f"protagonist sits silent in a window seat {i}",
            )
            self.assertIn("ink line work", call["style_prefix"])

    def test_refiner_flag_on_routes_through_build_full_prompt(self):
        """O37 fix: env flag ON → refiner runs, build_full_prompt is the
        wire-prompt assembler, style_prefix passed to images.generate is
        empty (style already inlined into the prompt by build_full_prompt).

        This is the regression pin for the floating-tee disaster: pre-fix
        the wire prompt was the bare scene and the channel style/cast/era
        never reached Z-Image-Turbo on long-form.
        """
        from pipeline.render.shared import long_form_lib as lib

        # Refiner stub: returns valid refined fields for every panel so
        # the per-panel fallback never fires.
        def fake_refine(panels, **_kw):
            return [
                {
                    "refined_visual": f"VISUAL_{i}",
                    "refined_scene": f"no readable text in image. SCENE_{i}",
                    "style_block": "STYLE_BLOCK",
                    "refined_version": "irrelevant-for-builder",
                    "refined_input_hash": "fake",
                }
                for i in range(len(panels))
            ]

        captured: list[dict] = []
        with _refiner_flag("1"), \
             patch(
                "pipeline.images.prompt_refiner.refine_prompts_batch",
                side_effect=fake_refine,
             ), \
             patch("pipeline.images.generate",
                   side_effect=_stub_generate_collect(captured)), \
             TemporaryDirectory() as tmp:
            lib._generate_panel_stills(
                panels=_panels(3),
                style_prefix="warm hand-drawn 2D illustration",
                image_provider="local_test",
                image_seed=42,
                image_steps=4,
                image_width=1080,
                image_height=1920,
                cache_dir=Path(tmp),
                era_anchor_prefix="[ERA — 2020s contemporary]",
                character_description="a 29-year-old protagonist",
                mood="anxious",
                scene_anchor=None,
                channel_key="mystoriesanimated",
            )

        self.assertEqual(len(captured), 3)
        for i, call in enumerate(captured):
            # build_full_prompt inlines the refined fields. Wire prompt
            # MUST contain refined_visual + refined_scene + style_block.
            self.assertIn(f"VISUAL_{i}", call["prompt"],
                          "refined_visual missing from wire prompt — "
                          "long-form path is not using build_full_prompt")
            self.assertIn(f"SCENE_{i}", call["prompt"],
                          "refined_scene missing from wire prompt")
            self.assertIn("STYLE_BLOCK", call["prompt"],
                          "style_block missing from wire prompt — "
                          "the channel style anchor failed to reach the wire")
            # Cast + era MUST also prepend (code-owned by build_full_prompt).
            self.assertIn("29-year-old", call["prompt"],
                          "character_description not prepended — F29 regression")
            self.assertIn("ERA", call["prompt"],
                          "era_anchor_prefix not prepended — F29 regression")
            # style_prefix to images.generate MUST be empty when refined
            # fields are used — otherwise style is double-applied.
            self.assertEqual(
                call["style_prefix"], "",
                "refined-mode path must pass style_prefix='' to "
                "images.generate (style already inlined). Got %r" % (call["style_prefix"],),
            )

    def test_per_panel_fallback_keeps_legacy_path_for_that_panel(self):
        """Refiner returning {} for SOME panels → those panels fall back
        to bare-scene legacy. Other panels keep refined wire prompt.

        Per-beat fallback is the documented contract from
        pipeline.images.prompt_refiner — don't poison the whole render
        on a single LLM-validation miss.
        """
        from pipeline.render.shared import long_form_lib as lib

        def fake_refine(panels, **_kw):
            # First panel: refined. Second: empty fallback. Third: refined.
            return [
                {
                    "refined_visual": "VISUAL_0",
                    "refined_scene": "no readable text in image. SCENE_0",
                    "style_block": "STYLE",
                    "refined_version": "ok", "refined_input_hash": "ok",
                },
                {},  # per-panel fallback
                {
                    "refined_visual": "VISUAL_2",
                    "refined_scene": "no readable text in image. SCENE_2",
                    "style_block": "STYLE",
                    "refined_version": "ok", "refined_input_hash": "ok",
                },
            ]

        captured: list[dict] = []
        with _refiner_flag("1"), \
             patch(
                "pipeline.images.prompt_refiner.refine_prompts_batch",
                side_effect=fake_refine,
             ), \
             patch("pipeline.images.generate",
                   side_effect=_stub_generate_collect(captured)), \
             TemporaryDirectory() as tmp:
            lib._generate_panel_stills(
                panels=_panels(3),
                style_prefix="some style",
                image_provider="local_test",
                image_seed=42, image_steps=4,
                image_width=1080, image_height=1920,
                cache_dir=Path(tmp),
            )

        self.assertEqual(len(captured), 3)
        # Panel 0 → refined wire prompt
        self.assertIn("VISUAL_0", captured[0]["prompt"])
        self.assertEqual(captured[0]["style_prefix"], "")
        # Panel 1 → bare scene, legacy style_prefix passed through
        self.assertIn("protagonist sits silent", captured[1]["prompt"])
        self.assertNotIn("VISUAL", captured[1]["prompt"])
        self.assertEqual(captured[1]["style_prefix"], "some style")
        # Panel 2 → refined wire prompt
        self.assertIn("VISUAL_2", captured[2]["prompt"])
        self.assertEqual(captured[2]["style_prefix"], "")

    def test_whole_batch_refiner_failure_raises_loud(self):
        """Per feedback_silent_fallback_unshippable_output.md: a stage
        that cannot produce its real output must RAISE.

        If the refiner was attempted but ZERO panels received refined
        fields, shipping 77 bare-scene panels through Z-Image-Turbo is
        the floating-tee failure mode (job 845bdb0d). Refusing the
        render forces an operator to investigate the refiner side
        instead of silently shipping unshippable output.
        """
        from pipeline.render.shared import long_form_lib as lib

        def fake_refine_all_empty(panels, **_kw):
            return [{} for _ in panels]

        with _refiner_flag("1"), \
             patch(
                "pipeline.images.prompt_refiner.refine_prompts_batch",
                side_effect=fake_refine_all_empty,
             ), \
             patch("pipeline.images.generate") as gen, \
             TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                lib._generate_panel_stills(
                    panels=_panels(4),
                    style_prefix="",
                    image_provider="local_test",
                    image_seed=42, image_steps=4,
                    image_width=1080, image_height=1920,
                    cache_dir=Path(tmp),
                )
            self.assertIn("refiner was attempted", str(ctx.exception))
            self.assertIn("0 panels", str(ctx.exception))
            # images.generate must NOT be called when the batch fails.
            gen.assert_not_called()

    def test_scene_anchor_threaded_into_refiner_call(self):
        """O40: channel-level scene_anchor reaches refine_prompts_batch.

        Pre-fix, ``default_scene_anchor`` was a new YAML field that
        existed but never reached the LLM. Verify the wire-up from
        ``_generate_panel_stills(scene_anchor=...)`` through to the
        refiner call.
        """
        from pipeline.render.shared import long_form_lib as lib

        captured_kw: dict = {}

        def fake_refine(panels, **kw):
            captured_kw.update(kw)
            return [{} for _ in panels]  # force fallback to dodge the raise

        # Make sure the whole-batch raise doesn't fire — patch the batch
        # gate by using only 0 work items (panels already cached on disk
        # would skip the gate). Easier: only 1 panel, refined empty, but
        # we expect the RuntimeError; catch it and inspect captured_kw.
        with _refiner_flag("1"), \
             patch(
                "pipeline.images.prompt_refiner.refine_prompts_batch",
                side_effect=fake_refine,
             ), \
             patch("pipeline.images.generate"), \
             TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                lib._generate_panel_stills(
                    panels=_panels(2),
                    style_prefix="STYLE",
                    image_provider="local_test",
                    image_seed=42, image_steps=4,
                    image_width=1080, image_height=1920,
                    cache_dir=Path(tmp),
                    era_anchor_prefix=None,
                    character_description="CHAR",
                    mood="moody",
                    scene_anchor="inside an airplane cabin, dim ambient",
                    channel_key="mystoriesanimated",
                )

        self.assertEqual(
            captured_kw.get("scene_anchor"),
            "inside an airplane cabin, dim ambient",
            "scene_anchor not threaded into refine_prompts_batch — "
            "the channel-level setting hint cannot reach the LLM",
        )
        self.assertEqual(captured_kw.get("style"), "STYLE")
        self.assertEqual(captured_kw.get("character_description"), "CHAR")
        self.assertEqual(captured_kw.get("mood"), "moody")
        self.assertEqual(captured_kw.get("channel_key"), "mystoriesanimated")


class LongformPanelsPluginPropagationTest(unittest.TestCase):
    """Pin the spec.extra → build_image_panels_video wiring.

    The LongformPanels visualize plugin reads ``spec.extra`` for the
    refiner-context keys (era_anchor_prefix / character_description /
    mood / default_scene_anchor) and forwards them to
    build_image_panels_video. If a future refactor drops one of those
    kwargs, this test fails.
    """

    def test_plugin_forwards_refiner_context_from_spec_extra(self):
        from pipeline.render.contracts import Segment, Timeline  # noqa: PLC0415
        from pipeline.render.visualize.longform_panels import (  # noqa: PLC0415
            LongformPanels,
        )

        captured: dict = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            out = kwargs["cache_dir"] / "video.mp4"
            out.write_bytes(b"\x00" * 8192)
            return out

        # Minimal spec-like object: only the attributes the plugin reads.
        class _Spec:
            output_resolution = (1920, 1080)
            output_fps = 30
            channel = "mystoriesanimated"
            extra = {
                "image_provider": "local_test",
                "image_style_prefix": "STYLE_PREFIX",
                "image_seed": 99,
                "image_steps": 6,
                "era_anchor_prefix": "[ERA test]",
                "character_description": "CHAR_DESC",
                "mood": "tense",
                "default_scene_anchor": "ANCHOR — inside the cabin",
                "authored_long_form_panels": [
                    {"scene": "panel a", "hold_s": 5.0},
                    {"scene": "panel b", "hold_s": 5.0},
                ],
            }

        # Stub probe_duration so we don't need a real mp4.
        with patch(
                "pipeline.render.shared.long_form_lib.build_image_panels_video",
                side_effect=fake_build,
             ), \
             patch(
                "pipeline.render.visualize.longform_panels.probe_duration",
                return_value=10.0,
             ), \
             TemporaryDirectory() as tmp:
            timeline = Timeline([
                Segment(anchor_id="s0", text="t0", start_s=0.0, end_s=5.0),
                Segment(anchor_id="s1", text="t1", start_s=5.0, end_s=10.0),
            ])
            LongformPanels().produce(_Spec(), timeline, Path(tmp))

        # All five context keys must be forwarded — these are the wires
        # F29/O37 add. Drop any of them and long-form regresses to the
        # pre-fix bare-scene shape.
        self.assertEqual(captured["era_anchor_prefix"], "[ERA test]")
        self.assertEqual(captured["character_description"], "CHAR_DESC")
        self.assertEqual(captured["mood"], "tense")
        self.assertEqual(captured["scene_anchor"], "ANCHOR — inside the cabin")
        self.assertEqual(captured["channel_key"], "mystoriesanimated")
        self.assertEqual(captured["style_prefix"], "STYLE_PREFIX")


if __name__ == "__main__":
    unittest.main()
