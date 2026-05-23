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
    target_duration_s: int | None = None,
) -> dict:
    """Build an outline payload that passes every contract check.

    Default per-section ``target_words`` is 450 (matches the legacy
    fixture and the 450-word ``_make_section_body`` default, so the
    new P3.2 ±10% per-section gate passes too). When the test wants
    the P3.3 outline sum-check to PASS, pass ``target_duration_s`` so
    the sum naturally lands in the ±15% band of the global target.
    """
    if target_duration_s is not None:
        total_words = int(target_duration_s * 150 / 60)
        per_section = max(1, total_words // max(1, n_sections))
    else:
        per_section = 450
    return {
        "hook": niche_words + " " + ("dread shadow watching " * 5),
        "thesis": "A test thesis.",
        "sections": [
            {
                "id": f"sec-{i}",
                "title": f"Section {i}",
                "brief": f"This section establishes {i}. " * 4,
                "target_words": per_section,
                "visual_brief": f"A scene for section {i}.",
                "quality_goal": f"establish beat {i} with concrete source detail",
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
    # P3.1: section-body output schema now includes word_count. Mock
    # emits an accurate count (matches len(nar.split())) so tests that
    # validate envelope-side behaviour don't trip the anchoring-mismatch
    # telemetry path unnecessarily.
    return {"narration": nar, "sentences": sentences, "word_count": len(nar.split())}


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
    """Panels with hold_s > PANEL_HOLD_HARD_MAX_S are clamped to
    PANEL_HOLD_SOFT_MAX_S — defense-in-depth so a misbehaving outline
    can't ship absurdly long static stills.

    2026-05-23: caps raised (Ken-Burns removed) — HARD 60s, SOFT 45s.
    A panel emitting hold_s=90 hits the cap and gets clamped to 45.
    """
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    outline = _make_outline(n_sections=3, n_panels=5)
    # Override all panels to 90s (above the new 60s hard cap)
    for p in outline["panel_briefs"]:
        p["hold_s"] = 90.0

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=600,
        )

    assert all(p.hold_s <= 60.0 for p in env.long_form.panels)


def test_aggregator_truncates_panels_at_channel_default():
    """Channel YAML's ``long_form.panel_max_count`` controls truncation.
    Default (no override) is 120 (was 60 pre-2026-05-23).
    """
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    outline = _make_outline(n_sections=3, n_panels=150)

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={"niche": "r/nosleep"},
            target_duration_s=600,
        )

    assert len(env.long_form.panels) == 120


def test_aggregator_truncates_panels_at_channel_override():
    """Channel YAML can override the cap: ``long_form.panel_max_count: 30``
    truncates to 30, regardless of the 120 default.
    """
    raw_story = {"slug": "x", "title": "T", "body": "Source"}
    outline = _make_outline(n_sections=3, n_panels=75)

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story,
            channel_cfg={
                "niche": "r/nosleep",
                "long_form": {"panel_max_count": 30},
            },
            target_duration_s=600,
        )

    assert len(env.long_form.panels) == 30


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
        # P3.2: min_words is 0.90 * target (±10% section band). For
        # target=450 → min_words=405. Return 50 words (way under).
        mocked.return_value = {
            "narration": "word " * 50,
            "sentences": ["word word."],
        }
        with pytest.raises(_rlf.SectionTooShortError) as exc_info:
            _rlf._call_section_body_llm(
                channel_context="ctx", topic="topic", thesis="thesis",
                outline=outline, notes="notes", section=section,
                section_words_target=450, section_words_floor=405,
            )
    assert exc_info.value.word_count == 50
    assert exc_info.value.min_words == 405
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



def test_section_body_prompt_does_not_contain_do_not_pad_phrase():
    """P3.5: the 'Do NOT pad with filler' negative-framing block must be
    gone from the section-body prompt template. Negative phrasing causes
    LLMs to over-correct (Q61)."""
    template = _rlf._SECTION_BODY_PROMPT_TEMPLATE
    assert "Do NOT pad with filler" not in template, (
        "'Do NOT pad with filler' negative-framing must not return — "
        "outline-authored quality_goal replaces it (P3.5)"
    )
    # And the prompt MUST have a {quality_goal} format slot.
    assert "{quality_goal}" in template, (
        "section-body prompt must accept quality_goal as a positive spec"
    )


def test_outline_schema_requires_quality_goal_per_section():
    """P3.5: outline JSON schema must mark `quality_goal` as required
    on every section, so the outline LLM authors a per-section positive
    spec rather than the section-body call falling back to a generic."""
    schema = _rlf._OUTLINE_SCHEMA
    section_schema = schema["properties"]["sections"]["items"]
    assert "quality_goal" in section_schema["required"], (
        "outline schema must require quality_goal per section (P3.5)"
    )
    assert "quality_goal" in section_schema["properties"], (
        "outline schema must define the quality_goal property (P3.5)"
    )


def test_quality_goal_flows_from_outline_into_section_body_prompt():
    """P3.5: each section-body call's prompt must contain the
    quality_goal value the outline authored for THAT section."""
    raw_story = {"slug": "x", "title": "T", "body": "Source body."}
    outline = _make_outline(n_sections=4)
    # Override quality_goal so we can spot the actual text in prompts.
    for i, s in enumerate(outline["sections"]):
        s["quality_goal"] = f"UNIQUE-GOAL-FOR-SEC-{i}-XYZ"

    captured: dict[str, str] = {}

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        if kwargs.get("stage") == "rewrite_long_form_section":
            # Find which section by the THIS SECTION marker.
            for line in prompt.split("\n"):
                if "← THIS SECTION" in line:
                    start = line.find("[")
                    end = line.find("]")
                    if start >= 0 and end > start:
                        captured[line[start + 1:end]] = prompt
                    break
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        _rlf.rewrite_long_form(
            raw_story, channel_cfg={}, target_duration_s=600,
        )

    # Every section-body prompt must contain its own unique quality_goal.
    for i in range(4):
        sid = f"sec-{i}"
        assert sid in captured, f"section {sid} prompt not captured"
        expected = f"UNIQUE-GOAL-FOR-SEC-{i}-XYZ"
        assert expected in captured[sid], (
            f"section {sid} prompt must include quality_goal {expected!r}"
        )


def test_normalize_outline_backfills_quality_goal_when_missing():
    """P3.5 robustness: if an outline LLM (older deployment, partial JSON)
    omits quality_goal, the normalizer must apply a positive fallback so
    the section-body prompt template still formats cleanly."""
    outline_in = {
        "hook": "x", "thesis": "y",
        "sections": [
            {"id": "sec-0", "title": "A", "brief": "B" * 40,
             "target_words": 400, "visual_brief": "V"},  # no quality_goal
        ],
        "panel_briefs": [], "sources": [], "title_options": ["T"],
    }
    raw_story = {"title": "T", "body": "B"}
    norm = _rlf._normalize_outline(
        outline_in,
        section_count_target=1, section_words_target=400,
        raw_story=raw_story, title_options_count=1,
    )
    assert norm["sections"][0]["quality_goal"]  # non-empty
    # Fallback must NOT be negative framing.
    qg = norm["sections"][0]["quality_goal"].lower()
    assert "do not" not in qg
    assert "don't" not in qg


# ---------- P3.1 — word_count self-emit field ---------------------------
#
# Q57.1 / P3.1: the section-body JSON output must include a `word_count`
# field that the LLM self-emits. The validator uses len(narration.split())
# as the source of truth (anchoring is the point); emitted-vs-actual
# delta is logged as telemetry only.


def test_section_body_schema_requires_word_count_field():
    """P3.1: the section-body JSON schema MUST require word_count so the
    LLM is forced to count what it writes (length anchoring)."""
    schema = _rlf._SECTION_BODY_SCHEMA
    assert "word_count" in schema["required"], (
        "section-body schema must require word_count (P3.1)"
    )
    assert schema["properties"]["word_count"]["type"] == "integer", (
        "word_count must be an integer field"
    )


def test_section_body_prompt_instructs_word_count_emission():
    """P3.1: the prompt must instruct the LLM to emit word_count in its
    JSON output (anchoring is the mechanism — counting forces care)."""
    template = _rlf._SECTION_BODY_PROMPT_TEMPLATE
    assert "word_count" in template, (
        "section-body prompt must mention word_count output field (P3.1)"
    )


def test_validator_uses_actual_word_count_not_emitted_value():
    """P3.1 (lock from Q60): when the LLM emits a word_count that
    disagrees with the actual narration word count, the gate must use
    the ACTUAL count. Lying about the count doesn't bypass the gate."""
    section = {
        "id": "sec-0", "title": "T", "brief": "B" * 40,
        "target_words": 450, "visual_brief": "V",
        "quality_goal": "establish stakes",
    }
    outline = _make_outline(n_sections=1)
    with patch.object(_rlf._llm, "call_claude_cli") as mocked:
        # 50 actual words; LLM emits 1000 (lying high). Validator MUST
        # ignore the emitted value and use the actual count.
        mocked.return_value = {
            "narration": "word " * 50,
            "sentences": ["word."],
            "word_count": 1000,
        }
        with pytest.raises(_rlf.SectionTooShortError) as exc:
            _rlf._call_section_body_llm(
                channel_context="ctx", topic="topic", thesis="thesis",
                outline=outline, notes="notes", section=section,
                section_words_target=450, section_words_floor=315,
            )
    # The error's word_count must reflect the ACTUAL count (50), not
    # the emitted lie (1000).
    assert exc.value.word_count == 50


# ---------- P3.3 — outline sum-check retry ------------------------------
#
# Q63 / P3.3: if sum(section.target_words) is outside ±15% of the user's
# total target, retry the outline once with the error in the prompt. If
# the 2nd outline is also out of band, hard-fail.


def test_outline_sum_check_retries_when_out_of_band():
    """P3.3: outline whose section sum is outside ±15% triggers a retry
    with the sum error appended to the prompt."""
    raw_story = {"slug": "x", "title": "T", "body": "Source body about scrolling."}
    # 30-min target = 4500 words. ±15% band: 3825 - 5175.
    bad_outline = _make_outline(n_sections=6)
    # Force section sums to ~3000 — well below the ±15% band.
    for s in bad_outline["sections"]:
        s["target_words"] = 500
    good_outline = _make_outline(n_sections=10)
    # Force section sums to ~4500 — inside ±15% band.
    for s in good_outline["sections"]:
        s["target_words"] = 450

    call_state = {"outline_calls": 0, "captured_prompts": []}

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            call_state["outline_calls"] += 1
            call_state["captured_prompts"].append(prompt)
            if call_state["outline_calls"] == 1:
                return bad_outline
            return good_outline
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={}, target_duration_s=1800,
        )

    assert env is not None
    # Outline LLM was called TWICE (once produced bad sum; retry produced good).
    assert call_state["outline_calls"] == 2, (
        f"outline sum-check must trigger a retry on out-of-band; "
        f"got {call_state['outline_calls']} outline calls"
    )
    # The second outline prompt must mention the sum error.
    second_prompt = call_state["captured_prompts"][1]
    assert "Previous outline" in second_prompt or "outline had section sums" in second_prompt, (
        "outline retry prompt must surface the sum error to the LLM"
    )


def test_outline_sum_check_skipped_when_in_band():
    """P3.3: outline whose section sum is within ±15% of the user target
    must NOT trigger a retry (single outline call)."""
    raw_story = {"slug": "x", "title": "T", "body": "Source body."}
    # 30-min target = 4500 words. ±15% band: 3825 - 5175.
    outline = _make_outline(n_sections=10)
    for s in outline["sections"]:
        s["target_words"] = 450  # sum = 4500 → exactly target

    call_count = {"outline": 0, "section": 0}

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            call_count["outline"] += 1
            return outline
        call_count["section"] += 1
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={}, target_duration_s=1800,
        )
    assert env is not None
    # No retry on a good outline.
    assert call_count["outline"] == 1


def test_outline_sum_check_second_bad_outline_is_logged_not_raised(caplog):
    """P3.3 (post-2026-05-23 calibration): if the 2nd outline is also
    out of band, the worker logs and continues. Per ADR-023 the gates
    are repair triggers; the downstream length validator
    (``validate_long_form_envelope``) already enforces correctness on
    the aggregated narration, so terminating here loses an iteration
    we could otherwise spend. The test pins the LOGGED signal so
    operators can still diagnose."""
    import logging
    raw_story = {"slug": "x", "title": "T", "body": "Source body."}
    bad_outline = _make_outline(n_sections=6)
    for s in bad_outline["sections"]:
        s["target_words"] = 200  # sum = 1200, way below 4500 ±15%

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return bad_outline
        return _make_section_body(words=450)

    caplog.set_level(logging.WARNING, logger="pipeline.llm.rewrite_long_form")
    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        env = _rlf.rewrite_long_form(
            raw_story, channel_cfg={}, target_duration_s=1800,
        )
    assert env is not None
    # The two warn/error log lines must surface the sum-check signal.
    sum_check_msgs = [
        r.message for r in caplog.records
        if "outline" in r.message.lower() and "band" in r.message.lower()
    ]
    assert len(sum_check_msgs) >= 2, (
        f"expected ≥2 outline-sum-check log lines (one warning on retry + "
        f"one error on the 2nd bad outline); got: {sum_check_msgs}"
    )


# ---------- P3.6 — iterative-extend retry shape -------------------------
#
# Q62 / P3.6: when a section fails its ±10% gate, the retry must be
# iterative-extend (LLM sees its own failed draft + an "expand to N
# words by adding source detail" instruction), not a regenerate-from-
# scratch loop that wastes its prior context.


def test_section_retry_prompt_includes_previous_draft_for_extend():
    """P3.6: the retry prompt for a short section MUST include the
    previous draft text (so the LLM extends, not rewrites)."""
    raw_story = {"slug": "x", "title": "T", "body": "Source body content."}
    outline = _make_outline(n_sections=2)

    captured_prompts: list[str] = []
    call_state = {"n": 0}
    failed_draft = "SHORT_DRAFT_MARKER " * 10  # unique marker we can search for

    def _mock(prompt, **kwargs):
        if kwargs.get("stage") == "rewrite_long_form_outline":
            return outline
        captured_prompts.append(prompt)
        call_state["n"] += 1
        if call_state["n"] == 1:
            # First call returns a too-short body with our marker.
            return {
                "narration": failed_draft,
                "sentences": ["short."],
                "word_count": 20,
            }
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        _rlf.rewrite_long_form(
            raw_story, channel_cfg={}, target_duration_s=300,
        )

    # The 2nd call (retry of the failed section) must include the
    # failed draft text — iterative extend, not regenerate.
    retry_prompts = [p for p in captured_prompts[1:] if "SHORT_DRAFT_MARKER" in p]
    assert retry_prompts, (
        "P3.6: section retry must include the failed draft for iterative "
        "extension, not regenerate from scratch"
    )
    # And the retry prompt must say "expand" or "extend" (not "rewrite").
    one_retry = retry_prompts[0]
    assert ("expand" in one_retry.lower() or "extend" in one_retry.lower()), (
        "P3.6: retry prompt must explicitly instruct extending the draft"
    )
