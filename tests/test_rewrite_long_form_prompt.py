"""Tests for ``pipeline.llm.rewrite_long_form`` — focused on the
prompt's craft rules + token-budget contract.

Why this exists: the long-form rewriter is the gate between user
intent (target duration, channel character) and the rendered output.
Two recurring quality bugs trace back to soft instructions in the
prompt:

  1. Character drift across panels (the rendered protagonist morphs
     between random ages / appearances per panel) — the prompt did
     not require character spec re-use.
  2. Truncated 30-min scripts (job 8413e79d, 2026-05-12) — combo of
     a soft "roughly N words" instruction + an unset SDK token cap.
     The token cap is fixed in pipeline/llm/cli.py; the prompt-side
     half of the fix is to keep the 4500-words instruction visible
     and add the character-consistency block.

These tests pin the prompt contract so a future "tighten the prompt"
edit can't silently drop the rules that prevent these regressions.
"""
from __future__ import annotations

import unittest

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import rewrite_long_form as rlf


class PlanningTargetsTest(unittest.TestCase):
    """Sanity-check ``_planned_sections_and_panels`` so the prompt
    keeps demanding the right word count for the requested duration."""

    def test_30min_target_asks_for_4500_words(self) -> None:
        # 30 min × 150 wpm = 4500 words. If a refactor changes the
        # wpm assumption, audio-vs-script duration drifts and we get
        # the 17-min-vs-30-min bug class back.
        words, *_ = rlf._planned_sections_and_panels(1800)
        self.assertEqual(words, 4500)

    def test_15min_target_asks_for_2250_words(self) -> None:
        words, *_ = rlf._planned_sections_and_panels(900)
        self.assertEqual(words, 2250)

    def test_panel_count_capped_at_24(self) -> None:
        # Channel YAMLs (mystoriesanimated etc.) cap at 24 panels —
        # the heuristic must respect that or image-gen runs blow up.
        _, _, _, panel_count, _ = rlf._planned_sections_and_panels(3600)
        self.assertLessEqual(panel_count, 24)


class CharacterConsistencyPromptTest(unittest.TestCase):
    """Pin the character-lock rule in ``_PROMPT_TEMPLATE``.

    Regression — the 2026-05-12 mystoriesanimated render had the
    protagonist rendered as a young child in some panels and an
    elderly man in others because the rewriter authored each panel
    scene independently with no character re-use directive.
    """

    def test_template_demands_one_character_spec_upfront(self) -> None:
        body = " ".join(rlf._PROMPT_TEMPLATE.split())
        # The literal phrase ties the rule to the diagnosis.
        self.assertIn("CHARACTER CONSISTENCY", body,
                      "rewrite_long_form prompt must include the "
                      "CHARACTER CONSISTENCY craft rule (see 2026-05-12 "
                      "mystoriesanimated post-mortem)")

    def test_template_requires_repeating_spec_in_every_panel(self) -> None:
        body = " ".join(rlf._PROMPT_TEMPLATE.split())
        # Verbatim repetition is the only thing that works against
        # an image generator with no inter-panel memory.
        self.assertIn("repeat the WHOLE character spec verbatim", body)

    def test_template_uses_channel_recurring_character_when_present(self) -> None:
        body = " ".join(rlf._PROMPT_TEMPLATE.split())
        # The rule must defer to the channel YAML's
        # character_description (surfaced into the prompt as
        # "Recurring character:") rather than inventing a new one.
        self.assertIn("Recurring character", body)
        self.assertIn("USE THAT EXACTLY", body)

    def test_template_carves_out_landscape_panels(self) -> None:
        # Don't force the rule on panels that have no people.
        body = " ".join(rlf._PROMPT_TEMPLATE.split())
        self.assertIn("Landscape / inanimate panels (no people)", body)


class ChannelContextSurfacesCharacterTest(unittest.TestCase):
    """``_channel_context`` must surface the YAML's
    ``character_description`` as ``Recurring character: …`` so the
    new craft rule has something to reference."""

    def test_character_description_propagates(self) -> None:
        cfg = {
            "name": "TestChannel",
            "character_description": "A round-headed cartoon person.",
        }
        ctx = rlf._channel_context(cfg)
        self.assertIn("Recurring character: A round-headed cartoon person.",
                      ctx)

    def test_no_character_description_is_silent(self) -> None:
        cfg = {"name": "TestChannel"}
        ctx = rlf._channel_context(cfg)
        self.assertNotIn("Recurring character", ctx)


if __name__ == "__main__":
    unittest.main()
