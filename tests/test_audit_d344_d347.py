"""Audit D3.44 + D3.47: aggregator/youtube _NON_CHANNEL_DIRS lockstep
+ rewrite_long_form respects YTFACTORY_MODEL_REWRITE_LONG_FORM env.

D3.44: pre-fix pipeline/research/aggregator.py had its own copy of
_NON_CHANNEL_DIRS which had drifted out of sync with
pipeline/research/youtube.py (missing `web-next`, `cloud`). Now
imported from youtube.py so there's exactly ONE list to maintain.

D3.47: pre-fix pipeline/llm/rewrite_long_form.py:383 hardcoded
`model="opus"`, ignoring the YTFACTORY_MODEL_REWRITE_LONG_FORM env
override that the dispatcher's model_for() helper respects. Now
routes through model_for("rewrite_long_form").
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch


class NonChannelDirsLockstepTest(unittest.TestCase):
    """Audit D3.44 — both modules now reference the same set."""

    def test_aggregator_imports_canonical_set(self):
        from pipeline.research import aggregator as _agg
        from pipeline.research import youtube as _yt
        # Identity check: the aggregator's _NON_CHANNEL_DIRS MUST be
        # the same object as youtube's, not a drifted copy.
        self.assertIs(_agg._NON_CHANNEL_DIRS, _yt._NON_CHANNEL_DIRS)

    def test_canonical_set_has_web_next_and_cloud(self):
        # Sanity: the canonical set covers the two newer dirs that
        # were missing pre-fix.
        from pipeline.research import youtube as _yt
        self.assertIn("web-next", _yt._NON_CHANNEL_DIRS)
        self.assertIn("cloud", _yt._NON_CHANNEL_DIRS)


class ModelForRewriteLongFormTest(unittest.TestCase):
    """Audit D3.47 — rewrite_long_form now calls model_for("rewrite_long_form")."""

    def test_default_resolves_to_opus(self):
        from pipeline.llm import cli as llm_cli
        env = {k: v for k, v in os.environ.items()
               if k != "YTFACTORY_MODEL_REWRITE_LONG_FORM"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(llm_cli.model_for("rewrite_long_form"), "opus")

    def test_env_override_respected(self):
        from pipeline.llm import cli as llm_cli
        with patch.dict(os.environ,
                         {"YTFACTORY_MODEL_REWRITE_LONG_FORM": "sonnet"}):
            self.assertEqual(llm_cli.model_for("rewrite_long_form"), "sonnet")

    def test_rewrite_long_form_passes_model_for_to_dispatcher(self):
        # Patch call_claude_cli so we can introspect the model arg.
        from pipeline.llm import rewrite_long_form as _rlf
        from pipeline.llm import cli as llm_cli

        captured = {}

        def _capture(prompt, **kw):
            captured["model"] = kw.get("model")
            captured["stage"] = kw.get("stage")
            return {
                "title_options": ["t1"],
                "description": "d",
                "thumbnail_text": "tt",
                "tags": [],
                "sections": [
                    {"id": "s1", "title": "T", "narration": "N",
                     "target_s": 60.0, "visual_brief": "v",
                     "panels": [
                        {"prompt": "p1", "duration_s": 5.0},
                     ]},
                ],
            }

        raw_story = {
            "slug": "test", "title": "T", "body": "B",
            "source": "s", "url": "u",
        }
        with patch.dict(os.environ,
                         {"YTFACTORY_MODEL_REWRITE_LONG_FORM": "haiku"}), \
             patch.object(_rlf._llm, "call_claude_cli", side_effect=_capture):
            try:
                _rlf.rewrite_long_form(
                    raw_story,
                    channel_cfg={},
                    target_duration_s=120,
                )
            except (TypeError, AttributeError, KeyError, ValueError):
                # Post-call processing might fail on the partial mock
                # — we only care about the LLM call args.
                pass
        self.assertEqual(captured.get("model"), "haiku")
        self.assertEqual(captured.get("stage"), "rewrite_long_form")


if __name__ == "__main__":
    unittest.main()
