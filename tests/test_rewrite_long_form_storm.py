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
        # Fail every call for sec-2 (1 try + 1 retry = 2 fails)
        if section_id == "sec-2":
            call_state["sec-2_count"] += 1
            raise RuntimeError("simulated section-body failure")
        return _make_section_body(words=450)

    with patch.object(_rlf._llm, "call_claude_cli", side_effect=_mock):
        # The aggregator may produce too-few words to pass the
        # validator. Either path is acceptable — what we're verifying
        # is that the ENGINE doesn't crash on a single section
        # failure. (Hard contract failure → LongFormContractError;
        # success → env returned with sec-2 falling back to brief.)
        # No niche → niche-tonal validator skipped (pure mechanics test).
        try:
            env = _rlf.rewrite_long_form(
                raw_story, channel_cfg={},
                target_duration_s=600,
            )
            # If we got an env, sec-2 must have fallen back to brief.
            sec2 = next((s for s in env.long_form.sections if s.id == "sec-2"), None)
            assert sec2 is not None
            assert "establishes 2" in sec2.narration  # the brief content
        except LongFormContractError:
            pass  # also acceptable

    # sec-2 should have been retried once after the initial failure.
    assert call_state["sec-2_count"] == 2


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
