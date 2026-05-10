"""Orchestrated rewrite path — RewriteContract end-to-end through run_stage.

Covers:
- Constraint-derived GOOD examples (no drift between prompt + validator).
- Successful one-shot when the LLM nails it on attempt 1.
- Auto-retry when attempt 1 fails missing_cta and attempt 2 succeeds.
- OrchestratorError when retries are exhausted on script_check errors.
- Warnings (long_narration, weak_hook) pass through to the StageResult
  without triggering retry.
- Cliffhanger flavour swaps the closer rules.
- Existing rewrite() callers see a Script object as before.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import rewrite as rw
from pipeline.llm import script_check
from pipeline.llm.contracts import RewriteContract
from pipeline.llm.fix import errors as fix_errors
from pipeline.llm.orchestrator import (
    OrchestratorError,
    StageContext,
    run_stage,
)


GOOD_AITA_BODY = (
    # 12 sentences, AITA closer ending in a CTA the validator accepts.
    "My MIL Carol announced she'd handle the wedding cake last week. "
    "I'm a professional baker. I said no thanks. "
    "She brought it anyway. Two days before the wedding she ordered her own. "
    "It cost four hundred dollars. The fondant was peeling. "
    "My fiancé sided with me. Carol stormed out. "
    "Now the family group chat is on fire. Was I out of line?"
)
GOOD_OUT = {
    "hook": "Am I wrong for refusing my MIL Carol's wedding cake?",
    "narration": GOOD_AITA_BODY,
    "title_options": ["MIL crashes wedding cake plans", "Cake fight", "MIL drama"],
}

# Same story but the closer doesn't match _CTA_RE on the first attempt
# (no verdict question, no comment-prompt, no question mark).
BAD_OUT_NO_CTA = {
    "hook": "Am I wrong for refusing my MIL Carol's wedding cake?",
    "narration": (
        "My MIL Carol announced she'd handle the wedding cake last week. "
        "I'm a professional baker. I said no thanks. "
        "She brought it anyway. Two days before the wedding she ordered her own. "
        "It cost four hundred dollars. The fondant was peeling. "
        "My fiancé sided with me. Carol stormed out. "
        "Subscribe for more messy family stories. The end."
    ),
    "title_options": ["MIL crashes wedding cake plans", "Cake fight", "MIL drama"],
}

# Close-to-spec but the protagonist used the BANNED acronym AITA.
BAD_OUT_AITA_ACRONYM = {
    "hook": "AITA for refusing my MIL Carol's wedding cake?",
    "narration": (
        "My MIL Carol announced she'd handle the wedding cake last week. "
        "I'm a professional baker. I said no thanks. "
        "She brought it anyway. Two days before the wedding she ordered her own. "
        "It cost four hundred dollars. The fondant was peeling. "
        "My fiancé sided with me. Carol stormed out. "
        "AITA for the verdict?"
    ),
    "title_options": ["MIL crashes wedding cake plans", "Cake fight", "MIL drama"],
}


class CTARuleInvariantTest(unittest.TestCase):
    """Every paired (pattern, example) in script_check.* MUST match itself.

    This is THE drift-prevention test. If a future edit breaks it, the
    rewrite contract's GOOD examples would teach the model phrasings the
    validator rejects — exactly the bug we hit on 2026-05-10 with the
    cake story.
    """

    def test_every_cta_rule_example_matches_its_pattern(self):
        import re
        for r in script_check._CTA_RULES:
            pat = re.compile(r.pattern, re.IGNORECASE)
            self.assertTrue(
                pat.search(r.example),
                msg=f"CTARule({r.pattern!r}, {r.example!r}) — example doesn't match pattern",
            )

    def test_every_cliffhanger_rule_example_matches_its_pattern(self):
        import re
        for r in script_check._CLIFFHANGER_CTA_RULES:
            pat = re.compile(r.pattern, re.IGNORECASE)
            self.assertTrue(
                pat.search(r.example),
                msg=f"CliffhangerCTARule({r.pattern!r}, {r.example!r}) — example doesn't match pattern",
            )

    def test_cake_story_tail_now_matches(self):
        # The exact phrasing that broke the 2026-05-10 smoke test.
        tail = "Tell me what you would have done. Subscribe for more messy family stories."
        self.assertTrue(script_check._CTA_RE.search(tail))


class RewriteContractConstraintsTest(unittest.TestCase):
    def test_constraints_pull_examples_from_validator_registry(self):
        contract = RewriteContract()
        ctx = StageContext(
            channel="mystoriesanimated",
            channel_cfg={"closer_format": "LIKE if YTA, COMMENT if NTA. AITA?"},
            raw_input={"title": "x", "body": "y"},
        )
        constraints = contract.gather_constraints(ctx)
        cta_constraint = next(c for c in constraints if c.name == "missing_cta")
        # Examples must be the SAME ones script_check.cta_examples() returns.
        self.assertEqual(
            list(cta_constraint.examples_good),
            script_check.cta_examples(),
        )

    def test_cliffhanger_uses_cliffhanger_examples(self):
        contract = RewriteContract()
        ctx = StageContext(
            channel="mystoriesanimated",
            channel_cfg={"cliffhanger": True},
            raw_input={"title": "x", "body": "y"},
        )
        constraints = contract.gather_constraints(ctx)
        cta_constraint = next(c for c in constraints if c.name == "missing_cta")
        self.assertEqual(
            list(cta_constraint.examples_good),
            script_check.cta_examples(cliffhanger=True),
        )

    def test_prompt_embeds_critical_rules_block(self):
        contract = RewriteContract()
        ctx = StageContext(
            channel="mystoriesanimated",
            channel_cfg={"closer_format": "LIKE if YTA"},
            raw_input={"title": "MIL drama", "body": "She brought a cake..."},
        )
        prompt = contract.build_prompt(ctx, contract.gather_constraints(ctx))
        self.assertIn("CRITICAL RULES", prompt)
        self.assertIn("missing_cta", prompt)
        self.assertIn("banned_acronyms", prompt)
        # GOOD examples are derived from validator registry.
        for ex in script_check.cta_examples()[:3]:
            self.assertIn(ex, prompt)


class RewriteContractValidationTest(unittest.TestCase):
    def setUp(self):
        self.contract = RewriteContract()
        self.ctx = StageContext(
            channel="mystoriesanimated",
            channel_cfg={"closer_format": "LIKE if YTA, COMMENT if NTA. AITA?"},
            raw_input={"title": "MIL drama", "body": "story body"},
        )

    def test_good_output_passes_with_zero_errors(self):
        fixes = self.contract.validate(self.ctx, GOOD_OUT)
        errs = fix_errors(fixes)
        self.assertEqual(errs, [], msg=f"unexpected errors: {[f.constraint for f in errs]}")

    def test_no_cta_output_returns_missing_cta_error(self):
        fixes = self.contract.validate(self.ctx, BAD_OUT_NO_CTA)
        errs = [f for f in fix_errors(fixes) if f.constraint == "missing_cta"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].target_path, "narration")
        self.assertEqual(errs[0].source_judge, "script_check")

    def test_validation_skips_when_narration_missing(self):
        fixes = self.contract.validate(self.ctx, {"hook": "h", "title_options": ["t"]})
        # Should report missing_field for narration AND not call script_check.
        self.assertTrue(any(f.constraint == "missing_field" and f.target_path == "narration" for f in fixes))


class RewriteOrchestratedRunStageTest(unittest.TestCase):
    """Exercise run_stage(RewriteContract, ...) with mocked LLM responses."""

    def setUp(self):
        self.contract = RewriteContract()
        self.ctx = StageContext(
            channel="mystoriesanimated",
            channel_cfg={"closer_format": "LIKE if YTA, COMMENT if NTA. AITA?"},
            raw_input={"title": "MIL drama", "body": "She brought a cake to my wedding."},
        )

    def test_one_shot_success(self):
        seq = [GOOD_OUT]
        def fake_llm(prompt, **kwargs):
            return seq.pop(0)
        result = run_stage(self.contract, self.ctx, llm_call=fake_llm, max_retries=2)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.output["narration"], GOOD_OUT["narration"])

    def test_auto_retry_on_missing_cta_then_success(self):
        # Attempt 1: missing CTA. Attempt 2: GOOD.
        seq = [BAD_OUT_NO_CTA, GOOD_OUT]
        def fake_llm(prompt, **kwargs):
            return seq.pop(0)
        result = run_stage(self.contract, self.ctx, llm_call=fake_llm, max_retries=2)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.output["narration"], GOOD_OUT["narration"])
        self.assertEqual(seq, [], msg="expected exactly 2 LLM calls")

    def test_regen_prompt_carries_failure_context(self):
        captured: list[str] = []
        seq = [BAD_OUT_NO_CTA, GOOD_OUT]
        def fake_llm(prompt, **kwargs):
            captured.append(prompt)
            return seq.pop(0)
        run_stage(self.contract, self.ctx, llm_call=fake_llm, max_retries=2)
        # Second prompt is the regen prompt — must mention the failed
        # constraint AND the previous bad narration so the model can fix it.
        regen = captured[1]
        self.assertIn("missing_cta", regen)
        self.assertIn(BAD_OUT_NO_CTA["narration"][:30], regen)

    def test_retries_exhausted_raises_orchestrator_error(self):
        # Every attempt fails → exhaust 0,1,2 attempts → raise.
        seq = [BAD_OUT_NO_CTA] * 5
        def fake_llm(prompt, **kwargs):
            return seq.pop(0)
        with self.assertRaises(OrchestratorError) as ctx:
            run_stage(self.contract, self.ctx, llm_call=fake_llm, max_retries=2)
        # default + max_retries=2 → 3 total attempts
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertEqual(ctx.exception.stage, "rewrite")
        names = [f.constraint for f in ctx.exception.last_failures]
        self.assertIn("missing_cta", names)

    def test_warnings_pass_through_without_retry(self):
        # An output that has only WARNING-level issues (long_narration if
        # applicable; weak_hook for non-AITA-strict) shouldn't retry.
        # Use a non-AITA channel_cfg so soft principles stay warning.
        ctx_relaxed = StageContext(
            channel="rhymetimejunction",
            channel_cfg={},  # no closer_format → not AITA-class
            raw_input={"title": "x", "body": "y"},
        )
        seq = [GOOD_OUT]
        def fake_llm(prompt, **kwargs):
            return seq.pop(0)
        result = run_stage(self.contract, ctx_relaxed, llm_call=fake_llm, max_retries=2)
        self.assertEqual(result.attempts, 1)


class RewriteIntegrationTest(unittest.TestCase):
    """rewrite() — the public entry — must use the orchestrated path by default."""

    RAW_STORY = {
        "slug": "test-orchestrated",
        "title": "MIL drama",
        "body": "She brought a cake to my wedding.",
        "url": "https://reddit.example",
        "source": "reddit",
    }

    def test_default_path_is_orchestrator(self):
        # Make sure no env var is leaking from another test
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YTFACTORY_REWRITE_USE_LEGACY", None)
            with patch.object(rw.llm, "call_claude_cli", return_value=GOOD_OUT) as call:
                script = rw.rewrite(
                    self.RAW_STORY,
                    channel_cfg={"closer_format": "LIKE if YTA, COMMENT if NTA. AITA?"},
                )
        # Orchestrator should call LLM once if attempt 1 succeeds.
        self.assertEqual(call.call_count, 1)
        self.assertEqual(script.slug, "test-orchestrated")
        self.assertIn("Was I out of line", script.narration)

    def test_legacy_path_when_env_set(self):
        with patch.dict(os.environ, {"YTFACTORY_REWRITE_USE_LEGACY": "1"}):
            with patch.object(rw.llm, "call_claude_cli", return_value=GOOD_OUT) as call:
                rw.rewrite(self.RAW_STORY, channel_cfg={})
        self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
