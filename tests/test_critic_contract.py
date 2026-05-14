"""Tests for ``pipeline.llm.contracts.critic_contract.CriticContract``.

Pinning the per-axis verdict-derivation behaviour added 2026-05-14
to fix the rubber-stamp bug. The contract MUST overwrite the LLM's
self-reported ``verdict`` with the axes-derived verdict (any axis ≤
3 → BLOCK; any axis < 7 → FIX; all ≥ 7 → SHIP) — see
``pipeline/llm/critic_axes.py``.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

from pipeline.llm import critic_axes
from pipeline.llm.contracts.critic_contract import CriticContract


@dataclass
class _StubCtx:
    """Minimal StageContext stand-in for contract tests."""
    upstream: dict


def _good_axes(**overrides):
    a = {
        "hook_strength": 8, "caption_legibility": 8,
        "cast_continuity": 8, "mute_mode_score": 8,
        "source_fidelity": 8, "closer_strength": 8,
    }
    a.update(overrides)
    return a


class GatherConstraintsTest(unittest.TestCase):

    def test_constraints_include_axes_field(self):
        c = CriticContract()
        names = [con.name for con in c.gather_constraints(_StubCtx({}))]
        self.assertIn("axes_field", names,
                      "axes_field constraint missing — pipeline can't gate")
        self.assertIn("verdict_field", names)
        self.assertIn("fixes_shape", names)

    def test_axes_field_mentions_every_axis(self):
        c = CriticContract()
        constraints = c.gather_constraints(_StubCtx({}))
        axes_con = next(con for con in constraints if con.name == "axes_field")
        for name in critic_axes.AXIS_NAMES:
            self.assertIn(name, axes_con.description)

    def test_axes_field_mentions_gating_rule(self):
        # The constraint description must spell out the gating rule
        # so the LLM knows that scoring honestly is the only path.
        c = CriticContract()
        constraints = c.gather_constraints(_StubCtx({}))
        axes_con = next(con for con in constraints if con.name == "axes_field")
        # Either spelled with words or symbols.
        self.assertTrue(
            "BLOCK" in axes_con.description and "FIX" in axes_con.description,
            "axes_field must explain BLOCK/FIX gates",
        )


class BuildPromptTest(unittest.TestCase):

    def test_prompt_includes_axes_block(self):
        c = CriticContract()
        ctx = _StubCtx({
            "script": {"narration": "test narration"},
            "cast": {"protagonist": {"description": "a person"}},
            "mp4_uri": "gs://bucket/video.mp4",
            "frame_uris": ["gs://bucket/f0.png", "gs://bucket/f1.png"],
        })
        prompt = c.build_prompt(ctx, c.gather_constraints(ctx))
        for name in critic_axes.AXIS_NAMES:
            self.assertIn(name, prompt,
                          f"axis {name!r} missing from prompt")

    def test_prompt_includes_gating_rule(self):
        c = CriticContract()
        ctx = _StubCtx({"script": {}, "cast": {}})
        prompt = c.build_prompt(ctx, c.gather_constraints(ctx))
        self.assertIn("GATING RULE", prompt)
        self.assertIn("BLOCK", prompt)
        self.assertIn("SHIP", prompt)

    def test_prompt_handles_missing_frame_uris(self):
        c = CriticContract()
        ctx = _StubCtx({"script": {}, "cast": {}})
        prompt = c.build_prompt(ctx, c.gather_constraints(ctx))
        self.assertIn("not provided", prompt)

    def test_prompt_handles_long_narration(self):
        c = CriticContract()
        # 3000-char narration — should be truncated to 2000.
        long_narr = "x" * 3000
        ctx = _StubCtx({
            "script": {"narration": long_narr}, "cast": {},
        })
        prompt = c.build_prompt(ctx, c.gather_constraints(ctx))
        # 2000 'x's should appear; not 3000.
        self.assertIn("x" * 2000, prompt)
        self.assertNotIn("x" * 2001, prompt)


class RegenPromptTest(unittest.TestCase):

    def test_regen_lists_all_axes(self):
        c = CriticContract()
        ctx = _StubCtx({})
        prompt = c.regen_prompt(ctx, prev_output={"bad": "shape"}, fixes=[])
        for name in critic_axes.AXIS_NAMES:
            self.assertIn(name, prompt)


class ValidateTest(unittest.TestCase):

    def setUp(self):
        self.contract = CriticContract()
        self.ctx = _StubCtx({})

    def test_axes_missing_yields_fix_constraint(self):
        out = {"verdict": "SHIP"}  # no axes
        fixes = self.contract.validate(self.ctx, out)
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0].constraint, "axes_field")
        # Verdict mutated to FIX (axes-derived; the LLM said SHIP but
        # we cannot trust SHIP without axes).
        self.assertEqual(out["verdict"], "FIX")
        self.assertEqual(out["verdict_llm"], "SHIP")

    def test_axes_present_but_not_dict_yields_fix(self):
        out = {"axes": [7, 7, 7, 7, 7, 7], "verdict": "SHIP"}
        fixes = self.contract.validate(self.ctx, out)
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0].constraint, "axes_field")
        self.assertEqual(out["verdict"], "FIX")

    def test_per_axis_non_int_yields_fix_per_bad_axis(self):
        out = {
            "axes": _good_axes(hook_strength="high", cast_continuity=None),
            "verdict": "SHIP",
        }
        fixes = self.contract.validate(self.ctx, out)
        # 2 bad axes → 2 axes_field fixes (defence-in-depth so the
        # regen prompt names every broken axis).
        self.assertEqual(len([f for f in fixes if f.constraint == "axes_field"]), 2)

    def test_all_axes_pass_yields_ship_with_no_fixes(self):
        out = {
            "axes": _good_axes(),
            "verdict": "SHIP",
            "fixes": [],
        }
        fixes = self.contract.validate(self.ctx, out)
        self.assertEqual(fixes, [])
        self.assertEqual(out["verdict"], "SHIP")
        self.assertEqual(out["verdict_llm"], "SHIP")

    def test_one_axis_below_seven_forces_fix_verdict(self):
        out = {
            "axes": _good_axes(caption_legibility=4),
            # LLM tries SHIP — should be overridden.
            "verdict": "SHIP",
            "fixes": [{
                "target_stage": "compose",
                "target_path": "captions",
                "constraint": "caption_overlay",
                "severity": "error",
                "reason": "captions invisible",
            }],
        }
        self.contract.validate(self.ctx, out)
        self.assertEqual(out["verdict"], "FIX")
        self.assertEqual(out["verdict_llm"], "SHIP")

    def test_axis_below_four_forces_block_verdict(self):
        out = {
            "axes": _good_axes(cast_continuity=2),
            "verdict": "SHIP",
        }
        self.contract.validate(self.ctx, out)
        self.assertEqual(out["verdict"], "BLOCK")
        self.assertEqual(out["verdict_llm"], "SHIP")

    def test_fix_verdict_with_empty_fixes_list_yields_constraint(self):
        # If derived verdict is FIX, the fixes list must be non-empty
        # so the runner has something to cascade.
        out = {
            "axes": _good_axes(hook_strength=5),
            "verdict": "FIX",
            # No fixes provided
        }
        result = self.contract.validate(self.ctx, out)
        # Should yield a fixes_shape constraint failure
        self.assertTrue(any(f.constraint == "fixes_shape" for f in result),
                        "FIX verdict with no fixes must trigger fixes_shape")

    def test_fix_verdict_with_malformed_fix_yields_constraint(self):
        out = {
            "axes": _good_axes(hook_strength=5),
            "verdict": "FIX",
            "fixes": [{"target_stage": "rewrite"}],  # missing required fields
        }
        result = self.contract.validate(self.ctx, out)
        self.assertTrue(any(f.constraint == "fixes_shape" for f in result))
        # The reason should mention which fields are missing.
        bad = next(f for f in result if f.constraint == "fixes_shape")
        self.assertIn("target_path", bad.reason)

    def test_block_verdict_does_not_require_fixes_list(self):
        # BLOCK = irrecoverable; runner doesn't cascade fixes anyway.
        out = {
            "axes": _good_axes(cast_continuity=1),
            "verdict": "SHIP",  # LLM tried SHIP
            "fixes": [],
        }
        result = self.contract.validate(self.ctx, out)
        self.assertEqual(out["verdict"], "BLOCK")
        # No fixes_shape constraint (BLOCK doesn't need them).
        self.assertFalse(any(f.constraint == "fixes_shape" for f in result),
                         "BLOCK verdict should not require fixes list")

    def test_validate_preserves_axes_in_output(self):
        # Sanity: validate() doesn't strip axes.
        out = {"axes": _good_axes(), "verdict": "SHIP", "fixes": []}
        self.contract.validate(self.ctx, out)
        self.assertEqual(out["axes"], _good_axes())


if __name__ == "__main__":
    unittest.main()
