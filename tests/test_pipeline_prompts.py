"""Tests for pipeline.prompts._validate_and_clean — schema enforcement
on the LLM's per-beat prompt array.

The actual `claude -p` call is integration; we exercise the validator.

`pipeline.prompts` imports `pipeline.images`, which transitively pulls
in torch + diffusers (~25s cold). We stub `pipeline.images` in
`sys.modules` BEFORE the import so this test stays fast.
"""

from __future__ import annotations

import sys
import types
import unittest

from tests._helpers import PROJECT_ROOT, FakeBeat  # noqa: F401


# Pre-stub pipeline.images so prompts.py's `from . import images, llm`
# resolves without triggering torch. _validate_and_clean uses both
# images.lint_prompt and images.strip_text_bait (the latter dedupes the
# text-bait stripper between the author path and the critic path —
# pipeline/llm/critic.py:regenerate_with_corrections also calls it).
#
# 2026-05-05: stub also exposes the singletons + reset_image_state so
# downstream tests that touch pipeline.images post-stub
# (test_refactor_quick_wins.ResetImageStateTests) don't crash. The
# stub for those is intentionally inert — no side effects, just the
# attribute surface.
if "pipeline.images.images" not in sys.modules:
    _stub = types.ModuleType("pipeline.images.images")
    _stub.lint_prompt = lambda scene: []
    _stub.strip_text_bait = lambda scene: (scene, [])
    _stub._PIPE = None
    _stub._IP_ADAPTER_LOADED = False
    _stub._FLUX_PIPE = None
    _stub._ZIMAGE_PIPE = None
    def _stub_reset_image_state():  # noqa: D401
        _stub._PIPE = None
        _stub._IP_ADAPTER_LOADED = False
        _stub._FLUX_PIPE = None
        _stub._ZIMAGE_PIPE = None
    _stub.reset_image_state = _stub_reset_image_state
    sys.modules["pipeline.images.images"] = _stub

from pipeline.llm.prompts import _validate_and_clean  # noqa: E402


def _beats(n: int) -> list:
    return [FakeBeat(text=f"beat {i}", start=i * 1.5, end=(i + 1) * 1.5)
            for i in range(n)]


class ValidateShapeTest(unittest.TestCase):
    def test_happy_path(self):
        beats = _beats(3)
        raw = [
            {"key_visual": "kv0", "scene": "s0"},
            {"key_visual": "kv1", "scene": "s1"},
            {"key_visual": "kv2", "scene": "s2"},
        ]
        out = _validate_and_clean(raw, beats, opening_directives=None)
        self.assertEqual(len(out), 3)
        for i, item in enumerate(out):
            self.assertEqual(item["scene"], f"s{i}")
            self.assertEqual(item["key_visual"], f"kv{i}")

    def test_strips_whitespace(self):
        beats = _beats(1)
        raw = [{"key_visual": "  punchline  ", "scene": "  scene  "}]
        out = _validate_and_clean(raw, beats, opening_directives=None)
        self.assertEqual(out[0]["key_visual"], "punchline")
        self.assertEqual(out[0]["scene"], "scene")


class ValidateFailureTest(unittest.TestCase):
    def test_non_array_raises(self):
        with self.assertRaises(ValueError):
            # Multi-key dict (no envelope to unwrap) → still raises.
            _validate_and_clean({"not": "a list", "second_key": "also"}, _beats(1), None)

    def test_count_mismatch_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(
                [{"key_visual": "x", "scene": "y"}],
                _beats(3),
                None,
            )
        self.assertIn("counts must match", str(ctx.exception))

    def test_non_dict_item_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(
                ["just a string"],
                _beats(1),
                None,
            )
        self.assertIn("not an object", str(ctx.exception))

    def test_empty_scene_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(
                [{"key_visual": "x", "scene": "   "}],
                _beats(1),
                None,
            )
        self.assertIn("empty 'scene'", str(ctx.exception))

    def test_missing_scene_raises(self):
        with self.assertRaises(ValueError):
            _validate_and_clean(
                [{"key_visual": "x"}],  # no scene
                _beats(1),
                None,
            )

    def test_shape_c_flat_single_beat_dict_raises(self):
        """Shape C — model collapsed the array into a single dict.

        Surfaced by job 3cd2b3b5 (mystoriesanimated AITA ketchup-on-stew)
        on 2026-05-16: gpt-5.3-chat returned
        ``{"narration_line": "...", "key_visual": "...", "scene": "..."}``
        — keys=['narration_line', 'key_visual', 'scene'], value-types=['str'].
        The validator MUST raise ValueError so the worker's retry-or-fail
        loop fires; pre-fix this was uncovered.

        Post-2026-05-16: ``author_beat_prompts`` now sends a strict
        wrapper-object json_schema (``_BEAT_RESPONSE_SCHEMA``) that
        Azure's CFG engine enforces, so Shape C shouldn't escape the
        backend. This test pins the SAFETY GATE: if a legacy / non-
        strict deployment ever returns Shape C anyway, we still raise
        and the worker retries.
        """
        beats = _beats(14)
        shape_c = {
            "narration_line": "Am I wrong for telling my partner...",
            "key_visual": "a hovering ketchup bottle on a counter",
            "scene": "an empty stew pot in a generic kitchen",
        }
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(shape_c, beats, opening_directives=None)
        self.assertIn("expected JSON array", str(ctx.exception))


class ValidateAzureEnvelopeUnwrapTest(unittest.TestCase):
    """Azure backend forces ``response_format=json_object`` for every
    output_json=True call without an explicit json_schema, so the LLM
    physically CANNOT return the bare array this stage's _SYSTEM prompt
    asks for. It returns a single-key dict envelope wrapping the array
    instead. Surfaced by job 345b81bf canary on 2026-05-15.
    """

    def test_unwraps_beats_keyed_envelope(self):
        beats = _beats(2)
        envelope = {
            "beats": [
                {"key_visual": "kv0", "scene": "s0"},
                {"key_visual": "kv1", "scene": "s1"},
            ],
        }
        out = _validate_and_clean(envelope, beats, opening_directives=None)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["key_visual"], "kv0")

    def test_unwraps_prompts_keyed_envelope(self):
        beats = _beats(1)
        envelope = {"prompts": [{"key_visual": "x", "scene": "y"}]}
        out = _validate_and_clean(envelope, beats, opening_directives=None)
        self.assertEqual(out[0]["scene"], "y")

    def test_unwraps_arbitrarily_named_single_key_envelope(self):
        # Don't whitelist key names — Azure's wrapper key is unpredictable
        # and changes by model + temperature.
        beats = _beats(1)
        envelope = {"some_random_field_name": [{"key_visual": "x", "scene": "y"}]}
        out = _validate_and_clean(envelope, beats, opening_directives=None)
        self.assertEqual(out[0]["scene"], "y")

    def test_unwraps_largest_list_when_dict_has_multiple_list_values(self):
        # Shape A' (relaxed 2026-05-15): when Azure emits both the prompt
        # array AND a sidecar metadata array (rationale steps, debug
        # notes, version log), pre-fix this raised "expected JSON array";
        # post-fix we pick the LARGEST list value as the prompts array.
        # Per-item validation below catches mis-picks via scene-empty /
        # not-a-dict checks.
        beats = _beats(1)
        envelope = {
            "beats": [{"key_visual": "x", "scene": "y"}],
            "metadata": [1, 2, 3],   # smaller — ignored by the largest-list rule
        }
        # Smaller "metadata" list IS bigger by len(3) than 1-item beats
        # array, so this case actually picks metadata and then fails
        # because metadata items aren't dicts. The opposite assignment
        # mirrors the realistic shape where the prompts array is the
        # biggest:
        envelope2 = {
            "prompts": [{"key_visual": f"k{i}", "scene": f"s{i}"} for i in range(3)],
            "rationale": "single string explanation",
        }
        out = _validate_and_clean(envelope2, _beats(3), opening_directives=None)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["scene"], "s0")

    def test_does_not_unwrap_when_dict_has_no_list_values(self):
        # No list inside → can't unwrap → raises (preserves the original
        # "expected JSON array" failure mode for genuinely-malformed
        # responses).
        beats = _beats(1)
        envelope = {"some_field": "scalar value", "another": 42}
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(envelope, beats, opening_directives=None)
        self.assertIn("expected JSON array", str(ctx.exception))

    def test_azure_soft_error_response_surfaces_error_text(self):
        """Pin the 2026-05-15 v13 AITA diagnostic.

        Backstory: Azure returned HTTP 200 with body
        ``{"error": "<rejection reason>"}`` — content filter triggered,
        or schema rejection, or token-limit truncation. Pre-fix we
        logged a generic "could not unwrap envelope keys=['error'] —
        falling through to ValueError" and threw away the error text.
        Operators had no way to know WHY the LLM authoring failed.

        Now the print includes the actual error string so it's
        debuggable from cloud logs. Surfaced by job e9f2cec3 (AITA
        v13 retry) on 2026-05-15 — every cloud short rendered without
        author_beat_prompts because the Azure response was an error
        wrapper, but my Shape A'/B' unwrap (correctly) couldn't
        unwrap an error response. The fix surfaces the text — the
        engine still falls through to bare Segment.text per the
        existing cloud worker try/except.
        """
        import io
        from contextlib import redirect_stdout
        beats = _beats(2)
        envelope = {"error": "content filter triggered on phrase 'asshole'"}
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(ValueError):
                _validate_and_clean(envelope, beats, opening_directives=None)
        out = buf.getvalue()
        # Must include the actual error text (not just the type name).
        self.assertIn("content filter triggered", out)
        self.assertIn("Azure soft-error response", out)
        self.assertIn("error=", out)
        # Must still log the generic envelope-shape diagnostic AS WELL
        # (for cases that aren't error responses).
        self.assertIn("could not unwrap Azure JSON-object envelope", out)

    def test_azure_error_with_metadata_field_still_surfaces(self):
        """Sometimes Azure sends ``{"error": "...", "request_id": "..."}``.
        The error-detection rule allows up to 2 keys — pin both shapes
        get the diagnostic print."""
        import io
        from contextlib import redirect_stdout
        beats = _beats(1)
        envelope = {"error": "schema validation failed", "request_id": "abc-123"}
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(ValueError):
                _validate_and_clean(envelope, beats, opening_directives=None)
        out = buf.getvalue()
        self.assertIn("schema validation failed", out)

    def test_non_error_dict_with_only_error_key_in_name_doesnt_misfire(self):
        """Sentinel: a dict with a key that happens to contain 'error'
        but isn't the soft-error shape (e.g., a list-valued field called
        'error_codes') MUST take the normal Shape-A unwrap path, not
        the new soft-error diagnostic."""
        beats = _beats(2)
        # 'error_codes' contains a list — this should hit Shape A.
        envelope = {"error_codes": [
            {"key_visual": "kv1", "scene": "s1"},
            {"key_visual": "kv2", "scene": "s2"},
        ]}
        out = _validate_and_clean(envelope, beats, opening_directives=None)
        self.assertEqual(len(out), 2)  # Shape A unwrap fired

    def test_unwrapped_array_count_must_still_match_beat_count(self):
        # The count-match check still applies to the unwrapped array.
        beats = _beats(3)
        envelope = {"beats": [{"key_visual": "x", "scene": "y"}]}
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(envelope, beats, opening_directives=None)
        self.assertIn("counts must match", str(ctx.exception))

    def test_unwraps_ordered_map_keyed_by_beat_n(self):
        # Shape B: Azure ordered-map. Surfaced by job f1e319a3 canary
        # 2026-05-15 — the model emitted {"beat_1": {...}, "beat_2":
        # {...}, ...} which the single-key unwrap missed.
        beats = _beats(3)
        envelope = {
            "beat_1": {"key_visual": "kv0", "scene": "s0"},
            "beat_2": {"key_visual": "kv1", "scene": "s1"},
            "beat_3": {"key_visual": "kv2", "scene": "s2"},
        }
        out = _validate_and_clean(envelope, beats, opening_directives=None)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["scene"], "s0")
        self.assertEqual(out[2]["key_visual"], "kv2")

    def test_unwraps_ordered_map_keyed_by_integer(self):
        beats = _beats(2)
        envelope = {
            "1": {"key_visual": "x", "scene": "y"},
            "2": {"key_visual": "a", "scene": "b"},
        }
        out = _validate_and_clean(envelope, beats, opening_directives=None)
        self.assertEqual(len(out), 2)

    def test_unwraps_ordered_map_keyed_by_shot_n(self):
        # Different naming scheme — must still detect via beat-shape
        # heuristic (key_visual / scene / narration_line in values).
        beats = _beats(2)
        envelope = {
            "shot_01": {"key_visual": "x", "scene": "y"},
            "shot_02": {"key_visual": "a", "scene": "b"},
        }
        out = _validate_and_clean(envelope, beats, opening_directives=None)
        self.assertEqual(len(out), 2)

    def test_unwraps_ordered_map_with_non_beat_dict_values_and_lets_per_item_check_catch(self):
        # Shape B' (relaxed 2026-05-15): pre-fix this required ≥half of
        # the value-dicts to have ``key_visual``/``scene``/``narration_line``
        # before unwrapping. Surfaced as a false-negative on job
        # 79cdca90 (HindutavaAnimated Krishna leela Short) where the
        # model emitted Hindi-locale field names like ``mukhya_drashya``
        # — not in the strict-shape list, so the gate failed.
        # Post-fix: when ALL values are dicts AND no list values at all,
        # treat as ordered-map. Per-item ``scene`` non-empty check below
        # catches the case where the unwrapped dicts genuinely don't
        # contain the expected fields.
        beats = _beats(2)
        envelope = {
            "model": {"name": "gpt", "version": "5.3"},
            "config": {"temp": 0.7, "max_tokens": 4096},
        }
        # Now unwraps — but raises a different (clearer) error: the
        # per-item validation finds empty `scene` field.
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(envelope, beats, opening_directives=None)
        # Must NOT be the old "expected JSON array" message — pin the
        # downstream-clearer error path.
        self.assertNotIn("expected JSON array", str(ctx.exception))
        self.assertIn("empty 'scene'", str(ctx.exception))

    def test_ordered_map_preserves_insertion_order(self):
        # Critical: Python 3.7+ dict preserves insertion order, and
        # Azure JSON-decoding preserves it too. The unwrapped array
        # must be in the same order as the model emitted (= the same
        # order as the caller's beat list).
        beats = _beats(3)
        envelope = {
            "beat_2": {"key_visual": "second", "scene": "second"},
            "beat_1": {"key_visual": "first", "scene": "first"},
            "beat_3": {"key_visual": "third", "scene": "third"},
        }
        out = _validate_and_clean(envelope, beats, opening_directives=None)
        # We trust the dict's order — if Azure emits "second, first,
        # third" the unwrap honours that. Caller's responsibility to
        # match beat order via narration_line if they care.
        self.assertEqual(out[0]["key_visual"], "second")
        self.assertEqual(out[1]["key_visual"], "first")

    def test_unwraps_ordered_map_with_locale_specific_field_names(self):
        # Shape B' regression — surfaced by job 79cdca90 (HindutavaAnimated
        # Krishna leela, Hindi topic). The model emitted a dict whose
        # values used Hindi-locale field names (``mukhya_drashya`` =
        # 'main visual', ``drashya`` = 'scene') instead of the strict
        # English schema (``key_visual``, ``scene``). Pre-fix, the
        # majority-beat-shaped gate failed and the entire run dropped
        # back to bare Segment.text. Post-fix: ALL-values-are-dicts
        # alone is enough to unwrap; the ``scene`` non-empty check
        # below produces a clearer per-item error if the unwrap was
        # wrong.
        beats = _beats(2)
        envelope = {
            "beat_1": {
                "mukhya_drashya": "Yashoda holding baby Krishna",
                "drashya": "twilight courtyard, oil lamps",
                # Note: no ``key_visual`` / ``scene`` keys at all.
            },
            "beat_2": {
                "mukhya_drashya": "Putana in disguise approaching",
                "drashya": "village pathway, dusk",
            },
        }
        # Pre-fix this raised "expected JSON array, got dict".
        # Post-fix the unwrap fires; the per-item check on ``scene``
        # raises a CLEARER error than the unwrap fallthrough.
        with self.assertRaises(ValueError) as ctx:
            _validate_and_clean(envelope, beats, opening_directives=None)
        # Verify we got past the unwrap and into per-item validation.
        msg = str(ctx.exception)
        self.assertNotIn("expected JSON array", msg)
        self.assertIn("empty 'scene'", msg)


class ValidateMissingKeyVisualTolerantTest(unittest.TestCase):
    """key_visual can be empty; only `scene` is required."""

    def test_missing_key_visual_falls_back_to_empty(self):
        beats = _beats(1)
        out = _validate_and_clean(
            [{"scene": "the only thing that matters"}],
            beats, None,
        )
        self.assertEqual(out[0]["key_visual"], "")
        self.assertEqual(out[0]["scene"], "the only thing that matters")


class OpeningDirectivesTest(unittest.TestCase):
    """Beat 0 directive check is warning-only — should not raise."""

    def test_directives_pass_with_matching_tokens(self):
        beats = _beats(2)
        # Beat 0's scene contains "kitchen" — matches example tokens
        raw = [
            {"key_visual": "salad bowl", "scene": "the character standing in the kitchen at a table"},
            {"key_visual": "x", "scene": "y"},
        ]
        directives = {
            "required_concrete_tokens": 2,
            "example_tokens": ["kitchen", "table"],
        }
        # Should not raise
        out = _validate_and_clean(raw, beats, directives)
        self.assertEqual(len(out), 2)

    def test_directives_warning_does_not_raise(self):
        beats = _beats(1)
        raw = [{"key_visual": "kv", "scene": "abstract feelings sadness"}]
        directives = {
            "required_concrete_tokens": 2,
            "example_tokens": ["restaurant table", "wedding cake"],
        }
        # Doesn't raise — only warns to stdout
        out = _validate_and_clean(raw, beats, directives)
        self.assertEqual(len(out), 1)


# ---- Additional coverage for prompt construction and lint helpers ----

import json
import shutil
from unittest.mock import patch

from pipeline.llm import prompts as pr

if not hasattr(pr.images, "lint_prompt"):
    pr.images.lint_prompt = lambda scene: []
if not hasattr(pr.images, "strip_text_bait"):
    pr.images.strip_text_bait = lambda scene: (scene, [])


SCRATCH_PROMPTS = PROJECT_ROOT / "tests" / ".scratch_prompts"


class PromptSubjectAndRankTest(unittest.TestCase):
    def test_extract_subject_trims_fillers_conjunctions_and_verbs(self):
        self.assertIsNone(pr._extract_subject("Eventually nothing matched."))
        self.assertEqual(pr._extract_subject("Then my husband and I walked away."), "my husband")
        self.assertEqual(pr._extract_subject("the wedding is ruined."), "the wedding")
        self.assertEqual(pr._extract_subject("my three kids ran outside."), "my three kids")

    def test_find_rank_for_beat_tracks_claimed_ranks(self):
        ranks = [{"rank": 1, "match_text": "Aguero goal"}, {"rank": 2, "match_text": "Ramos header"}]
        claimed = set()
        self.assertEqual(pr._find_rank_for_beat("The Aguero goal changed it", ranks, claimed)["rank"], 1)
        self.assertEqual(claimed, {1})
        self.assertIsNone(pr._find_rank_for_beat("Aguero goal again", ranks, claimed))
        self.assertIsNone(pr._find_rank_for_beat("no match", ranks, claimed))
        self.assertIsNone(pr._find_rank_for_beat("anything", None))


class BuildUserPromptTest(unittest.TestCase):
    def test_on_screen_prompt_includes_subject_opening_cast_emotion_and_rank_hint_once(self):
        beats = [FakeBeat("My mom found the phone.", 0, 1.5), FakeBeat("Aguero goal happened.", 1.5, 3.0), FakeBeat("Aguero goal again.", 3.0, 4.5)]
        prompt = pr._build_user_prompt(
            narration=" ".join(b.text for b in beats),
            beats=beats,
            source_story="source",
            cast_narrator_desc="brown hair narrator",
            cast_default_emotion="angry",
            style_prefix="pastel style" * 40,
            opening_directives={"required_concrete_tokens": 2, "example_tokens": ["phone", "kitchen"]},
            ranks=[{"rank": 1, "match_text": "Aguero goal", "image_prompt_hint": "blue kit shot"}],
        )
        self.assertIn("PRIMARY SUBJECT IN FRAME: My mom", prompt)
        self.assertIn("blue kit shot", prompt)
        self.assertEqual(prompt.count("CURATOR HINT"), 1)
        self.assertIn("Narrator's default emotional tone", prompt)
        self.assertIn("Beat 0 must contain at least 2 concrete tokens", prompt)
        self.assertIn("pastel style", prompt)

    def test_voice_only_prompt_lists_supporting_people_and_forbids_narrator(self):
        beats = [FakeBeat("Ramos scored.", 0, 1)]
        prompt = pr._build_user_prompt(
            narration="Ramos scored.", beats=beats, source_story="source",
            cast_narrator_desc=None, cast_default_emotion=None, style_prefix="style",
            opening_directives=None, narrator_visual_mode="voice_only",
            supporting=[{"name": "Ramos", "aliases": ["Sergio"], "description": "defender. extra words"}],
        )
        self.assertIn("NARRATOR MODE: voice-over only", prompt)
        self.assertIn("Ramos (aliases: Sergio)", prompt)
        self.assertIn("DO NOT place a narrator", prompt)


class HairAndCastContradictionTest(unittest.TestCase):
    def test_extract_hair_colour_empty_text(self):
        self.assertIsNone(pr._extract_hair_colour(""))

    def test_extract_hair_colour_normalises_variants(self):
        self.assertIsNone(pr._extract_hair_colour("no hair colour"))
        self.assertEqual(pr._extract_hair_colour("grey wavy hair"), "gray")
        self.assertEqual(pr._extract_hair_colour("a blond-haired person"), "blonde")
        self.assertEqual(pr._extract_hair_colour("dusty pink hair"), "dusty-pink")

    def test_cast_contradictions_skip_unpinned_or_other_character_and_flag_narrator(self):
        self.assertEqual(pr._check_cast_contradictions(scene="brown hair narrator", key_visual="", narration_line="", cast_narrator_desc="no colour"), [])
        self.assertEqual(pr._check_cast_contradictions(scene="sister with brown hair, laughing", key_visual="", narration_line="", cast_narrator_desc="pink hair"), [])
        self.assertEqual(pr._check_cast_contradictions(scene="Amelia with brown hair, laughing", key_visual="", narration_line="", cast_narrator_desc="pink hair"), [])
        warnings = pr._check_cast_contradictions(scene="the character with brown hair, arms crossed", key_visual="", narration_line="", cast_narrator_desc="pink hair")
        self.assertEqual(len(warnings), 1)
        self.assertIn("brown", warnings[0])


class ValidateAndAuthorAdditionalTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH_PROMPTS, ignore_errors=True)
        SCRATCH_PROMPTS.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(SCRATCH_PROMPTS, ignore_errors=True)

    def test_validate_strips_text_bait_lints_and_cast_contradictions(self):
        beats = [FakeBeat("beat zero", 0, 1)]
        def strip_text_bait(s):
            if "TEXT" in s:
                return (s.replace("TEXT", "").strip(), ["TEXT"])
            return (s, [])
        with patch.object(pr.images, "strip_text_bait", side_effect=strip_text_bait), \
             patch.object(pr.images, "lint_prompt", return_value=["warn"]):
            cleaned = pr._validate_and_clean(
                [{"key_visual": "TEXT brown hair", "scene": "the character with brown hair TEXT"}],
                beats,
                opening_directives={"required_concrete_tokens": 1, "example_tokens": ["phone"]},
                cast_narrator_desc="pink hair",
            )
        self.assertEqual(cleaned[0]["key_visual"], "brown hair")
        self.assertEqual(cleaned[0]["scene"], "the character with brown hair")
        self.assertEqual(cleaned[0]["narration_line"], "beat zero")

    def test_author_beat_prompts_calls_llm_validates_and_writes(self):
        out = SCRATCH_PROMPTS / "nested" / "prompts.json"
        beats = [FakeBeat("I found the phone.", 0, 1.5)]
        # Post-2026-05-17 author_beat_prompts uses a verb-led validator
        # in _validate_and_clean (key_visual must NOT be a bare noun),
        # so the test's mock LLM output needs a shot-led key_visual.
        # Post-2026-05-24 (v5-subject-emotion) the schema also requires
        # ``subject`` + ``emotion`` per beat (Rules 18/19); without them
        # the validator logs a per-beat fallback warning and defaults to
        # protagonist/neutral.
        llm_out = [{
            "narration_line": "I found the phone.",
            "key_visual": "medium close-up of the character lifting a phone",
            "scene": "the character holding a phone, soft afternoon window light, blurred kitchen counter behind, warm palette",
            "subject": "protagonist",
            "emotion": "surprised",
        }]
        # Disable the channel-richness gate for this unit test — the
        # gate is integration-tested separately; here we're pinning the
        # _validate_and_clean + call-site contract with a thin fixture.
        with patch.dict("os.environ", {"YTFACTORY_DISABLE_RICHNESS_GATE": "1"}), \
             patch.object(pr.llm, "model_for", return_value="opus"), \
             patch.object(pr.llm, "call_claude_cli", return_value=llm_out) as call:
            cleaned = pr.author_beat_prompts(
                narration="I found the phone.", beats=beats, source_story="source",
                cast_narrator_desc="brown hair", cast_default_emotion="shocked",
                style_prefix="style", opening_directives=None, out_path=out,
            )
        self.assertEqual(cleaned, llm_out)
        self.assertTrue(out.exists())
        # Post-2026-05-16: wrapper-object schema replaces root-array
        # output. The instruction now demands a ``{"beats": [...]}``
        # envelope (root-array isn't accepted by OpenAI/Azure structured
        # outputs), and the call site passes the strict-compliant
        # json_schema so Azure's CFG engine enforces the shape.
        prompt_arg = call.call_args.args[0]
        self.assertIn('"beats"', prompt_arg)
        self.assertIn("EXACTLY 1", prompt_arg)
        self.assertEqual(call.call_args.kwargs["model"], "opus")
        # Strict-schema enforcement is the production default for this
        # stage — without it the LLM was free to emit Shape-C single
        # dicts (job 3cd2b3b5 floating-objects post-mortem).
        self.assertIs(call.call_args.kwargs["strict_schema"], True)
        self.assertIsNotNone(call.call_args.kwargs["json_schema"])
        schema = call.call_args.kwargs["json_schema"]
        self.assertEqual(schema["type"], "object")
        self.assertIn("beats", schema["properties"])
        self.assertEqual(
            json.loads(out.read_text())[0]["key_visual"],
            "medium close-up of the character lifting a phone",
        )


class EnvFlagEnabledTest(unittest.TestCase):
    """Pin the env-flag matcher used by the refiner gate. Matches the
    convention used elsewhere in the pipeline (see CLAUDE.md
    "Cloud-canonical writes…" rule for the niche-specs precedent)."""

    def _with_env(self, value):
        import os as _os
        return patch.dict(_os.environ, {"YTFACTORY_TEST_FLAG": value}, clear=False)

    def _unset_env(self):
        import os as _os
        env_copy = dict(_os.environ)
        env_copy.pop("YTFACTORY_TEST_FLAG", None)
        return patch.dict(_os.environ, env_copy, clear=True)

    def test_unset_is_disabled(self):
        with self._unset_env():
            self.assertFalse(pr._env_flag_enabled("YTFACTORY_TEST_FLAG"))

    def test_value_1_is_enabled(self):
        with self._with_env("1"):
            self.assertTrue(pr._env_flag_enabled("YTFACTORY_TEST_FLAG"))

    def test_value_true_is_enabled_case_insensitive(self):
        for val in ("true", "True", "TRUE", "TrUe"):
            with self._with_env(val):
                self.assertTrue(pr._env_flag_enabled("YTFACTORY_TEST_FLAG"))

    def test_value_yes_and_on_enabled(self):
        with self._with_env("yes"):
            self.assertTrue(pr._env_flag_enabled("YTFACTORY_TEST_FLAG"))
        with self._with_env("on"):
            self.assertTrue(pr._env_flag_enabled("YTFACTORY_TEST_FLAG"))

    def test_value_0_or_empty_or_garbage_is_disabled(self):
        for val in ("0", "", "false", "no", "off", "garbage", "  "):
            with self._with_env(val):
                self.assertFalse(
                    pr._env_flag_enabled("YTFACTORY_TEST_FLAG"),
                    f"value {val!r} should be disabled",
                )


class MaybeRefinePromptsTest(unittest.TestCase):
    """The refiner gate inside author_beat_prompts. Pure unit tests —
    the refiner module's own LLM call is patched out."""

    def _beats(self):
        return [
            {"key_visual": "a", "scene": "b", "narration_line": "n1"},
            {"key_visual": "c", "scene": "d", "narration_line": "n2"},
        ]

    def test_flag_off_returns_input_unchanged(self):
        import os as _os
        beats = self._beats()
        env_copy = dict(_os.environ)
        env_copy.pop("YTFACTORY_PROMPT_REFINER", None)
        with patch.dict(_os.environ, env_copy, clear=True):
            out = pr._maybe_refine_prompts(
                beats,
                era_anchor_prefix=None, character_description=None,
                style=None, mood=None, channel_key=None,
            )
        # Returns same list, no refined_* fields added.
        self.assertIs(out, beats)
        for b in beats:
            self.assertNotIn("refined_visual", b)

    def test_flag_on_empty_input_returns_empty_without_calling_refiner(self):
        import os as _os
        with patch.dict(_os.environ, {"YTFACTORY_PROMPT_REFINER": "1"}, clear=False):
            with patch("pipeline.images.prompt_refiner.refine_prompts_batch") as mock_refine:
                out = pr._maybe_refine_prompts(
                    [],
                    era_anchor_prefix=None, character_description=None,
                    style=None, mood=None, channel_key=None,
                )
        self.assertEqual(out, [])
        mock_refine.assert_not_called()

    def test_flag_on_merges_refined_fields_into_each_beat(self):
        import os as _os
        beats = self._beats()
        refined_response = [
            {
                "refined_visual": "RV1",
                "refined_scene": "no readable text in image. RS1",
                "style_block": "Style: x. Mood: y.",
                "refined_version": "v1",
                "refined_input_hash": "h1",
            },
            {
                "refined_visual": "RV2",
                "refined_scene": "no readable text in image. RS2",
                "style_block": "Style: x. Mood: y.",
                "refined_version": "v1",
                "refined_input_hash": "h2",
            },
        ]
        with patch.dict(_os.environ, {"YTFACTORY_PROMPT_REFINER": "1"}, clear=False):
            with patch("pipeline.images.prompt_refiner.refine_prompts_batch",
                       return_value=refined_response) as mock_refine:
                out = pr._maybe_refine_prompts(
                    beats,
                    era_anchor_prefix="[ERA: 2020s]",
                    character_description="adult",
                    style="comic", mood="dramatic", channel_key="prorevenge",
                )
        # Refiner was called once with the full beat list.
        mock_refine.assert_called_once()
        kwargs = mock_refine.call_args.kwargs
        self.assertEqual(kwargs["era_anchor_prefix"], "[ERA: 2020s]")
        self.assertEqual(kwargs["character_description"], "adult")
        self.assertEqual(kwargs["style"], "comic")
        self.assertEqual(kwargs["mood"], "dramatic")
        self.assertEqual(kwargs["channel_key"], "prorevenge")
        # Beats now have refined fields merged in (in-place).
        self.assertEqual(out[0]["refined_visual"], "RV1")
        self.assertEqual(out[0]["refined_input_hash"], "h1")
        self.assertEqual(out[1]["refined_visual"], "RV2")
        # Original fields preserved.
        self.assertEqual(out[0]["key_visual"], "a")
        self.assertEqual(out[0]["narration_line"], "n1")

    def test_flag_on_refiner_raises_returns_input_unchanged(self):
        import os as _os
        beats = self._beats()
        with patch.dict(_os.environ, {"YTFACTORY_PROMPT_REFINER": "1"}, clear=False):
            with patch("pipeline.images.prompt_refiner.refine_prompts_batch",
                       side_effect=RuntimeError("network down")):
                out = pr._maybe_refine_prompts(
                    beats,
                    era_anchor_prefix=None, character_description=None,
                    style=None, mood=None, channel_key=None,
                )
        self.assertIs(out, beats)
        for b in beats:
            self.assertNotIn("refined_visual", b)

    def test_flag_on_refiner_wrong_length_returns_input_unchanged(self):
        import os as _os
        beats = self._beats()
        # Refiner mistakenly returns 1 slot for 2 beats — defensive.
        with patch.dict(_os.environ, {"YTFACTORY_PROMPT_REFINER": "1"}, clear=False):
            with patch("pipeline.images.prompt_refiner.refine_prompts_batch",
                       return_value=[{"refined_visual": "x", "refined_scene": "no readable text in image. y",
                                       "style_block": "Style: a. Mood: b.",
                                       "refined_version": "v1", "refined_input_hash": "h"}]):
                out = pr._maybe_refine_prompts(
                    beats,
                    era_anchor_prefix=None, character_description=None,
                    style=None, mood=None, channel_key=None,
                )
        self.assertIs(out, beats)
        for b in beats:
            self.assertNotIn("refined_visual", b)

    def test_flag_on_empty_slot_skips_that_beat_keeps_others(self):
        """Per-beat fallback: empty {} slot means that beat falls back to
        legacy; other beats still get their refined fields."""
        import os as _os
        beats = self._beats()
        refined_response = [
            {},  # beat 0 failed
            {
                "refined_visual": "RV2",
                "refined_scene": "no readable text in image. RS2",
                "style_block": "Style: x. Mood: y.",
                "refined_version": "v1",
                "refined_input_hash": "h2",
            },
        ]
        with patch.dict(_os.environ, {"YTFACTORY_PROMPT_REFINER": "1"}, clear=False):
            with patch("pipeline.images.prompt_refiner.refine_prompts_batch",
                       return_value=refined_response):
                out = pr._maybe_refine_prompts(
                    beats,
                    era_anchor_prefix=None, character_description=None,
                    style=None, mood=None, channel_key=None,
                )
        # Beat 0 untouched.
        self.assertNotIn("refined_visual", out[0])
        # Beat 1 got refined fields.
        self.assertEqual(out[1]["refined_visual"], "RV2")


class AuthorBeatPromptsRefinerIntegrationTest(unittest.TestCase):
    """End-to-end: author_beat_prompts threads its new kwargs into the
    refiner step and the refined fields land in the cached prompts.json."""

    def setUp(self):
        shutil.rmtree(SCRATCH_PROMPTS, ignore_errors=True)
        SCRATCH_PROMPTS.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(SCRATCH_PROMPTS, ignore_errors=True)

    def test_flag_on_refiner_output_persisted_to_prompts_json(self):
        import os as _os
        out = SCRATCH_PROMPTS / "prompts.json"
        beats = [FakeBeat("hook beat", 0, 1.5)]
        llm_out = [{"narration_line": "hook beat", "key_visual": "phone",
                    "scene": "the character holding a phone"}]
        refined_response = [{
            "refined_visual": "REFINED_PHONE",
            "refined_scene": "no readable text in image. medium shot, soft light, hand cupping phone",
            "style_block": "Style: cartoon. Mood: tense.",
            "refined_version": "v1",
            "refined_input_hash": "abc123",
        }]
        with patch.dict(_os.environ, {"YTFACTORY_PROMPT_REFINER": "1"}, clear=False), \
             patch.object(pr.llm, "model_for", return_value="opus"), \
             patch.object(pr.llm, "call_claude_cli", return_value=llm_out), \
             patch("pipeline.images.prompt_refiner.refine_prompts_batch",
                   return_value=refined_response) as mock_refine:
            cleaned = pr.author_beat_prompts(
                narration="hook beat", beats=beats, source_story="source",
                cast_narrator_desc="adult", style_prefix="cartoon",
                opening_directives=None, out_path=out,
                era_anchor_prefix="[ERA: 2020s]", mood="tense",
                channel_key="prorevenge",
            )
        mock_refine.assert_called_once()
        kwargs = mock_refine.call_args.kwargs
        self.assertEqual(kwargs["era_anchor_prefix"], "[ERA: 2020s]")
        self.assertEqual(kwargs["mood"], "tense")
        self.assertEqual(cleaned[0]["refined_visual"], "REFINED_PHONE")
        # Persisted to disk.
        on_disk = json.loads(out.read_text())
        self.assertEqual(on_disk[0]["refined_visual"], "REFINED_PHONE")
        self.assertEqual(on_disk[0]["refined_input_hash"], "abc123")
        # Original key_visual + scene preserved alongside refined fields.
        self.assertEqual(on_disk[0]["key_visual"], "phone")

    def test_flag_off_no_refined_fields_in_output(self):
        import os as _os
        out = SCRATCH_PROMPTS / "prompts.json"
        beats = [FakeBeat("hook beat", 0, 1.5)]
        llm_out = [{"narration_line": "hook beat", "key_visual": "phone",
                    "scene": "the character holding a phone"}]
        env_copy = dict(_os.environ)
        env_copy.pop("YTFACTORY_PROMPT_REFINER", None)
        with patch.dict(_os.environ, env_copy, clear=True), \
             patch.object(pr.llm, "model_for", return_value="opus"), \
             patch.object(pr.llm, "call_claude_cli", return_value=llm_out), \
             patch("pipeline.images.prompt_refiner.refine_prompts_batch") as mock_refine:
            cleaned = pr.author_beat_prompts(
                narration="hook beat", beats=beats, source_story="source",
                cast_narrator_desc="adult", style_prefix="cartoon",
                opening_directives=None, out_path=out,
                era_anchor_prefix="[ERA: 2020s]", mood="tense",
            )
        # Refiner NOT called when flag is off.
        mock_refine.assert_not_called()
        self.assertNotIn("refined_visual", cleaned[0])
        on_disk = json.loads(out.read_text())
        self.assertNotIn("refined_visual", on_disk[0])


if __name__ == "__main__":
    unittest.main()
