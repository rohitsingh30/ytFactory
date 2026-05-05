"""Tests for pipeline.airecap_rewrite — claude CLI is mocked out."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import airecap_rewrite as ar


# A canned LLM response that satisfies the schema.
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
        out = dict(_GOOD_LLM_OUTPUT, title_options=[
            "title 1", "title 2", "title 3", "title 4", "title 5",
        ])
        with patch.object(ar.llm, "call_claude_cli", return_value=out):
            script = ar.rewrite(_GOOD_RAW)
        self.assertEqual(len(script["title_options"]), 3)

    def test_strips_whitespace_from_fields(self):
        out = dict(_GOOD_LLM_OUTPUT,
                   hook="  Anthropic just plugged Claude  \n",
                   title_options=["  one  ", "  two  ", "  three  "])
        with patch.object(ar.llm, "call_claude_cli", return_value=out):
            script = ar.rewrite(_GOOD_RAW)
        self.assertFalse(script["hook"].startswith(" "))
        self.assertFalse(script["hook"].endswith(" "))
        for t in script["title_options"]:
            self.assertEqual(t, t.strip())


class RewriteRejectsBadInputTest(unittest.TestCase):
    def test_empty_raw_story_raises(self):
        with self.assertRaises(ar.AirecapRewriteError):
            ar.rewrite({"slug": "x"})  # no title or body

    def test_non_dict_llm_output_raises(self):
        with patch.object(ar.llm, "call_claude_cli", return_value="not a dict"):
            with self.assertRaises(ar.AirecapRewriteError):
                ar.rewrite(_GOOD_RAW)

    def test_missing_required_fields_raises(self):
        with patch.object(ar.llm, "call_claude_cli", return_value={"hook": "x"}):
            with self.assertRaises(ar.AirecapRewriteError):
                ar.rewrite(_GOOD_RAW)


class RewriteWarningsTest(unittest.TestCase):
    def test_short_narration_warns_but_does_not_raise(self):
        out = dict(_GOOD_LLM_OUTPUT, narration="Anthropic shipped connectors. FOLLOW for daily AI recaps. LIKE if this saved you a tab.")
        with patch.object(ar.llm, "call_claude_cli", return_value=out):
            # Should still return a script; warning goes to stderr.
            script = ar.rewrite(_GOOD_RAW)
        self.assertEqual(script["slug"], _GOOD_RAW["slug"])

    def test_missing_cta_warns_but_does_not_raise(self):
        narr = "x " * 150
        out = dict(_GOOD_LLM_OUTPUT, narration=narr)
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
        with tempfile.TemporaryDirectory() as d:
            path = ar.save(script, Path(d))
            self.assertEqual(path.name, "test-slug.json")
            self.assertTrue(path.exists())
            roundtrip = json.loads(path.read_text())
            self.assertEqual(roundtrip["slug"], "test-slug")


if __name__ == "__main__":
    unittest.main()
