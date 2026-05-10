"""Tests for cache, fix_router, dag, render_job, pipeline_runner.

Together these exercise the full orchestrator stack BELOW the LLM
layer — cache hit/miss + key invariance, Fix routing across stages,
DAG topology + downstream-invalidation, and the pipeline runner's
walk + critic-FIX cascade with cache reuse.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import cache as cache_mod
from pipeline.llm import fix_router, pipeline_runner
from pipeline.llm.dag import STAGE_DAG, StageNode, stages_invalidated_by, topo_order
from pipeline.llm.fix import Fix
from pipeline.llm.render_job import BeatArtifact, CriticVerdict, RenderJob


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class CacheKeyTest(unittest.TestCase):
    def test_same_inputs_same_key(self):
        k1 = cache_mod.key_for(stage="rewrite", channel="m", channel_cfg={"a": 1},
                               upstream={"b": 2}, sub_index=None)
        k2 = cache_mod.key_for(stage="rewrite", channel="m", channel_cfg={"a": 1},
                               upstream={"b": 2}, sub_index=None)
        self.assertEqual(k1, k2)

    def test_different_stage_different_key(self):
        k1 = cache_mod.key_for(stage="rewrite", channel="m", channel_cfg={}, upstream={})
        k2 = cache_mod.key_for(stage="cast",    channel="m", channel_cfg={}, upstream={})
        self.assertNotEqual(k1, k2)

    def test_different_sub_index_different_key(self):
        base = dict(stage="prompts", channel="m", channel_cfg={}, upstream={"b": 1})
        self.assertNotEqual(
            cache_mod.key_for(**base, sub_index=14),
            cache_mod.key_for(**base, sub_index=15),
        )

    def test_upstream_change_changes_key(self):
        k1 = cache_mod.key_for(stage="prompts", channel="m", channel_cfg={},
                               upstream={"script": {"narration": "a"}})
        k2 = cache_mod.key_for(stage="prompts", channel="m", channel_cfg={},
                               upstream={"script": {"narration": "b"}})
        self.assertNotEqual(k1, k2)


class LocalCacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = cache_mod.LocalCache(root=self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_miss_returns_none(self):
        self.assertIsNone(self.cache.get("nope"))
        self.assertFalse(self.cache.has("nope"))

    def test_put_then_get_round_trip(self):
        rec = cache_mod.CacheRecord(output={"hook": "x", "narration": "y"},
                                     binary_uri="gs://b/x.png",
                                     metadata={"attempt": 2})
        self.cache.put("abc", rec)
        self.assertTrue(self.cache.has("abc"))
        out = self.cache.get("abc")
        self.assertEqual(out.output, {"hook": "x", "narration": "y"})
        self.assertEqual(out.binary_uri, "gs://b/x.png")
        self.assertEqual(out.metadata, {"attempt": 2})

    def test_corrupt_entry_returns_none(self):
        # Manually corrupt the entry on disk; cache should treat as miss.
        self.cache.put("k", cache_mod.CacheRecord(output={"x": 1}))
        path = self.cache._path("k")
        path.write_text("not json")
        self.assertIsNone(self.cache.get("k"))


class DefaultCacheSelectionTest(unittest.TestCase):
    def setUp(self):
        cache_mod.reset_default_cache()
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("YTFACTORY_CACHE_DISABLED",
                                 "YTFACTORY_CACHE_BACKEND",
                                 "YTFACTORY_CACHE_BUCKET",
                                 "YTFACTORY_CACHE_DIR")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
        cache_mod.reset_default_cache()

    def test_disabled_returns_noop_cache(self):
        os.environ["YTFACTORY_CACHE_DISABLED"] = "1"
        c = cache_mod.get_default_cache()
        self.assertIsInstance(c, cache_mod.NoopCache)

    def test_local_default(self):
        c = cache_mod.get_default_cache()
        self.assertIsInstance(c, cache_mod.LocalCache)

    def test_gcs_without_bucket_falls_back_to_local(self):
        os.environ["YTFACTORY_CACHE_BACKEND"] = "gcs"
        c = cache_mod.get_default_cache()
        self.assertIsInstance(c, cache_mod.LocalCache)


# ---------------------------------------------------------------------------
# Fix router
# ---------------------------------------------------------------------------


class FixRouterTest(unittest.TestCase):
    def test_explicit_target_stage_wins(self):
        f = Fix(constraint="x", reason="y", target_stage="rewrite", target_path="narration")
        r = fix_router.route(f)
        self.assertEqual(r.stage, "rewrite")
        self.assertIsNone(r.sub_index)

    def test_routes_beats_image_to_images_with_sub_index(self):
        f = Fix(constraint="cast_drift", reason="hair changed", target_path="beats[14].image")
        r = fix_router.route(f)
        self.assertEqual(r.stage, "images")
        self.assertEqual(r.sub_index, 14)

    def test_routes_beats_prompt_to_prompts(self):
        f = Fix(constraint="kit_lock", reason="missing", target_path="beats[7].prompt")
        r = fix_router.route(f)
        self.assertEqual(r.stage, "prompts")
        self.assertEqual(r.sub_index, 7)

    def test_routes_cast_subpaths_to_cast(self):
        f = Fix(constraint="cast_drift", reason="x", target_path="cast.protagonist.hair")
        r = fix_router.route(f)
        self.assertEqual(r.stage, "cast")

    def test_unrouted_raises(self):
        with self.assertRaises(fix_router.UnroutableFix):
            fix_router.route(Fix(constraint="x", reason="y", target_path="mystery_path"))

    def test_cascade_groups_by_stage_and_sub_index(self):
        fixes = [
            Fix(constraint="cast_drift", reason="...", target_path="beats[7].image"),
            Fix(constraint="kit_lock",  reason="...", target_path="beats[7].image"),
            Fix(constraint="missing",   reason="...", target_path="beats[14].image"),
            Fix(constraint="missing_cta", reason="x", target_path="narration"),
        ]
        grouped = fix_router.cascade(fixes)
        self.assertEqual(set(grouped.keys()), {"images", "rewrite"})
        self.assertEqual(set(grouped["images"].keys()), {7, 14})
        self.assertEqual(len(grouped["images"][7]), 2)
        self.assertEqual(len(grouped["images"][14]), 1)
        self.assertEqual(len(grouped["rewrite"][None]), 1)


# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------


class DAGTest(unittest.TestCase):
    def test_topo_order_is_valid(self):
        order = topo_order()
        seen: set[str] = set()
        for s in order:
            for d in STAGE_DAG[s].deps:
                self.assertIn(d, seen,
                              f"stage {s!r} ran before its dep {d!r}")
            seen.add(s)
        self.assertEqual(len(order), len(STAGE_DAG))

    def test_cycle_detected(self):
        bad = {
            "a": StageNode("a", deps=("b",), fanout=1, kind="llm"),
            "b": StageNode("b", deps=("a",), fanout=1, kind="llm"),
        }
        with self.assertRaises(ValueError):
            topo_order(bad)

    def test_undefined_dep_detected(self):
        bad = {
            "a": StageNode("a", deps=("missing",), fanout=1, kind="llm"),
        }
        with self.assertRaises(ValueError):
            topo_order(bad)

    def test_downstream_includes_transitive(self):
        # rewrite → cast → prompts → images → compose → critic → upload
        # tts → asr → compose
        # Changing rewrite invalidates EVERYTHING downstream.
        ds = stages_invalidated_by("rewrite")
        self.assertIn("cast", ds)
        self.assertIn("prompts", ds)
        self.assertIn("images", ds)
        self.assertIn("tts", ds)
        self.assertIn("compose", ds)
        self.assertIn("critic", ds)
        self.assertIn("upload", ds)

    def test_downstream_of_leaf_is_empty(self):
        self.assertEqual(stages_invalidated_by("upload"), [])


# ---------------------------------------------------------------------------
# Pipeline runner — uses the real RewriteContract + CriticContract,
# mocks the LLM call layer.
# ---------------------------------------------------------------------------


# Reusable narration that satisfies the RewriteContract validator.
GOOD_NARRATION = (
    "Am I wrong for refusing my MIL Carol's wedding cake? "
    "I'm a professional baker. I said no thanks. "
    "She brought her own anyway. Two days before the wedding she ordered "
    "a four hundred dollar cake. The fondant was peeling. "
    "My fiancé sided with me. Carol stormed out. "
    "Now the family group chat is on fire. Was I out of line?"
)
GOOD_REWRITE_OUT = {
    "hook": "Am I wrong for refusing my MIL Carol's wedding cake?",
    "narration": GOOD_NARRATION,
    "title_options": ["MIL crashes wedding cake plans", "Cake fight", "MIL drama"],
}


class PipelineRunnerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = cache_mod.LocalCache(root=self._tmp.name)
        self.job = RenderJob(
            job_id="test-job",
            channel="mystoriesanimated",
            channel_cfg={"closer_format": "LIKE if YTA, COMMENT if NTA. AITA?"},
            raw_input={"slug": "test", "title": "MIL drama",
                       "body": "She brought a cake to the wedding.",
                       "url": "https://reddit.example", "source": "reddit"},
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_llm(self, sequence_by_stage: dict[str, list]):
        """Return a fake call_claude_cli that pops from per-stage queues."""
        def fake(prompt, *, stage=None, **kwargs):
            stack = sequence_by_stage.get(stage)
            if not stack:
                raise AssertionError(f"no fake LLM response queued for stage={stage!r}")
            return stack.pop(0)
        return fake

    def test_rewrite_stage_runs_with_cache_miss(self):
        fake = self._fake_llm({"rewrite": [GOOD_REWRITE_OUT]})
        # Patch the orchestrator's call_claude_cli — that's what the
        # contract goes through. Skip external stages to keep the test
        # focused on the LLM half.
        with patch.object(pipeline_runner, "run_stage",
                          wraps=pipeline_runner.run_stage) as wrapped:
            with patch("pipeline.llm.orchestrator.call_claude_cli", fake):
                result = pipeline_runner.run_pipeline(
                    self.job, cache=self.cache, skip_external=True,
                )
        # Rewrite ran exactly once and persisted into the job.
        self.assertEqual(self.job.script, GOOD_REWRITE_OUT)
        # Beats materialised from the narration so prompts/images can fan out.
        self.assertGreater(len(self.job.beats), 0)
        self.assertIsInstance(self.job.beats[0], BeatArtifact)

    def test_rewrite_cache_hit_skips_llm(self):
        # Pre-populate the cache as if a prior run produced this.
        from pipeline.llm import cache as cache_mod2  # local alias, avoid shadow
        key = cache_mod2.key_for(
            stage="rewrite", channel=self.job.channel,
            channel_cfg=self.job.channel_cfg,
            upstream=self.job.upstream_for("rewrite"),
            sub_index=None,
        )
        self.cache.put(key, cache_mod2.CacheRecord(output=GOOD_REWRITE_OUT))

        # If the LLM is called at all we'll know — fake() raises.
        def fake(*a, **k):
            raise AssertionError("LLM should not be called on cache hit")

        with patch("pipeline.llm.orchestrator.call_claude_cli", fake):
            result = pipeline_runner.run_pipeline(
                self.job, cache=self.cache, skip_external=True,
            )
        self.assertEqual(self.job.script, GOOD_REWRITE_OUT)
        self.assertEqual(result.cache_hits.get("rewrite"), 1)


class PipelineRunnerCriticCascadeTest(unittest.TestCase):
    """Critic returns FIX → cascade re-runs the named stages (cache-aware).

    Uses a tiny test-only DAG (rewrite → critic) so the cascade is
    isolated from compose / upload (which need external handlers).
    The cascade logic is the same regardless of DAG size.
    """

    # Minimal DAG for the cascade test: rewrite then critic.
    TEST_DAG = {
        "rewrite": StageNode("rewrite", deps=(),          fanout=1, kind="llm"),
        "critic":  StageNode("critic",  deps=("rewrite",), fanout=1, kind="llm"),
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = cache_mod.LocalCache(root=self._tmp.name)
        self.job = RenderJob(
            job_id="test-cascade",
            channel="mystoriesanimated",
            channel_cfg={"closer_format": "LIKE if YTA, COMMENT if NTA. AITA?"},
            raw_input={"slug": "x", "title": "MIL drama",
                       "body": "Story body.",
                       "url": "https://reddit.example", "source": "reddit"},
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_fix_verdict_cascade_invalidates_named_stage(self):
        # Critic verdict: FIX targeting rewrite.narration. The runner
        # should add rewrite back to pending and re-run it.
        critic_outputs = [
            {
                "verdict": "FIX",
                "weakest_param": "missing_cta_polish",
                "fixes": [{
                    "target_stage": "rewrite",
                    "target_path": "narration",
                    "constraint": "weak_pivot",
                    "severity": "error",
                    "reason": "the WAIT-WHAT pivot is too soft",
                }],
            },
            # Second pass critic — SHIP this time.
            {"verdict": "SHIP", "weakest_param": "", "fixes": []},
        ]
        rewrite_outputs = [GOOD_REWRITE_OUT, GOOD_REWRITE_OUT]   # initial + cascade

        def fake(prompt, *, stage=None, **kwargs):
            if stage == "rewrite":
                return rewrite_outputs.pop(0)
            if stage == "critic":
                return critic_outputs.pop(0)
            raise AssertionError(f"unexpected stage={stage!r}")

        with patch("pipeline.llm.orchestrator.call_claude_cli", fake):
            result = pipeline_runner.run_pipeline(
                self.job, dag=self.TEST_DAG, cache=self.cache,
                skip_external=True, max_critic_passes=2,
            )
        # Rewrite ran twice (initial + cascade), critic ran twice.
        self.assertEqual(rewrite_outputs, [],
                         msg="rewrite should have run twice (initial + cascade)")
        self.assertEqual(critic_outputs, [],
                         msg="critic should have run twice")
        self.assertEqual(result.critic_passes, 1)
        self.assertEqual(result.final_verdict, "SHIP")
        # The Fix that came back from critic was recorded on the job.
        self.assertEqual(len(self.job.fixes_applied), 1)
        self.assertEqual(self.job.fixes_applied[0].constraint, "weak_pivot")

    def test_critic_pass_cap_limits_cascade(self):
        # Critic keeps returning FIX; runner should cap at max_passes=1.
        always_fix = {
            "verdict": "FIX",
            "weakest_param": "stuck",
            "fixes": [{
                "target_stage": "rewrite", "target_path": "narration",
                "constraint": "wont_pass", "severity": "error",
                "reason": "model can't satisfy this",
            }],
        }
        critic_outputs = [dict(always_fix), dict(always_fix), dict(always_fix)]
        # Rewrite responses: initial + at-most-1 cascade re-run = 2 total.
        rewrite_outputs = [GOOD_REWRITE_OUT, GOOD_REWRITE_OUT, GOOD_REWRITE_OUT]

        def fake(prompt, *, stage=None, **kwargs):
            if stage == "rewrite":
                return rewrite_outputs.pop(0)
            if stage == "critic":
                return critic_outputs.pop(0)
            raise AssertionError(f"unexpected stage={stage!r}")

        with patch("pipeline.llm.orchestrator.call_claude_cli", fake):
            result = pipeline_runner.run_pipeline(
                self.job, dag=self.TEST_DAG, cache=self.cache,
                skip_external=True, max_critic_passes=1,
            )
        # max_passes=1 → 1 cascade. Critic called twice (initial + 1
        # cascade), rewrite twice.
        self.assertEqual(len(critic_outputs), 1,
                         msg="critic should have run twice (initial + 1 cascade)")
        self.assertEqual(len(rewrite_outputs), 1,
                         msg="rewrite should have run twice (initial + cascade)")
        self.assertEqual(result.critic_passes, 1)
        self.assertEqual(result.final_verdict, "FIX")


# ---------------------------------------------------------------------------
# Flavours — lightweight assertions on composition
# ---------------------------------------------------------------------------


class FlavoursTest(unittest.TestCase):
    def test_aita_only(self):
        from pipeline.llm.contracts.flavours import AITAFlavour, compose_flavours
        cs = compose_flavours([AITAFlavour()])
        names = {c.name for c in cs}
        self.assertIn("missing_cta", names)
        self.assertIn("banned_acronyms", names)

    def test_cliffhanger_replaces_aita_cta(self):
        from pipeline.llm import script_check
        from pipeline.llm.contracts.flavours import (
            AITAFlavour, CliffhangerFlavour, compose_flavours,
        )
        cs = compose_flavours([AITAFlavour(), CliffhangerFlavour()])
        cta = next(c for c in cs if c.name == "missing_cta")
        # Cliffhanger CTA examples should now be active.
        self.assertEqual(
            list(cta.examples_good),
            script_check.cta_examples(cliffhanger=True),
        )


if __name__ == "__main__":
    unittest.main()
