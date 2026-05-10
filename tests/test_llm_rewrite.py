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


if __name__ == "__main__":
    unittest.main()
