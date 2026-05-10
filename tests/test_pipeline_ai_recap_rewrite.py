"""Tests for pipeline.llm.ai_recap_rewrite — claude CLI is mocked out."""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT

from pipeline.llm import ai_recap_rewrite as ar


_GOOD_LLM_OUTPUT = {
    "hook": "Anthropic just plugged Claude directly into Photoshop, Blender, and Ableton.",
    "narration": (
        "Anthropic just plugged Claude directly into Photoshop, Blender, and Ableton. "
        "They're called Connectors. Nine of them, shipped together, available on every "
        "Claude plan today. Claude can now read your scene in Blender, your timeline "
        "in Ableton, your layers in Photoshop, and edit them. No copy-paste. No "
        "screenshot prompts. The model gets a real handle on your project. This is "
        "the moment AI assistants stop being a tab and start being a tool. Until now "
        "you described your problem to Claude and pasted the answer back. With "
        "Connectors, Claude opens the file, sees the project, makes the change. For "
        "solo creators, that's a workflow shift. For Adobe and Autodesk, the value is "
        "migrating up a layer. The apps that win the next decade aren't the ones with "
        "the prettiest panels. They're the ones with the cleanest API for an agent. "
        "FOLLOW for daily AI recaps. LIKE if this saved you a tab."
    ),
    "title_options": [
        "Claude just plugged into Photoshop, Blender, and Ableton",
        "Why Anthropic's Connectors threaten Adobe's moat",
        "The day AI stopped being a tab",
    ],
}

_GOOD_RAW = {
    "slug": "anthropic-connectors-creative-apps",
    "title": "Anthropic Connectors — Claude wires into Photoshop, Blender, Ableton",
    "body": "On April 28 2026, Anthropic released 9 Claude Connectors for creative apps.",
    "source": "manual:test",
    "url": "https://www.anthropic.com/news/claude-for-creative-work",
    "metadata": {},
}

SCRATCH_AI_RECAP = PROJECT_ROOT / "tests" / ".scratch_ai_recap"


class RewriteHappyPathTest(unittest.TestCase):
    def test_returns_well_formed_script(self):
        with patch.object(ar.llm, "call_claude_cli", return_value=_GOOD_LLM_OUTPUT):
            script = ar.rewrite(_GOOD_RAW)

        self.assertEqual(script["slug"], "anthropic-connectors-creative-apps")
        self.assertEqual(script["source"], "manual:test")
        self.assertEqual(script["source_url"], "https://www.anthropic.com/news/claude-for-creative-work")
        self.assertTrue(script["hook"].startswith("Anthropic"))
        self.assertEqual(len(script["title_options"]), 3)

    def test_caps_title_options_at_three(self):
        out = dict(_GOOD_LLM_OUTPUT, title_options=["title 1", "title 2", "title 3", "title 4", "title 5"])
        with patch.object(ar.llm, "call_claude_cli", return_value=out):
            script = ar.rewrite(_GOOD_RAW)
        self.assertEqual(len(script["title_options"]), 3)

    def test_strips_whitespace_from_fields(self):
        out = dict(
            _GOOD_LLM_OUTPUT,
            hook="  Anthropic just plugged Claude  \n",
            title_options=["  one  ", "  two  ", "  three  "],
        )
        with patch.object(ar.llm, "call_claude_cli", return_value=out):
            script = ar.rewrite(_GOOD_RAW)
        self.assertFalse(script["hook"].startswith(" "))
        self.assertFalse(script["hook"].endswith(" "))
        for title in script["title_options"]:
            self.assertEqual(title, title.strip())


class RewriteRejectsBadInputTest(unittest.TestCase):
    def test_empty_raw_story_raises(self):
        with self.assertRaises(ar.RecapRewriteError):
            ar.rewrite({"slug": "x"})

    def test_non_dict_llm_output_raises(self):
        with patch.object(ar.llm, "call_claude_cli", return_value="not a dict"):
            with self.assertRaises(ar.RecapRewriteError):
                ar.rewrite(_GOOD_RAW)

    def test_missing_required_fields_raises(self):
        with patch.object(ar.llm, "call_claude_cli", return_value={"hook": "x"}):
            with self.assertRaises(ar.RecapRewriteError):
                ar.rewrite(_GOOD_RAW)


class RewriteWarningsTest(unittest.TestCase):
    def test_short_narration_warns_but_does_not_raise(self):
        out = dict(
            _GOOD_LLM_OUTPUT,
            narration="Anthropic shipped connectors. FOLLOW for daily AI recaps. LIKE if this saved you a tab.",
        )
        with patch.object(ar.llm, "call_claude_cli", return_value=out):
            script = ar.rewrite(_GOOD_RAW)
        self.assertEqual(script["slug"], _GOOD_RAW["slug"])

    def test_missing_cta_warns_but_does_not_raise(self):
        out = dict(_GOOD_LLM_OUTPUT, narration="x " * 150)
        with patch.object(ar.llm, "call_claude_cli", return_value=out):
            script = ar.rewrite(_GOOD_RAW)
        self.assertNotIn("FOLLOW for daily AI recaps", script["narration"])


class SaveTest(unittest.TestCase):
    def test_writes_script_json_at_slug(self):
        script = {
            "slug": "test-slug",
            "hook": "h",
            "narration": "n",
            "title_options": ["t1"],
            "source_url": "u",
            "source": "s",
        }
        out_dir = SCRATCH_AI_RECAP / "save"
        shutil.rmtree(out_dir, ignore_errors=True)
        try:
            path = ar.save(script, out_dir)
            self.assertEqual(path.name, "test-slug.json")
            self.assertTrue(path.exists())
            roundtrip = json.loads(path.read_text())
            self.assertEqual(roundtrip["slug"], "test-slug")
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


class MainCliTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH_AI_RECAP, ignore_errors=True)
        SCRATCH_AI_RECAP.mkdir(parents=True, exist_ok=True)
        self.raw = SCRATCH_AI_RECAP / "raw.json"
        self.raw.write_text(json.dumps(_GOOD_RAW))

    def tearDown(self):
        shutil.rmtree(SCRATCH_AI_RECAP, ignore_errors=True)

    def test_missing_raw_returns_2(self):
        with patch.object(sys, "argv", ["ai_recap", "--raw", str(SCRATCH_AI_RECAP / "missing.json")]):
            self.assertEqual(ar.main(), 2)

    def test_rewrite_error_returns_1(self):
        with patch.object(sys, "argv", ["ai_recap", "--raw", str(self.raw)]), patch.object(
            ar, "rewrite", side_effect=ar.RecapRewriteError("bad")
        ):
            self.assertEqual(ar.main(), 1)
        with patch.object(sys, "argv", ["ai_recap", "--raw", str(self.raw)]), patch.object(
            ar, "rewrite", side_effect=ar.llm.ClaudeCLIError("bad")
        ):
            self.assertEqual(ar.main(), 1)

    def test_print_only_skips_save(self):
        script = {"slug": "s", "hook": "h", "narration": "n", "title_options": ["t"], "source_url": "", "source": ""}
        with patch.object(sys, "argv", ["ai_recap", "--raw", str(self.raw), "--print-only", "--model", "haiku"]), patch.object(
            ar, "rewrite", return_value=script
        ) as rewrite_mock, patch.object(ar, "save") as save_mock:
            self.assertEqual(ar.main(), 0)
        rewrite_mock.assert_called_once_with(_GOOD_RAW, model="haiku")
        save_mock.assert_not_called()

    def test_success_saves_to_out_dir(self):
        script = {"slug": "s", "hook": "h", "narration": "n", "title_options": ["t"], "source_url": "", "source": ""}
        out_dir = SCRATCH_AI_RECAP / "out"
        with patch.object(sys, "argv", ["ai_recap", "--raw", str(self.raw), "--out", str(out_dir)]), patch.object(
            ar, "rewrite", return_value=script
        ):
            self.assertEqual(ar.main(), 0)
        self.assertTrue((out_dir / "s.json").exists())


if __name__ == "__main__":
    unittest.main()
