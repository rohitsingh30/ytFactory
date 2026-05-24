"""Regression test for the 2026-05-24 refiner Shape-C fix
(REFINER_VERSION v3-scene-anchor → v4-wrapper-schema).

Background: ``refine_prompts_batch`` called
``call_llm(output_json=True)`` without ``json_schema``. Azure structured
outputs reject root-array types, so the request fell back to
``response_format={"type":"json_object"}`` — which structurally forces
a single root object. The system prompt instructed "return a JSON
array of N objects" but the LLM physically could not comply; it
collapsed every batch into one object. Result: ``fallback_count = n``
on every call → every beat fell back to legacy. On 2026-05-24 the
shorts render (job 88d98126) shipped looking coherent only because the
legacy fallback path on shorts has ``character_description`` baked
into ``build_full_prompt``. On long-form (job c4aed485) the legacy
fallback would have produced floating-jersey panels, so the new
safety net at ``long_form_lib:817`` correctly raised.

These tests pin:

1. ``_REFINER_RESPONSE_SCHEMA`` exists and is strict-compliant per
   Azure docs (additionalProperties=false, every property required,
   no unsupported keywords).
2. ``refine_prompts_batch`` passes ``json_schema=_REFINER_RESPONSE_SCHEMA``
   and ``strict_schema=True`` to ``call_llm`` — not the bare default
   that triggers Shape-C.
3. The parser unwraps ``{"refined_beats": [...]}`` correctly.
4. ``REFINER_VERSION`` bumped past v3-scene-anchor so cached refined
   fields auto-invalidate.

See memory ``project_shape_c_azure_structured_output_array_bug.md``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.images import images as _images_mod
from pipeline.images import prompt_refiner as _r
from pipeline.images.prompt_refiner import (
    REFINER_VERSION,
    _REFINER_RESPONSE_SCHEMA,
    refine_prompts_batch,
)


@pytest.fixture(autouse=True)
def _strip_text_bait_noop():
    """Patch ``strip_text_bait`` to pass through unchanged.

    The post-LLM sanitiser strips the bare word "text" — which is in the
    canonical anti-text prefix "no readable text in image." that every
    refined_scene MUST start with (Rule 1 of the refiner system prompt).
    The bait list and the anti-text prefix conflict, so production
    callers' tests universally patch this out. The conflict is a known
    pre-existing issue separate from the Shape-C wrapper-schema fix
    we're pinning here.
    """
    with patch.object(
        _images_mod, "strip_text_bait", lambda s: (s, []),
    ):
        yield


# ---- schema shape -------------------------------------------------------


def test_refiner_response_schema_is_a_wrapper_object_not_root_array() -> None:
    """Root-array types are rejected by Azure structured outputs. The
    schema must be a wrapper object with the array nested inside —
    same pattern as _BEAT_RESPONSE_SCHEMA in pipeline/llm/prompts.py."""
    assert _REFINER_RESPONSE_SCHEMA["type"] == "object", (
        "_REFINER_RESPONSE_SCHEMA root type must be 'object', not "
        "'array'. Azure structurally rejects root arrays — see memory "
        "project_shape_c_azure_structured_output_array_bug.md."
    )
    assert "refined_beats" in _REFINER_RESPONSE_SCHEMA["properties"], (
        "Wrapper must nest the array under 'refined_beats' (matches the "
        "system prompt instruction)."
    )
    assert (
        _REFINER_RESPONSE_SCHEMA["properties"]["refined_beats"]["type"]
        == "array"
    ), "refined_beats must be an array of refined-field objects."


def test_refiner_response_schema_is_strict_compliant() -> None:
    """Per Azure docs: every object has additionalProperties=false,
    every property in required, no unsupported keywords."""
    assert _REFINER_RESPONSE_SCHEMA.get("additionalProperties") is False
    assert "refined_beats" in _REFINER_RESPONSE_SCHEMA.get("required", [])
    items = _REFINER_RESPONSE_SCHEMA["properties"]["refined_beats"]["items"]
    assert items["additionalProperties"] is False
    for field in ("refined_visual", "refined_scene", "style_block"):
        assert field in items["required"], (
            f"item field {field!r} must be in 'required' for strict "
            f"schema compliance"
        )
        assert field in items["properties"]
    # No unsupported keywords (Azure docs).
    for forbidden in ("minItems", "maxItems", "pattern", "format"):
        # Check at every level — wrapper and items.
        for node in (
            _REFINER_RESPONSE_SCHEMA,
            _REFINER_RESPONSE_SCHEMA["properties"]["refined_beats"],
            items,
        ):
            assert forbidden not in node, (
                f"strict-compliant schema must not contain {forbidden!r}"
            )


# ---- version bump -------------------------------------------------------


def test_refiner_version_bumped_past_v3() -> None:
    """v3-scene-anchor caches were generated WITHOUT the wrapper-schema
    fix and might be poisoned; v4 invalidates them via the hash."""
    assert REFINER_VERSION != "v3-scene-anchor", (
        f"REFINER_VERSION must be bumped past v3-scene-anchor so cached "
        f"refined fields from before the wrapper-schema fix get "
        f"invalidated. Got {REFINER_VERSION!r}."
    )
    assert REFINER_VERSION.startswith("v") and REFINER_VERSION != "v3"


# ---- call_llm contract --------------------------------------------------


def test_refine_prompts_batch_passes_strict_wrapper_schema_to_llm() -> None:
    """refine_prompts_batch must pass json_schema=_REFINER_RESPONSE_SCHEMA
    and strict_schema=True so Azure enforces the wrapper-object shape
    at the token-generation level (CFG)."""
    captured: dict = {}

    def _fake_llm_call(prompt, *, output_json, model, stage, json_schema=None,
                       strict_schema=False):
        captured["output_json"] = output_json
        captured["json_schema"] = json_schema
        captured["strict_schema"] = strict_schema
        captured["model"] = model
        captured["stage"] = stage
        # Return a valid wrapper response so the rest of the function
        # path is exercised.
        return {
            "refined_beats": [
                {
                    "refined_visual": "wide establishing shot of subject",
                    "refined_scene": (
                        "no readable text in image. living room interior, "
                        "soft diffused daylight from the window."
                    ),
                    "style_block": (
                        "Style: warm hand-drawn 2D illustration. Mood: "
                        "calm, observational."
                    ),
                },
            ],
        }

    out = refine_prompts_batch(
        [{"key_visual": "kv_0", "scene": "the subject sitting on a sofa"}],
        era_anchor_prefix=None,
        character_description=None,
        style="warm 2D illustration",
        mood="calm",
        llm_call=_fake_llm_call,
    )
    assert captured["output_json"] is True
    assert captured["json_schema"] is _REFINER_RESPONSE_SCHEMA, (
        "refine_prompts_batch must pass _REFINER_RESPONSE_SCHEMA as the "
        "json_schema kwarg to call_llm, not None (the v3 bug)."
    )
    assert captured["strict_schema"] is True, (
        "strict_schema must be True so Azure's CFG token-level "
        "enforcement prevents the LLM from emitting a non-wrapper shape."
    )
    assert captured["stage"] == "prompt_refine"
    # And the parser unwrapped correctly — 1 beat in, 1 refined field
    # out.
    assert len(out) == 1
    assert out[0].get("refined_visual", "").startswith("wide")
    assert out[0].get("refined_version") == REFINER_VERSION


# ---- parser unwrap ------------------------------------------------------


def test_refine_prompts_batch_unwraps_wrapper_object() -> None:
    """Parser must accept the canonical wrapper shape
    ``{"refined_beats": [...]}`` returned by the v4 strict schema."""

    def _fake(prompt, *, output_json, model, stage, json_schema=None,
              strict_schema=False):
        return {
            "refined_beats": [
                {
                    "refined_visual": f"shot type {i}",
                    "refined_scene": (
                        "no readable text in image. setting "
                        f"{i}, cinematic warm key light from the left."
                    ),
                    "style_block": f"Style: s{i}. Mood: m{i}.",
                }
                for i in range(3)
            ],
        }

    beats = [{"key_visual": f"kv_{i}", "scene": f"s{i}"} for i in range(3)]
    out = refine_prompts_batch(
        beats,
        era_anchor_prefix=None,
        character_description=None,
        style=None,
        mood=None,
        llm_call=_fake,
    )
    assert len(out) == 3
    for i, item in enumerate(out):
        assert item["refined_visual"].startswith(f"shot type {i}")
        assert item["refined_scene"].startswith("no readable text in image.")
        assert item["style_block"].startswith(f"Style: s{i}.")


def test_refine_prompts_batch_raises_when_llm_returns_bare_dict() -> None:
    """Defense-in-depth: if for any reason the LLM returns a bare dict
    (the pre-v4 Shape-C failure mode), the refiner must RAISE instead
    of being interpreted as a single beat — and instead of silently
    returning [{}] * n which the pre-2026-05-24 contract did.

    The downstream legacy build_full_prompt path prepends the
    protagonist character_description onto every panel and produces
    cast-collapse (job 39d1ec2d). Raising surfaces the failure as a
    job error instead of silently degrading. See
    /ai/known-fragility.md F26 + memory silent-fallback-unshippable-output.
    """

    def _fake(prompt, *, output_json, model, stage, json_schema=None,
              strict_schema=False):
        # The exact Shape-C symptom: one root object instead of the
        # wrapper. Pre-fix this collapsed the whole batch silently.
        return {
            "refined_visual": "single object",
            "refined_scene": "kitchen with afternoon light",
            "style_block": "Style: x. Mood: y.",
        }

    beats = [{"key_visual": f"kv_{i}", "scene": f"s{i}"} for i in range(3)]
    with pytest.raises(RuntimeError, match="(dict_returned|reason=dict_returned)"):
        refine_prompts_batch(
            beats,
            era_anchor_prefix=None,
            character_description=None,
            style=None,
            mood=None,
            llm_call=_fake,
        )


def test_refine_prompts_batch_still_accepts_bare_list_for_test_seams() -> None:
    """Back-compat: legacy injection seams in tests may still return a
    raw list directly (the pre-v4 contract). The parser accepts both."""

    def _fake(prompt, *, output_json, model, stage):
        # Legacy seam signature — no json_schema/strict_schema kwargs.
        # Returns a bare list.
        return [
            {
                "refined_visual": f"kv {i}",
                "refined_scene": (
                    f"no readable text in image. legacy {i}, golden-hour "
                    "backlight."
                ),
                "style_block": f"Style: x{i}. Mood: y{i}.",
            }
            for i in range(2)
        ]

    beats = [{"key_visual": f"kv_{i}", "scene": f"s{i}"} for i in range(2)]
    out = refine_prompts_batch(
        beats,
        era_anchor_prefix=None,
        character_description=None,
        style=None,
        mood=None,
        llm_call=_fake,
    )
    assert len(out) == 2
    for i, item in enumerate(out):
        assert item["refined_visual"] == f"kv {i}"
