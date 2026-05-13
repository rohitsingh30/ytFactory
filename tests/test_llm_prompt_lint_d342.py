"""Audit D3.42 — pipeline/llm/prompt_lint.py had a dead variable
`mentioned = _mentioned_cast_names(...)` that was computed but
never read. The helper itself was also unused after removal.

Tests pin the cast-drift soft-check that DOES use cast_names, so
removing the dead path doesn't regress the actual lint behaviour.
"""
from __future__ import annotations

import unittest

from pipeline.llm import prompt_lint


def _make_prompts(scene: str) -> list[dict]:
    return [{"scene": scene}]


def _cast(narrator: str = "Sarah", supporting: list[str] | None = None) -> dict:
    # _gather_cast_names only walks `supporting` (narrator is rendered
    # separately and isn't a "cast drift" candidate name). To put the
    # narrator into cast_names we list them under supporting too.
    return {
        "narrator": {"name": narrator},
        "supporting": [{"name": n} for n in (supporting or [narrator])],
    }


class CastDriftTest(unittest.TestCase):
    def test_known_cast_name_no_warning(self):
        issues = prompt_lint.check_prompts(
            _make_prompts("Sarah walks into the room. She picks up the book."),
            expected_beats=1,
            cast=_cast(),
        )
        codes = {i.code for i in issues}
        self.assertNotIn("prompt_cast_drift", codes,
                         f"expected no drift, got: {[i.message for i in issues]}")

    def test_unknown_proper_noun_emits_drift_warning(self):
        issues = prompt_lint.check_prompts(
            _make_prompts("Bartholomew strides past Sarah."),
            expected_beats=1,
            cast=_cast(),
        )
        codes = [i.code for i in issues]
        self.assertIn("prompt_cast_drift", codes,
                      f"expected drift, got: {codes}")
        msg = next(i.message for i in issues if i.code == "prompt_cast_drift")
        self.assertIn("Bartholomew", msg)

    def test_no_cast_skips_drift_check(self):
        # When cast is None, drift check should not fire regardless
        # of how many proper nouns appear.
        issues = prompt_lint.check_prompts(
            _make_prompts("Bartholomew, Sarah, and Maria stand together."),
            expected_beats=1,
            cast=None,
        )
        codes = {i.code for i in issues}
        self.assertNotIn("prompt_cast_drift", codes)


class HelperRemovalSmokeTest(unittest.TestCase):
    """Audit D3.42 — `_mentioned_cast_names` was removed alongside
    its only call site. Confirm the helper is truly gone so future
    edits don't accidentally re-introduce dead code with the old
    name."""

    def test_helper_no_longer_exported(self):
        self.assertFalse(hasattr(prompt_lint, "_mentioned_cast_names"))


if __name__ == "__main__":
    unittest.main()
