from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import rewrite as rw


SCRATCH = PROJECT_ROOT / "tests" / ".scratch_rewrite"
RAW_STORY = {"slug": "story-slug", "title": "My title", "body": "My body", "url": "https://ex", "source": "reddit"}
GOOD_OUT = {"hook": " My hook ", "narration": "Wine. Crafts. Just us.", "title_options": [" One ", "Two", "Three", "Four"]}


# These tests probe the LEGACY single-shot rewrite path (prompt assembly,
# CLI invocation, error handling). They were written before the
# orchestrator landed; opt them into the legacy path explicitly.
# Tests for the orchestrated path live in test_llm_rewrite_orchestrated.py.
_LEGACY_ENV = {"YTFACTORY_REWRITE_USE_LEGACY": "1"}


class RewriteTest(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(os.environ, _LEGACY_ENV)
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def test_rewrite_success_uses_model_and_lint_fix(self):
        with patch.object(rw.llm, "model_for", return_value="opus") as model_for, \
             patch.object(rw.llm, "call_claude_cli", return_value=GOOD_OUT) as call:
            script = rw.rewrite(RAW_STORY, channel_cfg={"cliffhanger": True})
        model_for.assert_called_once_with("rewrite")
        self.assertIn("CLIFFHANGER FORMAT", call.call_args.args[0])
        self.assertEqual(call.call_args.kwargs["model"], "opus")
        self.assertEqual(script.slug, "story-slug")
        self.assertEqual(script.hook, "My hook")
        self.assertIn("Wine, crafts, just us.", script.narration)
        self.assertEqual(script.title_options, ["One", "Two", "Three"])
        self.assertEqual(script.source_url, "https://ex")
        self.assertEqual(script.source, "reddit")

    def test_rewrite_no_closer_uses_generic_prompt_and_default_slug(self):
        with patch.object(rw.llm, "model_for", return_value="haiku"), \
             patch.object(rw.llm, "call_claude_cli", return_value=GOOD_OUT) as call:
            script = rw.rewrite({"body": "Only body"}, channel_cfg={})
        self.assertEqual(script.slug, "untitled")
        self.assertIn("any vote-prompt CTA is fine", call.call_args.args[0])

    def test_rewrite_rejects_bad_input_and_bad_llm_shapes(self):
        with self.assertRaises(ValueError):
            rw.rewrite({"slug": "x"})
        cases = ["not dict", {"hook": "h"}, {"hook": "h", "narration": "n", "title_options": []}, {"hook": "h", "narration": "n", "title_options": "bad"}]
        for out in cases:
            with patch.object(rw.llm, "model_for", return_value="opus"), patch.object(rw.llm, "call_claude_cli", return_value=out):
                with self.assertRaises(ValueError):
                    rw.rewrite(RAW_STORY)

    def test_rewrite_part2_success_and_errors(self):
        with self.assertRaises(ValueError):
            rw.rewrite_part2({"slug": "x"}, "part one")
        with self.assertRaises(ValueError):
            rw.rewrite_part2(RAW_STORY, "  ")
        with patch.object(rw.llm, "model_for", return_value="opus"), \
             patch.object(rw.llm, "call_claude_cli", return_value=GOOD_OUT) as call:
            script = rw.rewrite_part2(RAW_STORY, "Part one ended here", channel_cfg={"closer_format": "LIKE if YTA"})
        self.assertIn("PART 2", call.call_args.args[0])
        self.assertEqual(script.slug, "story-slug")
        for out in [[], {"hook": "h"}, {"hook": "h", "narration": "n", "title_options": []}]:
            with patch.object(rw.llm, "model_for", return_value="opus"), patch.object(rw.llm, "call_claude_cli", return_value=out):
                with self.assertRaises(ValueError):
                    rw.rewrite_part2(RAW_STORY, "Part one")

    def test_load_and_save_roundtrip(self):
        path = SCRATCH / "nested" / "script.json"
        script = rw.Script(slug="s", hook="h", narration="n", title_options=["t"])
        rw.save_script(script, path)
        self.assertEqual(rw.load_script(path), script)
        legacy = SCRATCH / "legacy.json"
        legacy.write_text(json.dumps({"slug": "s", "hook": "h", "narration": "n", "title_options": ["t"]}))
        loaded = rw.load_script(legacy)
        self.assertEqual(loaded.source_url, "")
        self.assertEqual(loaded.source, "")


class PromptNumbersRuleTest(unittest.TestCase):
    """Pin the 2026-05-14 update to the numbers-formatting rule.

    Pre-fix the rewriter prompt told the LLM to spell ALL numbers as
    words ('two thousand five', 'twelve fifty-eight', 'sixty guests').
    Modern Cloud Run TTS handles digits naturally, so the rule was
    relaxed: years/dates/ages/counts use digits; only currency-with-
    symbol and decimals/fractions still need spelling out.

    Pin the new rule by asserting the prompt mentions the carve-out so
    a casual edit doesn't slip back to the old 'always spell out' rule.
    """

    def test_prompt_mentions_digits_for_years_and_counts(self):
        # The prompt tells the LLM that years/dates/ages/counts use
        # DIGITS. Search for the keyword + the example pattern.
        # _SHARED_CRAFT_RULES is the relevant string constant.
        from pipeline.llm import rewrite as rw
        prosody = rw._SHARED_CRAFT_RULES
        # We embed a comment about Chatterbox + IndicF5 and an example
        # showing 1258 / 2005 in digit form. Pin one of the markers.
        self.assertIn(
            "DIGITS", prosody,
            "rewrite prompt must instruct LLM to use DIGITS for "
            "years/dates/ages/counts (per 2026-05-14 audit)",
        )
        self.assertIn(
            "1258", prosody,
            "rewrite prompt must show '1258' as a digit-form example "
            "(historyrecapped/baghdad-mongols-1258 was the regression case)",
        )

    def test_prompt_keeps_currency_symbol_warning(self):
        # Currency with $ / € symbol still needs spelling out — TTS
        # mishandles the symbol regardless of model.
        from pipeline.llm import rewrite as rw
        prosody = rw._SHARED_CRAFT_RULES
        self.assertIn(
            "$2000", prosody,
            "rewrite prompt must keep the $2000 currency-symbol "
            "warning (TTS reads the symbol as 'two zero zero zero')",
        )

    def test_prompt_no_longer_says_always_spell_out(self):
        # The old rule said 'ALWAYS spell out as words ... applies to
        # ages, counts, amounts, dates — every number.' That clause
        # was the regression source. Make sure it's gone.
        from pipeline.llm import rewrite as rw
        prosody = rw._SHARED_CRAFT_RULES
        self.assertNotIn(
            "applies to ages, counts, amounts, dates", prosody,
            "the legacy 'spell every number as words' rule must not "
            "be reintroduced — it caused the 'twelve fifty-eight' "
            "TTS regression on baghdad-mongols-1258",
        )


class PromptEraAnchorInstructionTest(unittest.TestCase):
    """Pin the 2026-05-14 Phase 4b prompt update that teaches the LLM
    to emit metadata.era_anchor for historical topics."""

    def test_prompt_mentions_era_anchor_field(self):
        from pipeline.llm import rewrite as rw
        self.assertIn("era_anchor", rw._BASE_PROMPT,
                      "_BASE_PROMPT must mention era_anchor in the "
                      "JSON output schema (Phase 4b wiring)")

    def test_prompt_lists_known_taxonomy_keys(self):
        # The prompt should enumerate at least a few taxonomy keys so
        # the LLM picks valid kebab-case IDs (not invented ones).
        from pipeline.llm import rewrite as rw
        for key in (
            "13c-mongol-yuan-warband",
            "ww1-1914-1918-trench",
            "ww2-1939-1945-european-theater",
            "1c-roman-legion-segmentata",
        ):
            self.assertIn(key, rw._BASE_PROMPT,
                          f"_BASE_PROMPT must list era key {key!r}")

    def test_prompt_says_omit_for_non_historical(self):
        # Critical — without this guard the LLM would emit garbage
        # era_anchor values for AITA / sports / contemporary topics.
        from pipeline.llm import rewrite as rw
        self.assertIn("OMIT for non-historical", rw._BASE_PROMPT,
                      "_BASE_PROMPT must instruct OMIT for non-historical")

    def test_script_dataclass_has_metadata_field(self):
        # The Script.metadata field accepts the LLM's emitted era_anchor
        # blob and round-trips through save_script / asdict.
        from pipeline.llm.rewrite import Script, save_script, load_script
        import tempfile
        import shutil
        s = Script(
            slug="test", hook="h", narration="n",
            title_options=["t"],
            metadata={"era_anchor": "13c-mongol-yuan-warband"},
        )
        tmp_dir = Path(tempfile.mkdtemp())
        try:
            path = tmp_dir / "test.json"
            save_script(s, path)
            loaded = load_script(path)
            self.assertEqual(
                loaded.metadata.get("era_anchor"),
                "13c-mongol-yuan-warband",
            )
        finally:
            shutil.rmtree(tmp_dir)

    def test_script_dataclass_metadata_defaults_to_none(self):
        # Backward compat — existing Script(...) calls without metadata
        # default to None, not a dict (avoids dict-mutation bugs across
        # instances).
        from pipeline.llm.rewrite import Script
        s = Script(slug="s", hook="h", narration="n", title_options=["t"])
        self.assertIsNone(s.metadata)


if __name__ == "__main__":
    unittest.main()
