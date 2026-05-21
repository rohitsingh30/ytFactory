"""Tests for ``pipeline.llm.critic_axes`` — the per-axis verdict gate.

These tests pin the SHIP/FIX/BLOCK threshold logic that was added on
2026-05-14 to fix the critic rubber-stamp bug (27 of 27 rendered jobs
shipped with verdict=SHIP despite obvious bugs).

The shared module is imported by both the legacy ``critique_short``
path AND the contract-driven ``CriticContract`` path, so these tests
also indirectly cover both call sites.
"""

from __future__ import annotations

import unittest

from pipeline.llm import critic_axes


_FULL_AXES_GOOD = {
    "hook_strength": 8,
    "caption_legibility": 9,
    "cast_continuity": 8,
    "mute_mode_score": 7,
    "source_fidelity": 8,
    "closer_strength": 7,
}


def _axes(**overrides):
    """Convenience: full axes dict with selected overrides."""
    a = dict(_FULL_AXES_GOOD)
    a.update(overrides)
    return a


class DeriveVerdictTest(unittest.TestCase):

    def test_all_axes_at_ship_min_yields_ship(self):
        axes = _axes(**{name: 7 for name in critic_axes.REQUIRED_AXIS_NAMES})
        self.assertEqual(critic_axes.derive_verdict(axes), "SHIP")

    def test_all_axes_at_ten_yields_ship(self):
        axes = _axes(**{name: 10 for name in critic_axes.REQUIRED_AXIS_NAMES})
        self.assertEqual(critic_axes.derive_verdict(axes), "SHIP")

    def test_one_axis_at_six_yields_fix(self):
        axes = _axes(caption_legibility=6)
        self.assertEqual(critic_axes.derive_verdict(axes), "FIX")

    def test_one_axis_at_four_yields_fix(self):
        axes = _axes(cast_continuity=4)
        self.assertEqual(critic_axes.derive_verdict(axes), "FIX")

    def test_one_axis_at_three_yields_block(self):
        # The cake-AITA / Ronaldinho situation — gibberish captions
        # are ≤3 caption_legibility; we want BLOCK, not FIX.
        axes = _axes(caption_legibility=3)
        self.assertEqual(critic_axes.derive_verdict(axes), "BLOCK")

    def test_one_axis_at_one_yields_block(self):
        axes = _axes(source_fidelity=1)
        self.assertEqual(critic_axes.derive_verdict(axes), "BLOCK")

    def test_block_dominates_fix(self):
        # If one axis is 3 and another is 5, BLOCK wins (the BLOCK
        # axis is irrecoverable; no point trying to FIX).
        axes = _axes(cast_continuity=3, hook_strength=5)
        self.assertEqual(critic_axes.derive_verdict(axes), "BLOCK")

    def test_missing_axes_dict_yields_fix(self):
        # Sentinel — LLM emitted no axes at all → not a SHIP.
        self.assertEqual(critic_axes.derive_verdict(None), "FIX")
        self.assertEqual(critic_axes.derive_verdict({}), "FIX")

    def test_partial_axes_yields_fix(self):
        # Even if all PRESENT axes are 10/10, missing one means we
        # can't make a shipping decision → FIX (re-prompt the LLM).
        axes = {"hook_strength": 10, "caption_legibility": 10}
        self.assertEqual(critic_axes.derive_verdict(axes), "FIX")

    def test_non_int_axis_yields_fix(self):
        # LLM hallucinated a string ("high") for an axis. Don't
        # silently coerce — re-prompt.
        axes = _axes(hook_strength="high")  # type: ignore[arg-type]
        self.assertEqual(critic_axes.derive_verdict(axes), "FIX")

    def test_float_axis_yields_fix(self):
        axes = _axes(hook_strength=7.5)  # type: ignore[arg-type]
        self.assertEqual(critic_axes.derive_verdict(axes), "FIX")

    def test_bool_not_treated_as_int(self):
        # bool is an int subclass in Python — explicitly reject so
        # ``True`` doesn't get treated as 1 → BLOCK by accident.
        axes = _axes(hook_strength=True)  # type: ignore[arg-type]
        self.assertEqual(critic_axes.derive_verdict(axes), "FIX")

    def test_non_mapping_yields_fix(self):
        self.assertEqual(critic_axes.derive_verdict([7, 8, 9, 7, 8, 7]), "FIX")
        self.assertEqual(critic_axes.derive_verdict("SHIP"), "FIX")
        self.assertEqual(critic_axes.derive_verdict(7), "FIX")


class WeakestAxisTest(unittest.TestCase):

    def test_returns_lowest_axis(self):
        axes = _axes(closer_strength=4, hook_strength=10)
        result = critic_axes.weakest_axis(axes)
        self.assertEqual(result, ("closer_strength", 4))

    def test_returns_first_in_tie(self):
        # Tie-broken by AXIS_NAMES order (stable, deterministic).
        axes = _axes(hook_strength=4, caption_legibility=4)
        result = critic_axes.weakest_axis(axes)
        # hook_strength is first in AXES so wins the tie.
        self.assertEqual(result, ("hook_strength", 4))

    def test_missing_axes_returns_none(self):
        self.assertIsNone(critic_axes.weakest_axis(None))
        self.assertIsNone(critic_axes.weakest_axis({}))

    def test_partial_axes_returns_none(self):
        # If we can't score every REQUIRED axis, no single one is the
        # "weakest" — caller decides what to do.
        partial = {name: 7 for name in critic_axes.REQUIRED_AXIS_NAMES[:3]}
        self.assertIsNone(critic_axes.weakest_axis(partial))

    def test_empty_axis_names_yields_none(self):
        # Defensive: if the REQUIRED_AXIS_NAMES tuple were ever empty,
        # the parsed list would be empty and we'd reach the
        # "if not parsed" branch. Simulate by patching the module.
        original_req = critic_axes.REQUIRED_AXIS_NAMES
        original_opt = critic_axes.OPTIONAL_AXES
        try:
            critic_axes.REQUIRED_AXIS_NAMES = ()  # type: ignore[misc]
            critic_axes.OPTIONAL_AXES = frozenset()  # type: ignore[misc]
            self.assertIsNone(critic_axes.weakest_axis({"any": 7}))
        finally:
            critic_axes.REQUIRED_AXIS_NAMES = original_req  # type: ignore[misc]
            critic_axes.OPTIONAL_AXES = original_opt  # type: ignore[misc]


class AxesSummaryTest(unittest.TestCase):

    def test_summary_lists_all_axes_in_order(self):
        s = critic_axes.axes_summary(_FULL_AXES_GOOD)
        for name in critic_axes.REQUIRED_AXIS_NAMES:
            self.assertIn(f"{name}=", s)
        # First REQUIRED axis listed comes first in the string.
        first = critic_axes.REQUIRED_AXIS_NAMES[0]
        last = critic_axes.REQUIRED_AXIS_NAMES[-1]
        self.assertLess(s.index(first), s.index(last))

    def test_missing_returns_no_axes_sentinel(self):
        self.assertEqual(critic_axes.axes_summary(None), "<no axes>")
        self.assertEqual(critic_axes.axes_summary("nope"), "<no axes>")

    def test_missing_individual_axis_marked_question(self):
        partial = {name: 7 for name in critic_axes.REQUIRED_AXIS_NAMES[:3]}
        s = critic_axes.axes_summary(partial)
        # Missing axes are marked with "=?".
        self.assertIn("=?", s)


class JsonSchemaTest(unittest.TestCase):

    def test_schema_requires_every_required_axis(self):
        sch = critic_axes.axes_json_schema()
        self.assertEqual(sch["type"], "object")
        # Only REQUIRED axes are LLM-facing required keys; optional
        # axes (e.g. vbench_score) are populated by the CPU adapter.
        self.assertSetEqual(
            set(sch["required"]), set(critic_axes.REQUIRED_AXIS_NAMES),
            "every required axis must be required at the schema level",
        )

    def test_schema_exposes_optional_axes_as_properties_not_required(self):
        sch = critic_axes.axes_json_schema()
        for opt in critic_axes.OPTIONAL_AXES:
            # Property is still defined — the schema accepts a value
            # there when the adapter writes one — but it's NOT in
            # ``required`` so an LLM response that omits it validates.
            self.assertIn(opt, sch["properties"])
            self.assertNotIn(opt, sch["required"])

    def test_schema_constrains_axis_range_1_to_10(self):
        sch = critic_axes.axes_json_schema()
        for name in critic_axes.AXIS_NAMES:
            prop = sch["properties"][name]
            self.assertEqual(prop["type"], "integer")
            self.assertEqual(prop["minimum"], 1)
            self.assertEqual(prop["maximum"], 10)

    def test_schema_blocks_extra_keys(self):
        # additionalProperties: false — LLM can't smuggle in a
        # surprise axis to game the gate.
        sch = critic_axes.axes_json_schema()
        self.assertFalse(sch["additionalProperties"])


class RenderAxesBlockTest(unittest.TestCase):

    def test_block_lists_every_required_axis_with_description(self):
        block = critic_axes.render_axes_block()
        for name, desc in critic_axes.AXES:
            if name in critic_axes.OPTIONAL_AXES:
                continue
            self.assertIn(name, block,
                          f"axis name {name!r} missing from prompt block")
            # First sentence of description should appear.
            first_sentence = desc.split(".")[0][:40]
            self.assertIn(first_sentence, block)

    def test_block_excludes_optional_axes(self):
        # Optional axes are populated by the CPU adapter, not the LLM,
        # and asking the LLM to score them would either hallucinate
        # a number or break every critic call when the adapter is
        # unavailable.
        block = critic_axes.render_axes_block()
        for opt in critic_axes.OPTIONAL_AXES:
            self.assertNotIn(
                opt, block,
                f"optional axis {opt!r} leaked into LLM prompt block",
            )

    def test_block_uses_default_indent(self):
        block = critic_axes.render_axes_block()
        for line in block.splitlines():
            if line.strip().startswith("- "):
                # Default indent is 2 spaces.
                self.assertTrue(line.startswith("  - "),
                                f"unexpected indent: {line!r}")

    def test_block_respects_custom_indent(self):
        block = critic_axes.render_axes_block(indent="    ")
        first = next(
            line for line in block.splitlines() if line.strip().startswith("- ")
        )
        self.assertTrue(first.startswith("    - "))


class OptionalAxisVbenchTest(unittest.TestCase):
    """Pins the contract that ``vbench_score`` is an OPTIONAL axis —
    its absence does not block ship, but its presence is gated like
    any other axis. Added 2026-05-15 with the VBench adapter."""

    def test_vbench_score_is_optional_axis(self):
        self.assertIn("vbench_score", critic_axes.OPTIONAL_AXES)
        self.assertNotIn("vbench_score", critic_axes.REQUIRED_AXIS_NAMES)
        # AXIS_NAMES is the union — vbench_score still appears there.
        self.assertIn("vbench_score", critic_axes.AXIS_NAMES)

    def test_missing_vbench_does_not_block_ship(self):
        # Cloud worker without vbench installed → adapter writes no
        # axis. The other 6 axes must still be able to SHIP.
        axes = _axes()  # no vbench_score
        self.assertEqual(critic_axes.derive_verdict(axes), "SHIP")

    def test_low_vbench_score_acts_as_fix_gate(self):
        # VBench raw < 70 (i.e. 1-10 axis < 7) → FIX.
        axes = _axes(vbench_score=6)
        self.assertEqual(critic_axes.derive_verdict(axes), "FIX")

    def test_very_low_vbench_score_acts_as_block_gate(self):
        # VBench raw < 40 (i.e. 1-10 axis ≤ 3) → BLOCK.
        axes = _axes(vbench_score=3)
        self.assertEqual(critic_axes.derive_verdict(axes), "BLOCK")

    def test_high_vbench_score_ships(self):
        axes = _axes(vbench_score=9)
        self.assertEqual(critic_axes.derive_verdict(axes), "SHIP")

    def test_malformed_vbench_score_when_present_yields_fix(self):
        # Adapter mis-emitted a string → FIX so we notice the
        # regression rather than silently shipping.
        axes = _axes(vbench_score="high")  # type: ignore[arg-type]
        self.assertEqual(critic_axes.derive_verdict(axes), "FIX")

    def test_bool_vbench_score_when_present_yields_fix(self):
        # Same bool-is-int rejection as the required axes.
        axes = _axes(vbench_score=True)  # type: ignore[arg-type]
        self.assertEqual(critic_axes.derive_verdict(axes), "FIX")

    def test_weakest_axis_returns_vbench_when_present_and_lowest(self):
        axes = _axes(vbench_score=4)
        self.assertEqual(
            critic_axes.weakest_axis(axes),
            ("vbench_score", 4),
        )

    def test_weakest_axis_skips_missing_optional_axis(self):
        # All required axes ≥ 7, vbench absent → weakest is the
        # lowest of the required axes, NOT None and NOT vbench_score.
        axes = _axes(mute_mode_score=7)  # forced lowest
        result = critic_axes.weakest_axis(axes)
        assert result is not None
        self.assertIn(result[0], critic_axes.REQUIRED_AXIS_NAMES)


if __name__ == "__main__":
    unittest.main()
