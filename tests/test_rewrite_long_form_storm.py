"""Regression tests for the STORM-pattern rewriter.

Replaces the pre-2026-05-13 single-shot tests. The new architecture
is two-phase (outline + parallel section bodies + aggregator), so the
test surface is fundamentally different:

  * Outline call returns a structured spine (verified per-call).
  * Section-body calls run in parallel via ThreadPoolExecutor —
    each call mocked to return a per-section narration.
  * Aggregator stitches them into a ScriptEnvelope.
  * Validator runs once at the end (no contract-retry-during-call).

The tests assert STORM mechanics, not LLM content quality (that's
what the canary render verifies).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import pipeline.llm.rewrite_long_form as _rlf
from pipeline.critic_long_form import LongFormContractError


# ---------- helpers ------------------------------------------------------


def _make_outline(
    *, n_sections: int = 10, n_panels: int = 24,
    niche_words: str = "shadow doorway watching figure dread fear cold",
) -> dict:
    """Build an outline payload that passes every contract check."""
    return {
        "hook": niche_words + " " + ("dread shadow watching " * 5),
        "thesis": "A test thesis.",
        "sections": [
            {
                "id": f"sec-{i}",
                "title": f"Section {i}",
                "brief": f"This section establishes {i}. " * 4,
                "target_words": 450,
                "visual_brief": f"A scene for section {i}.",
            }
            for i in range(n_sections)
        ],
        "panel_briefs": [
            {
                "scene": f"panel scene {i} with shadow watching",
                "hold_s": 6.0,
                "after_section_id": f"sec-{i % n_sections}",
            }
            for i in range(n_panels)
        ],
        "sources": [],
        "title_options": [
            "There is something standing in the hallway",
            "The figure was watching me from the doorway",
            "The shadow appeared at midnight",
        ],
    }


def _make_section_body(*, words: int = 450, niche_words: str = "shadow watching dread") -> dict:
    """Build a section-body payload with the requested word count.

    Includes niche-tonal tokens scattered throughout so the validator's
    niche-tonal lexicon check (first 200 words) passes when the body
    is the first content the validator sees. Content is deliberately
    fluffy filler — these tests verify STORM mechanics, not LLM
    quality (canary render verifies content quality).
    """
    # First 200 words MUST contain niche tokens (shadow / watching /
    # dread / figure / etc) for r/nosleep validator. Pad rest with
    # plausible word-noise so total word count meets target.
    head = ("The shadow stood watching from the doorway. A figure waited "
            "in the hallway, dread filling the room. Cold seeped through "
            "the floor. Something whispered behind the closet door. " * 6)
    head_words = head.split()
    pad_target = max(0, words - len(head_words))
    pad = " ".join(["filler"] * pad_target)
    nar = (head + " " + pad).strip()
    sentences = [s.strip() + "." for s in nar.split(".") if s.strip()][:50]
    return {"narration": nar, "sentences": sentences}


# ---------- Phase 1: outline call ----------------------------------------


def test_outline_prompt_includes_niche_title_rules_for_nosleep():
    """When niche=r/nosleep, outline prompt MUST inject the per-niche
    title-rule block."""
    raw_story = {"slug": "x", "title": "T", "body": "Source body about " + "shadow " * 100}
    with patch.object(_rlf._llm, "call_claude_cli") as mocked:
        mocked.return_value = _make_outline()
        # Stub Phase 2 + validator so we can isolate Phase 1.
        with patch.object(_rlf, "_generate_all_section_bodies") as gb:
            gb.return_value = [
                {**s, "_body": _make_section_body(words=450)}
                for s in mocked.return_value["sections"]
            ]
            _rlf.rewrite_long_form(
                raw_story, channel_cfg={"niche": "r/nosleep"},
                target_duration_s=600,
            )
    # First call is the outline. Verify the prompt body.
    outline_prompt = mocked.call_args_list[0][0][0]
    assert "noun-anchor (room / door / hallway" in outline_prompt
    assert "dread verb" in outline_prompt


def test_outline_prompt_omits_niche_rules_for_unknown_niche():
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    with patch.object(_rlf._llm, "call_claude_cli") as mocked:
        mocked.return_value = _make_outline()
        with patch.object(_rlf, "_generate_all_section_bodies") as gb:
            gb.return_value = [
                {**s, "_body": _make_section_body(words=450)}
                for s in mocked.return_value["sections"]
            ]
            _rlf.rewrite_long_form(
                raw_story, channel_cfg={"niche": "r/totally_made_up"},
                target_duration_s=600,
            )
    outline_prompt = mocked.call_args_list[0][0][0]
    # No niche-specific block when the niche isn't registered.
    assert "NICHE-SPECIFIC TITLE RULES" not in outline_prompt


def test_outline_call_uses_dedicated_stage_name():
    """Telemetry separates outline calls from section-body calls so
    the dashboard can show per-phase latency."""
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    with patch.object(_rlf._llm, "call_claude_cli") as mocked:
        mocked.return_value = _make_outline()
        with patch.object(_rlf, "_generate_all_section_bodies") as gb:
            gb.return_value = [
                {**s, "_body": _make_section_body(words=450)}
                for s in mocked.return_value["sections"]
            ]
            _rlf.rewrite_long_form(
                raw_story, channel_cfg={"niche": "r/nosleep"},
                target_duration_s=600,
            )
    outline_kwargs = mocked.call_args_list[0][1]
    assert outline_kwargs.get("stage") == "rewrite_long_form_outline"


# ---------- Phase 2: parallel section bodies -----------------------------


def test_section_bodies_call_one_llm_per_section():
    """N sections in outline → N section-body LLM calls."""
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    outline = _make_outline(n_sections=8)
    section_body = _make_section_body(words=450)

    call_count = [0]
    def _mock(*args, **kwargs):
        call_count[0] += 1
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        return section_body

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=600,
        )
    # 1 outline call + 8 section-body calls = 9 total
    assert call_count[0] == 1 + 8
    # Aggregated env preserves all 8 sections
    assert len(env.long_form.sections) == 8


def test_section_body_call_includes_outline_summary():
    """Each section-body call should see a compact summary of the OTHER
    sections (so it doesn't duplicate / contradict)."""
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    outline = _make_outline(n_sections=4)
    captured_section_prompts: list[str] = []

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        if kwargs.get("stage") == "rewrite_long_form_section":
            captured_section_prompts.append(prompt)
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=600,
        )

    assert len(captured_section_prompts) == 4
    # Every section-body prompt must contain the outline summary block.
    for prompt in captured_section_prompts:
        assert "Outline of all sections" in prompt
        # And the THIS SECTION marker should appear exactly once.
        assert prompt.count("← THIS SECTION") == 1
        # Each prompt must include all 4 section ids.
        for i in range(4):
            assert f"sec-{i}" in prompt


def test_section_body_failure_falls_back_to_brief():
    """When ONE section's body call fails (after retry), the
    aggregator falls back to the section's brief as narration so the
    render at least has SOMETHING for that slot — under-delivery
    surfaces via the validator."""
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    outline = _make_outline(n_sections=4)

    call_state = {"sec-2_count": 0}
    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        # Identify the requested section by the "← THIS SECTION" marker.
        section_id = "?"
        for line in prompt.split("\n"):
            if "← THIS SECTION" in line:
                start = line.find("[")
                end = line.find("]")
                if start >= 0 and end > start:
                    section_id = line[start + 1:end]
                break
        # Fail every call for sec-2.
        # Post-2026-05-20 retry budget: 1 initial + 2 retries = 3 in
        # the parallel fan-out, then the auto-repair pass re-calls
        # short sections up to 2 more times (since sec-2 fell back to
        # brief and is therefore under min_words). Total: 5 calls.
        if section_id == "sec-2":
            call_state["sec-2_count"] += 1
            raise RuntimeError("simulated section-body failure")
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        # The validator now downgrades length-only hard violations to
        # soft after the auto-repair pass, so rewrite_long_form should
        # ALWAYS return an envelope — never raise — when only length
        # is the issue. sec-2 falls back to its brief content.
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={},
            target_duration_s=600,
        )
        sec2 = next((s for s in env.long_form.sections if s.id == "sec-2"), None)
        assert sec2 is not None
        assert "establishes 2" in sec2.narration  # the brief content

    # sec-2 hit: 3 in the parallel fan-out (1 initial + 2 retries) +
    # 2 in the auto-repair pass = 5 total calls before giving up.
    assert call_state["sec-2_count"] == 5


def test_section_body_content_filter_does_not_retry():
    """When a section body trips Azure content_filter, retrying with
    the same prompt won't help — content_filter is deterministic per
    prompt. Each PASS (initial fan-out, auto-repair) must skip retry
    within itself on content_filter and fall back to brief immediately.

    Post-2026-05-20 behavior: 1 call in the initial fan-out + 1 call
    in the auto-repair pass = 2 total. The repair-pass call uses a
    different prompt (the emphasis block is filled in), so it's
    legitimately worth trying once. What this test guards is the
    within-pass content-filter retry skip — 1 call per pass, not 2-3.

    Telemetry: TEL-FS-26 / TEL-EXEC-05 — 3 r/nosleep section bodies
    tripped this on 2026-05-13 and the renders aborted because each
    section burned 2 LLM calls (initial + retry, both rejected) instead
    of 1. That bug stays fixed.
    """
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    outline = _make_outline(n_sections=4)

    call_state = {"sec-1_count": 0}
    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        section_id = "?"
        for line in prompt.split("\n"):
            if "← THIS SECTION" in line:
                start = line.find("[")
                end = line.find("]")
                if start >= 0 and end > start:
                    section_id = line[start + 1:end]
                break
        if section_id == "sec-1":
            call_state["sec-1_count"] += 1
            raise _rlf._llm.ContentFilterError(
                "azure_openai stage=rewrite_long_form_section "
                "response suppressed by Azure content filter"
            )
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        try:
            env = _rlf.rewrite_long_form(
                raw_story, channel_cfg={},
                target_duration_s=600,
            )
            # If env returned, sec-1 fell back to brief.
            sec1 = next((s for s in env.long_form.sections if s.id == "sec-1"), None)
            assert sec1 is not None
            assert "establishes 1" in sec1.narration
        except LongFormContractError:
            pass  # acceptable when too few sections delivered

    # CRITICAL: each pass made exactly 1 call to sec-1 (no within-pass
    # retry on content_filter). Initial fan-out = 1, auto-repair = 1.
    # The 2026-05-13 regression (burning N retries on a deterministic
    # rejection within a single pass) stays fixed.
    assert call_state["sec-1_count"] == 2, (
        f"content_filter must NOT trigger within-pass retry; "
        f"expected 2 (1 fan-out + 1 repair); got {call_state['sec-1_count']}"
    )


def test_outline_call_runs_before_any_section_body_call():
    """Phase 1 (outline) must complete before Phase 2 (sections) starts."""
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    outline = _make_outline(n_sections=3)
    sequence: list[str] = []

    def _mock(prompt, **kwargs):
        sequence.append(kwargs.get("stage", "?"))
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=600,
        )

    # Outline call MUST be first
    assert sequence[0] == "rewrite_long_form_outline"
    # Every subsequent call MUST be a section call
    for s in sequence[1:]:
        assert s == "rewrite_long_form_section"


# ---------- Phase 3: aggregator ------------------------------------------


def test_aggregator_drops_panels_with_empty_scene():
    """Defensive parse — any panel with empty scene is dropped, not
    crashed-on."""
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    outline = _make_outline(n_sections=3, n_panels=5)
    # Inject a bad panel with empty scene.
    outline["panel_briefs"].append({"scene": "", "hold_s": 6.0})
    outline["panel_briefs"].append({"scene": "   ", "hold_s": 6.0})

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=600,
        )

    # Original 5 valid + 0 of the 2 empty
    assert len(env.long_form.panels) == 5


def test_aggregator_caps_panel_hold_at_renderer_max():
    """Panels with hold_s > PANEL_HOLD_HARD_MAX_S (12s) are clamped
    to PANEL_HOLD_SOFT_MAX_S (8s) — defense-in-depth so a misbehaving
    outline can't ship a dead-frame video."""
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    outline = _make_outline(n_sections=3, n_panels=5)
    # Override all panels to 30s
    for p in outline["panel_briefs"]:
        p["hold_s"] = 30.0

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=600,
        )

    assert all(p.hold_s <= 12.0 for p in env.long_form.panels)


def test_aggregator_truncates_panels_at_60():
    """Cloud-renderer ceiling is 60. Outline emitting 75 → truncate."""
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    outline = _make_outline(n_sections=3, n_panels=75)

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=600,
        )

    assert len(env.long_form.panels) == 60


def test_aggregator_preserves_section_order():
    """Even though section bodies generate in parallel, aggregator
    preserves outline order."""
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    outline = _make_outline(n_sections=6)

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        # Identify which section is being requested by looking for the
        # "← THIS SECTION" marker — it appears on exactly ONE line of
        # the outline summary, next to that section's id.
        section_id = "?"
        for line in prompt.split("\n"):
            if "← THIS SECTION" in line:
                # Line shape: "  - [sec-3] Section 3: ... ← THIS SECTION"
                start = line.find("[")
                end = line.find("]")
                if start >= 0 and end > start:
                    section_id = line[start + 1:end]
                break
        return {
            "narration": f"BODY-{section_id} " + ("filler word " * 100),
            "sentences": [f"Body for {section_id}.", "Filler."],
        }

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={},
            target_duration_s=600,
        )

    # Verify section ids are in outline order.
    expected_ids = [s["id"] for s in outline["sections"]]
    actual_ids = [s.id for s in env.long_form.sections]
    assert actual_ids == expected_ids
    # Verify each section's narration starts with its body marker
    # (proves the body call's output landed in the right slot).
    for env_section in env.long_form.sections:
        assert env_section.narration.startswith(f"BODY-{env_section.id}")


# ---------- end-to-end happy path ----------------------------------------


def test_end_to_end_happy_path_produces_clean_envelope():
    """Full STORM flow: outline → 10 parallel section bodies →
    aggregator → validator passes → ScriptEnvelope returned."""
    raw_story = {"slug": "x", "title": "T", "body": "Source body about shadows watching."}
    outline = _make_outline(n_sections=10, n_panels=24)

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        # Each body delivers ~450 words → 10 × 450 = 4500 → meets the
        # 30-min word target exactly (passes validator soft + hard floors).
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=1800,
        )

    assert env.kind == "long_form"
    assert env.long_form is not None
    assert len(env.long_form.sections) == 10
    assert len(env.long_form.panels) == 24
    assert env.long_form.hook
    assert env.long_form.thesis
    assert env.title_options


# ---------- planner helper (preserved from pre-2026-05-13) ---------------


def test_planned_panels_NOT_capped_at_24():
    """The prompt-planner does NOT artificially cap panel_count_target
    at 24. Renderer's mode-aware cap (24 local, 60 cloud) is the
    source of truth."""
    out = _rlf._planned_sections_and_panels(1800)
    panel_count_target = out[6]
    assert panel_count_target > 24


def test_planned_panels_floor_at_8_for_short_long_form():
    """1-min target → floor of 8 panels (minimum density)."""
    out = _rlf._planned_sections_and_panels(60)
    assert out[6] == 8


# ---------- Auto-repair pass (regression for failed render) -----------
#
# Anchor: job a734babbd3674599bbe7369b5f3e256c failed 2026-05-20 with
#   `section 7 contains 51 words; mean section is 363 words — that's
#    14% of mean. The LLM degraded mid-rewrite (tired-by-the-end
#    pattern). Re-rewrite with explicit per-section minimum.`
# The render aborted with LongFormContractError. After the auto-repair
# pass landed, the user-facing contract is: ANY combination of inputs
# must produce a renderable envelope; length issues become soft warnings.


def test_short_section_triggers_section_too_short_error():
    """The section-body LLM call MUST raise SectionTooShortError when
    the returned narration is below the per-section min_words floor.
    This is what the retry-with-emphasis loop catches."""
    section = {
        "id": "sec-0", "title": "T", "brief": "B",
        "target_words": 450, "visual_brief": "V",
    }
    outline = _make_outline(n_sections=1)
    with patch.object(_rlf._llm, "call_claude_cli") as mocked:
        # min_words for target=450 is 0.70 * 450 = 315. Return 50 words.
        mocked.return_value = {
            "narration": "word " * 50,
            "sentences": ["word word."],
        }
        with pytest.raises(_rlf.SectionTooShortError) as exc_info:
            _rlf._call_section_body_llm(
                channel_context="ctx", topic="topic", thesis="thesis",
                outline=outline, notes="notes", section=section,
                section_words_target=450, section_words_floor=315,
            )
    assert exc_info.value.word_count == 50
    assert exc_info.value.min_words == 315
    assert "sec-0" in str(exc_info.value)


def test_section_too_short_retry_uses_emphasis_block_in_prompt():
    """The retry layer must pass prev_short_word_count to the section-body
    call, which fires the emphasis block in the prompt template."""
    raw_story = {"slug": "x", "title": "T", "body": "Source body about scrolling " * 50}
    outline = _make_outline(n_sections=3)

    captured_prompts: list[str] = []
    call_count = {"n": 0}

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        captured_prompts.append(prompt)
        call_count["n"] += 1
        # First call returns a short body (50 words); subsequent calls
        # return a healthy 450-word body so the retry succeeds.
        if call_count["n"] == 1:
            return {"narration": "word " * 50, "sentences": ["word."]}
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={}, target_duration_s=600,
        )

    # At least one prompt must contain the RETRY NOTICE emphasis block.
    retry_prompts = [p for p in captured_prompts if "RETRY NOTICE" in p]
    assert retry_prompts, "expected at least one retry prompt with emphasis block"
    # The emphasis block must cite the previous short word count.
    assert any("50 words" in p for p in retry_prompts), (
        "retry emphasis must cite the previous short word count"
    )
    # And the envelope must come back fully populated.
    assert env is not None
    assert len(env.long_form.sections) == 3


def test_short_section_does_not_raise_length_contract_error():
    """The exact failure pattern from job a734babb…: section under-delivery
    that previously raised LongFormContractError must now ship the
    envelope (length violations downgraded to soft after auto-repair)."""
    raw_story = {"slug": "x", "title": "T", "body": "Source body content."}
    outline = _make_outline(n_sections=8)

    # Sec-7 always returns a tiny body (51 words, matching the original
    # failure). Every other section returns a healthy 450-word body.
    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        section_id = "?"
        for line in prompt.split("\n"):
            if "← THIS SECTION" in line:
                start = line.find("[")
                end = line.find("]")
                if start >= 0 and end > start:
                    section_id = line[start + 1:end]
                break
        if section_id == "sec-7":
            return {
                "narration": "word " * 51,
                "sentences": ["word."],
            }
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        # MUST NOT raise — the auto-repair pass + downgrade-to-soft
        # logic must let the render proceed even when one section
        # is structurally unable to hit the floor.
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={}, target_duration_s=1800,
        )

    assert env is not None
    # All 8 sections must be present (sec-7 keeps its short body —
    # better short narration than the brief fallback).
    assert len(env.long_form.sections) == 8
    sec7 = next(s for s in env.long_form.sections if s.id == "sec-7")
    assert sec7.narration  # non-empty


def test_auto_repair_skips_when_all_sections_meet_floor():
    """When the initial fan-out hits every section's floor, the
    auto-repair pass MUST be a no-op (no extra LLM calls beyond outline
    + 1-per-section).
    """
    raw_story = {"slug": "x", "title": "T", "body": "Source body."}
    outline = _make_outline(n_sections=4)

    call_count = {"outline": 0, "section": 0}

    def _mock(prompt, **kwargs):
        stage = kwargs.get("stage")
        if stage == "rewrite_long_form_outline":
            call_count["outline"] += 1
            return outline
        call_count["section"] += 1
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={}, target_duration_s=600,
        )

    assert env is not None
    assert call_count["outline"] == 1
    # Exactly 4 section calls — no repair triggered.
    assert call_count["section"] == 4, (
        f"healthy sections should not trigger repair; got {call_count['section']} calls"
    )
