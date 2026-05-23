"""Tests for pipeline.images.prompt_refiner — the FLUX.2 [klein] DALL-E 3-style
prompt-refiner pre-step.

The refiner makes ONE batched LLM call per render. These tests exercise:

  * the deterministic helpers (``shot_for_beat``, ``sanitize_attractors``,
    ``compute_input_hash``) without touching the LLM
  * the validator + per-beat fallback path in ``refine_prompts_batch`` using
    a stub ``llm_call`` injected through the test seam — no network, no
    sys.modules patching
  * the whole-batch failure / kill-switch behaviour (LLM raises, malformed
    output, wrong array length) returning ``[{}] * N``
  * the post-LLM ``strip_text_bait`` sanitisation that clears a beat whose
    refined fields snuck a banned token back in

We DO NOT poison ``sys.modules["pipeline.images.images"]`` here — the
real module imports in <200ms and stubbing globally would leak the stub
into other test files that need the real ``build_full_prompt``. Use
``unittest.mock.patch.object`` to override ``strip_text_bait`` per-test.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401 — sets sys.path

from pipeline.images import images as _images_mod
from pipeline.images import prompt_refiner as pr


def _strip_passthrough(scene):
    """Default test strip_text_bait: passes scene through unchanged."""
    return (scene, [])


def _strip_with_behaviour(behaviour):
    """Returns a strip_text_bait fn that uses ``behaviour`` dict to
    decide which inputs trigger which (clean, removed) responses."""
    def _strip(scene):
        return behaviour.get(scene, (scene, []))
    return _strip


def _install_strip(test_self, behaviour=None):
    """Patch ``images.strip_text_bait`` for the duration of ``test_self``.

    Auto-restores via ``addCleanup``. Pattern is borrowed from
    ``unittest.TestCase`` convention so test isolation is bulletproof —
    no sys.modules poisoning, no leak to other test files.
    """
    strip_fn = _strip_with_behaviour(behaviour) if behaviour else _strip_passthrough
    patcher = patch.object(_images_mod, "strip_text_bait", strip_fn)
    patcher.start()
    test_self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# Pure helpers


class RefinerCalibrationTest(unittest.TestCase):
    """P4.1: pin that the refiner is calibrated for Z-Image-Turbo, not
    FLUX.2 klein. The previous calibration emitted 4-10-word noun
    phrases that underspecified z-turbo and let it default to
    product-photo backgrounds. Regression-guard so a future revert to
    klein vocabulary breaks loudly."""

    def test_system_prompt_targets_z_image_turbo(self):
        sys_text = pr._REFINER_SYSTEM.lower()
        self.assertIn("z-image-turbo", sys_text,
                      "refiner system prompt must declare Z-Image-Turbo as target")
        # Klein-specific tokens must not appear (BFL, qwen3, flux2, klein
        # would all signal the old calibration is back).
        self.assertNotIn("flux.2 [klein]", sys_text)
        self.assertNotIn("flux.2 klein", sys_text)
        self.assertNotIn("qwen3", sys_text)

    def test_system_prompt_targets_80_to_250_word_range(self):
        """Z-turbo's optimal prompt length is ~80-250 words — the system
        prompt must explicitly aim there (4-10-word refined_visual from
        the klein era would crash z-turbo into floating-object product
        photos)."""
        sys_text = pr._REFINER_SYSTEM
        # The new system prompt must mention z-turbo's word-count sweet
        # spot somewhere in the calibration guidance.
        self.assertTrue(
            "80-250" in sys_text or "80 to 250" in sys_text,
            "system prompt must declare z-turbo's 80-250-word sweet spot",
        )

    def test_refiner_version_bumped_off_v1(self):
        """Bumping ``REFINER_VERSION`` invalidates cached refined-* fields
        from the klein era; v1 is the klein-era version, anything else
        signals z-turbo calibration is live."""
        self.assertNotEqual(pr.REFINER_VERSION, "v1")


class ShotRotationTest(unittest.TestCase):
    def test_returns_canonical_shot_for_beat_zero(self):
        self.assertEqual(
            pr.shot_for_beat(0),
            pr.SHOT_ROTATION[0],
        )

    def test_wraps_around_modulo_length(self):
        n = len(pr.SHOT_ROTATION)
        self.assertEqual(pr.shot_for_beat(n), pr.SHOT_ROTATION[0])
        self.assertEqual(pr.shot_for_beat(n + 3), pr.SHOT_ROTATION[3])
        self.assertEqual(pr.shot_for_beat(2 * n + 5), pr.SHOT_ROTATION[5])

    def test_consecutive_beats_are_distinct(self):
        # The whole point of injecting a rotation is to avoid the
        # frozen-first-11s class-of-bug. Adjacent indices must NOT share
        # a shot type, otherwise we re-enter the same attractor.
        for i in range(len(pr.SHOT_ROTATION) - 1):
            self.assertNotEqual(
                pr.shot_for_beat(i),
                pr.shot_for_beat(i + 1),
                f"beats {i} and {i+1} got the same shot — rotation must "
                "guarantee distinctness across the rolling window",
            )


class SanitizeAttractorsTest(unittest.TestCase):
    def test_empty_input_returns_empty(self):
        self.assertEqual(pr.sanitize_attractors(""), "")

    def test_no_triggers_returns_input_unchanged(self):
        clean = "a calm man at a kitchen counter, morning light"
        self.assertEqual(pr.sanitize_attractors(clean), clean)

    def test_rewrites_set_a_tiny_legal_trap(self):
        # The exact phrase that triggered the t_15/t_16 Like+Subscribe
        # scene in the 7ea772a0 render — re-rewrite must catch it.
        out = pr.sanitize_attractors("I set a tiny legal trap for my roommate")
        self.assertIn("left a small note", out)
        self.assertNotIn("trap", out)

    def test_rewrites_youtube_to_neutral_platform_name(self):
        out = pr.sanitize_attractors("a YouTube tutorial on viral videos")
        self.assertNotIn("YouTube", out)
        self.assertIn("the platform", out)
        # "viral videos" matches the social-phrasing regex → "popular content"
        self.assertIn("popular content", out)

    def test_bare_viral_in_non_social_context_preserved(self):
        # Rubber-duck 2026-05-14: narrowed the viral regex to phrases
        # like "going viral" / "viral video" so medical/science usage
        # ("viral infection") is left alone.
        for sentence in [
            "viral infection spreading through the office",
            "the viral load remained low",
            "a viral mutation appeared in week three",
        ]:
            out = pr.sanitize_attractors(sentence)
            self.assertEqual(
                out, sentence,
                f"narrow viral regex must not mangle {sentence!r}",
            )

    def test_going_viral_phrase_rewritten(self):
        out = pr.sanitize_attractors("the post was going viral overnight")
        self.assertNotIn("going viral", out)
        self.assertIn("becoming popular", out)

    def test_viral_video_phrase_rewritten(self):
        out = pr.sanitize_attractors("she filmed a viral clip")
        self.assertNotIn("viral clip", out.lower())
        self.assertIn("popular content", out)

    def test_rewrites_subscribe_family(self):
        for verb in ["subscribe", "subscribed", "subscribers"]:
            out = pr.sanitize_attractors(f"please {verb} for more")
            self.assertIn("follow", out, f"{verb!r} not rewritten")

    def test_is_case_insensitive(self):
        out = pr.sanitize_attractors("CLICKBAIT thumbnail")
        self.assertNotIn("CLICKBAIT", out.upper().replace("EYE-CATCHING HEADLINE", ""))
        self.assertIn("eye-catching headline", out.lower())

    def test_is_idempotent(self):
        once = pr.sanitize_attractors("set a tiny trap and going viral")
        twice = pr.sanitize_attractors(once)
        self.assertEqual(once, twice)


class ComputeInputHashTest(unittest.TestCase):
    def _beat(self, kv="a cup of coffee", scene="man stirring"):
        return {"key_visual": kv, "scene": scene}

    def test_stable_across_identical_inputs(self):
        h1 = pr.compute_input_hash(
            beat=self._beat(),
            era_anchor_prefix="[ERA: 2020s]",
            character_description="adult man, brown hair",
            style="comic illustration",
            mood="dramatic",
        )
        h2 = pr.compute_input_hash(
            beat=self._beat(),
            era_anchor_prefix="[ERA: 2020s]",
            character_description="adult man, brown hair",
            style="comic illustration",
            mood="dramatic",
        )
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16, "16-char hash for cache compactness")

    def test_hash_changes_when_key_visual_changes(self):
        h1 = pr.compute_input_hash(
            beat=self._beat(kv="a cup of coffee"),
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
        )
        h2 = pr.compute_input_hash(
            beat=self._beat(kv="a slice of toast"),
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
        )
        self.assertNotEqual(h1, h2)

    def test_hash_changes_when_scene_changes(self):
        # Critic-patched scenes auto-invalidate the cache via this branch.
        h1 = pr.compute_input_hash(
            beat=self._beat(scene="man stirring"),
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
        )
        h2 = pr.compute_input_hash(
            beat=self._beat(scene="man pouring"),
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
        )
        self.assertNotEqual(h1, h2)

    def test_hash_changes_when_era_anchor_changes(self):
        common = dict(
            beat=self._beat(),
            character_description=None,
            style=None, mood=None,
        )
        h1 = pr.compute_input_hash(era_anchor_prefix="[ERA: 1258]", **common)
        h2 = pr.compute_input_hash(era_anchor_prefix="[ERA: 2020s]", **common)
        self.assertNotEqual(h1, h2)

    def test_hash_changes_when_character_changes(self):
        common = dict(
            beat=self._beat(),
            era_anchor_prefix=None,
            style=None, mood=None,
        )
        h1 = pr.compute_input_hash(character_description="adult man, brown hair", **common)
        h2 = pr.compute_input_hash(character_description="adult woman, red hair", **common)
        self.assertNotEqual(h1, h2)

    def test_whitespace_normalised(self):
        common = dict(
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
        )
        h1 = pr.compute_input_hash(beat=self._beat(kv="coffee"), **common)
        h2 = pr.compute_input_hash(beat=self._beat(kv="  coffee  "), **common)
        self.assertEqual(h1, h2)

    def test_unit_separator_join_prevents_field_collision(self):
        # Pathological case: putting field 2's content into the end of
        # field 1 should NOT collide with the natural layout. The
        # unit-separator delimiter in the hash assembly guarantees this.
        common = dict(
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
        )
        h1 = pr.compute_input_hash(
            beat={"key_visual": "a", "scene": "b"}, **common,
        )
        h2 = pr.compute_input_hash(
            beat={"key_visual": "a\u241fb", "scene": ""}, **common,
        )
        # Even though naive concat would collide, separator joins them
        # uniquely.
        self.assertNotEqual(h1, h2)


# ---------------------------------------------------------------------------
# refine_prompts_batch — the main entry point


def _good_item(visual: str, scene_tail: str, style="comic", mood="dramatic"):
    """Helper: produce a refiner-LLM response item that passes validation."""
    return {
        "refined_visual": visual,
        "refined_scene": f"no readable text in image. {scene_tail}",
        "style_block": f"Style: {style}. Mood: {mood}.",
    }


class RefineBatchEmptyInputTest(unittest.TestCase):
    def test_empty_beats_returns_empty_list_without_calling_llm(self):
        # Sentinel to prove the LLM is not called for an empty batch.
        called = []
        def llm_call(*a, **k):  # noqa: ARG001
            called.append(1)
            raise AssertionError("LLM should not be invoked on empty input")

        out = pr.refine_prompts_batch(
            [], era_anchor_prefix=None, character_description=None,
            style=None, mood=None, llm_call=llm_call,
        )
        self.assertEqual(out, [])
        self.assertEqual(called, [])


class RefineBatchHappyPathTest(unittest.TestCase):
    def setUp(self):
        # Reset the strip stub between tests so previous bait-strip
        # behaviour doesn't bleed in.
        _install_strip(self)

    def test_returns_one_dict_per_beat(self):
        beats = [
            {"key_visual": "cup of coffee", "scene": "man stirring"},
            {"key_visual": "sticky note", "scene": "hand pressing"},
        ]
        def llm_call(prompt, *, output_json, model, stage):  # noqa: ARG001
            assert stage == "prompt_refine"
            assert model == "haiku"
            assert output_json is True
            return [
                _good_item("cup of coffee", "medium shot, soft window light, calm posture"),
                _good_item("sticky note", "extreme close-up, harsh overhead light, finger pressing"),
            ]
        out = pr.refine_prompts_batch(
            beats,
            era_anchor_prefix=None, character_description=None,
            style="comic", mood="dramatic",
            llm_call=llm_call,
        )
        self.assertEqual(len(out), 2)
        for slot in out:
            self.assertIn("refined_visual", slot)
            self.assertIn("refined_scene", slot)
            self.assertIn("style_block", slot)
            self.assertIn("refined_version", slot)
            self.assertEqual(slot["refined_version"], pr.REFINER_VERSION)
            self.assertIn("refined_input_hash", slot)
            self.assertEqual(len(slot["refined_input_hash"]), 16)

    def test_refined_scene_keeps_anti_text_prefix(self):
        beats = [{"key_visual": "x", "scene": "y"}]
        def llm_call(*a, **k):  # noqa: ARG001
            return [_good_item("x", "medium shot, soft light, neutral posture")]
        out = pr.refine_prompts_batch(
            beats,
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
            llm_call=llm_call,
        )
        self.assertTrue(
            out[0]["refined_scene"].lower().startswith("no readable text in image"),
            f"got: {out[0]['refined_scene']!r}",
        )


class RefineBatchValidationTest(unittest.TestCase):
    """Per-beat malformed output → that beat slot is {}, batch survives."""

    def setUp(self):
        _install_strip(self)

    def test_item_missing_refined_visual_clears_that_beat_only(self):
        beats = [{"key_visual": "a", "scene": "b"}, {"key_visual": "c", "scene": "d"}]
        def llm_call(*a, **k):  # noqa: ARG001
            return [
                {"refined_visual": "", "refined_scene": "no readable text in image. ok", "style_block": "Style: x. Mood: y."},
                _good_item("c", "wide shot, soft light, calm"),
            ]
        out = pr.refine_prompts_batch(
            beats,
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
            llm_call=llm_call,
        )
        self.assertEqual(out[0], {})
        self.assertTrue(out[1])

    def test_item_missing_anti_text_prefix_clears_that_beat(self):
        beats = [{"key_visual": "a", "scene": "b"}]
        def llm_call(*a, **k):  # noqa: ARG001
            return [{
                "refined_visual": "a",
                "refined_scene": "medium shot, no anti-text prefix here",
                "style_block": "Style: x. Mood: y.",
            }]
        out = pr.refine_prompts_batch(
            beats,
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
            llm_call=llm_call,
        )
        self.assertEqual(out[0], {})

    def test_item_not_an_object_clears_that_beat(self):
        beats = [{"key_visual": "a", "scene": "b"}, {"key_visual": "c", "scene": "d"}]
        def llm_call(*a, **k):  # noqa: ARG001
            return ["not a dict", _good_item("c", "wide shot, light, calm")]
        out = pr.refine_prompts_batch(
            beats,
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
            llm_call=llm_call,
        )
        self.assertEqual(out[0], {})
        self.assertTrue(out[1])

    def test_post_llm_strip_text_bait_clears_beat_that_leaked_banned_word(self):
        # The refiner LLM emitted "speech bubble" again — our deterministic
        # post-pass must catch it and clear the slot so render falls back.
        _install_strip(self, behaviour={
            "evil-visual": ("evil-visual", ["speech bubble"]),
        })
        beats = [{"key_visual": "a", "scene": "b"}]
        def llm_call(*a, **k):  # noqa: ARG001
            return [{
                "refined_visual": "evil-visual",
                "refined_scene": "no readable text in image. ok",
                "style_block": "Style: x. Mood: y.",
            }]
        out = pr.refine_prompts_batch(
            beats,
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
            llm_call=llm_call,
        )
        self.assertEqual(out[0], {})


class RefineBatchKillSwitchTest(unittest.TestCase):
    """Whole-batch failure → [{}] * N. Render falls through to legacy path."""

    def setUp(self):
        _install_strip(self)

    def test_llm_raises_returns_empty_dicts(self):
        beats = [{"key_visual": "a", "scene": "b"} for _ in range(3)]
        def llm_call(*a, **k):  # noqa: ARG001
            raise RuntimeError("network down")
        out = pr.refine_prompts_batch(
            beats,
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
            llm_call=llm_call,
        )
        self.assertEqual(out, [{}, {}, {}])

    def test_llm_returns_non_list_returns_empty_dicts(self):
        beats = [{"key_visual": "a", "scene": "b"}]
        def llm_call(*a, **k):  # noqa: ARG001
            return {"refined_visual": "oops, not a list"}
        out = pr.refine_prompts_batch(
            beats,
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
            llm_call=llm_call,
        )
        self.assertEqual(out, [{}])

    def test_llm_returns_wrong_length_returns_empty_dicts(self):
        beats = [{"key_visual": "a", "scene": "b"} for _ in range(3)]
        def llm_call(*a, **k):  # noqa: ARG001
            return [_good_item("only one", "wide shot, light, calm")]
        out = pr.refine_prompts_batch(
            beats,
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
            llm_call=llm_call,
        )
        self.assertEqual(out, [{}, {}, {}])


class RefineBatchAttractorSanitisationTest(unittest.TestCase):
    """The LLM must NEVER see attractor trigger words from the narration."""

    def setUp(self):
        _install_strip(self)

    def test_trap_phrase_sanitised_before_llm_sees_it(self):
        seen_prompt = []
        beats = [{
            "key_visual": "I set a tiny legal trap",
            "scene": "going viral on YouTube",
        }]
        def llm_call(prompt, *, output_json, model, stage):  # noqa: ARG001
            seen_prompt.append(prompt)
            return [_good_item("a small note", "wide shot, soft light, hand placing")]
        pr.refine_prompts_batch(
            beats,
            era_anchor_prefix=None, character_description=None,
            style=None, mood=None,
            llm_call=llm_call,
        )
        sent = seen_prompt[0]
        # The system prompt's rule 4 enumerates banned tokens by name
        # ("YouTube logo, Subscribe button…") so we can't assert against
        # the whole prompt — that block is supposed to mention them. We
        # care that the BEAT content the LLM ingests as input (the
        # narration-derived stuff) has no triggers. Split at the BEATS
        # section anchor and inspect only the trailing payload.
        _, _, beats_section = sent.partition("BEATS TO REFINE")
        self.assertTrue(beats_section, "beats section missing from user prompt")
        self.assertNotIn("YouTube", beats_section)
        self.assertNotIn("viral", beats_section.lower())
        # The "I set a tiny legal trap" phrase IS in our rewrite map and
        # should land as "I left a small note" in the beat content.
        self.assertNotIn("legal trap", beats_section.lower())
        self.assertIn("left a small note", beats_section)
        # And the YouTube/viral rewrites are present:
        self.assertIn("popular", beats_section)
        self.assertIn("the platform", beats_section)


class RefineBatchInputHashTest(unittest.TestCase):
    """Cache invalidation rides on the hash. Verify it lands correctly."""

    def setUp(self):
        _install_strip(self)

    def test_hash_in_output_matches_recomputed_hash(self):
        beat = {"key_visual": "k", "scene": "s"}
        def llm_call(*a, **k):  # noqa: ARG001
            return [_good_item("k", "wide shot, soft light, calm")]
        out = pr.refine_prompts_batch(
            [beat],
            era_anchor_prefix="[ERA: 2020s]",
            character_description="adult",
            style="comic", mood="dramatic",
            llm_call=llm_call,
        )
        expected = pr.compute_input_hash(
            beat=beat,
            era_anchor_prefix="[ERA: 2020s]",
            character_description="adult",
            style="comic", mood="dramatic",
        )
        self.assertEqual(out[0]["refined_input_hash"], expected)


class RefinedFieldsForRenderGateTest(unittest.TestCase):
    """Render-time gate enforces env flag + hash freshness. Without this,
    a cleared env flag would still honour cached refined fields and the
    kill switch wouldn't kill."""

    def _make_beat(self, *, era="ERA1", character="char1", style="s1",
                   mood="m1", key_visual="kv", scene="sc"):
        beat = {
            "key_visual": key_visual,
            "scene": scene,
            "refined_visual": "RV",
            "refined_scene": "no readable text in image. RS",
            "style_block": "Style: a. Mood: b.",
            "refined_version": pr.REFINER_VERSION,
        }
        beat["refined_input_hash"] = pr.compute_input_hash(
            beat=beat,
            era_anchor_prefix=era,
            character_description=character,
            style=style,
            mood=mood,
        )
        return beat

    def _env(self, value):
        import os
        return _patch_env("YTFACTORY_PROMPT_REFINER", value)

    def test_env_flag_off_returns_none_triple(self):
        beat = self._make_beat()
        with self._env(None):  # unset
            out = pr.refined_fields_for_render(
                beat,
                era_anchor_prefix="ERA1", character_description="char1",
                style="s1", mood="m1",
            )
        self.assertEqual(out, (None, None, None))

    def test_env_flag_explicit_zero_returns_none_triple(self):
        beat = self._make_beat()
        with self._env("0"):
            out = pr.refined_fields_for_render(
                beat,
                era_anchor_prefix="ERA1", character_description="char1",
                style="s1", mood="m1",
            )
        self.assertEqual(out, (None, None, None))

    def test_env_on_and_hash_matches_returns_refined(self):
        beat = self._make_beat()
        with self._env("1"):
            out = pr.refined_fields_for_render(
                beat,
                era_anchor_prefix="ERA1", character_description="char1",
                style="s1", mood="m1",
            )
        self.assertEqual(out, ("RV", "no readable text in image. RS", "Style: a. Mood: b."))

    def test_env_on_but_critic_patched_scene_falls_back(self):
        """Critic patches beat['scene'] in prompts.json — the input hash
        no longer matches what the refiner ran against. This branch is
        what protects against rendering with stale refined_* fields."""
        beat = self._make_beat()
        beat["scene"] = "the critic patched this scene"
        with self._env("1"):
            out = pr.refined_fields_for_render(
                beat,
                era_anchor_prefix="ERA1", character_description="char1",
                style="s1", mood="m1",
            )
        self.assertEqual(out, (None, None, None))

    def test_env_on_but_era_changed_falls_back(self):
        beat = self._make_beat()
        with self._env("1"):
            out = pr.refined_fields_for_render(
                beat,
                era_anchor_prefix="ERA_DIFFERENT",
                character_description="char1",
                style="s1", mood="m1",
            )
        self.assertEqual(out, (None, None, None))

    def test_env_on_but_character_changed_falls_back(self):
        beat = self._make_beat()
        with self._env("1"):
            out = pr.refined_fields_for_render(
                beat,
                era_anchor_prefix="ERA1",
                character_description="char_DIFFERENT",
                style="s1", mood="m1",
            )
        self.assertEqual(out, (None, None, None))

    def test_env_on_but_version_bumped_falls_back(self):
        beat = self._make_beat()
        beat["refined_version"] = "v0_obsolete"
        with self._env("1"):
            out = pr.refined_fields_for_render(
                beat,
                era_anchor_prefix="ERA1", character_description="char1",
                style="s1", mood="m1",
            )
        self.assertEqual(out, (None, None, None))

    def test_env_on_but_refined_field_missing_falls_back(self):
        for field in ("refined_visual", "refined_scene", "style_block"):
            beat = self._make_beat()
            beat[field] = ""
            with self._env("1"):
                out = pr.refined_fields_for_render(
                    beat,
                    era_anchor_prefix="ERA1", character_description="char1",
                    style="s1", mood="m1",
                )
            self.assertEqual(
                out, (None, None, None),
                f"empty {field!r} must trigger fallback",
            )

    def test_env_on_but_no_refined_fields_at_all_falls_back(self):
        """A cached prompts.json from BEFORE the refiner shipped has no
        refined_* fields. Must fall back without error."""
        legacy_beat = {"key_visual": "kv", "scene": "sc"}
        with self._env("1"):
            out = pr.refined_fields_for_render(
                legacy_beat,
                era_anchor_prefix="ERA1", character_description="char1",
                style="s1", mood="m1",
            )
        self.assertEqual(out, (None, None, None))


def _patch_env(name, value):
    """Context manager helper: set name to value (or unset if None)."""
    import os
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        prev = os.environ.get(name)
        try:
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
            yield
        finally:
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev
    return _ctx()


if __name__ == "__main__":
    unittest.main()
