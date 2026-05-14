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
        axes = _axes(**{name: 7 for name in critic_axes.AXIS_NAMES})
        self.assertEqual(critic_axes.derive_verdict(axes), "SHIP")

    def test_all_axes_at_ten_yields_ship(self):
        axes = _axes(**{name: 10 for name in critic_axes.AXIS_NAMES})
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
        # If we can't score every axis, no single one is the
        # "weakest" — caller decides what to do.
        partial = {name: 7 for name in critic_axes.AXIS_NAMES[:3]}
        self.assertIsNone(critic_axes.weakest_axis(partial))

    def test_empty_axis_names_yields_none(self):
        # Defensive: if the AXIS_NAMES tuple were ever empty, the
        # parsed list would be empty and we'd reach the "if not parsed"
        # branch. Simulate by patching the module's AXIS_NAMES.
        original = critic_axes.AXIS_NAMES
        try:
            critic_axes.AXIS_NAMES = ()  # type: ignore[misc]
            self.assertIsNone(critic_axes.weakest_axis({"any": 7}))
        finally:
            critic_axes.AXIS_NAMES = original  # type: ignore[misc]


class AxesSummaryTest(unittest.TestCase):

    def test_summary_lists_all_axes_in_order(self):
        s = critic_axes.axes_summary(_FULL_AXES_GOOD)
        for name in critic_axes.AXIS_NAMES:
            self.assertIn(f"{name}=", s)
        # First axis listed comes first in the string.
        first = critic_axes.AXIS_NAMES[0]
        last = critic_axes.AXIS_NAMES[-1]
        self.assertLess(s.index(first), s.index(last))

    def test_missing_returns_no_axes_sentinel(self):
        self.assertEqual(critic_axes.axes_summary(None), "<no axes>")
        self.assertEqual(critic_axes.axes_summary("nope"), "<no axes>")

    def test_missing_individual_axis_marked_question(self):
        partial = {name: 7 for name in critic_axes.AXIS_NAMES[:3]}
        s = critic_axes.axes_summary(partial)
        # Missing axes are marked with "=?".
        self.assertIn("=?", s)


class JsonSchemaTest(unittest.TestCase):

    def test_schema_requires_every_axis(self):
        sch = critic_axes.axes_json_schema()
        self.assertEqual(sch["type"], "object")
        self.assertSetEqual(
            set(sch["required"]), set(critic_axes.AXIS_NAMES),
            "every axis must be required at the schema level",
        )

    def test_schema_constrains_axis_range_1_to_10(self):
        sch = critic_axes.axes_json_schema()
        for name in critic_axes.AXIS_NAMES:
            prop = sch["properties"][name]
            self.assertEqual(prop["type"], "integer")
            self.assertEqual(prop["minimum"], 1)
            self.assertEqual(prop["maximum"], 10)

    def test_schema_blocks_extra_keys(self):
        # additionalProperties: false — LLM can't smuggle in a
        # 7th axis to game the gate.
        sch = critic_axes.axes_json_schema()
        self.assertFalse(sch["additionalProperties"])


class RenderAxesBlockTest(unittest.TestCase):

    def test_block_lists_every_axis_with_description(self):
        block = critic_axes.render_axes_block()
        for name, desc in critic_axes.AXES:
            self.assertIn(name, block,
                          f"axis name {name!r} missing from prompt block")
            # First sentence of description should appear.
            first_sentence = desc.split(".")[0][:40]
            self.assertIn(first_sentence, block)

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


if __name__ == "__main__":
    unittest.main()
